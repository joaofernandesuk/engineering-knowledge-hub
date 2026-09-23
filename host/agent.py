"""Small native host control plane. Standard library only; never accepts shell commands."""
from __future__ import annotations
import argparse
import json
import os
import re
import secrets
import signal
import shutil
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

PROJECT_ID = re.compile(r"^[a-f0-9]{16}$")
NAME = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,80}$")
ALLOWED = {"status", "preview", "register_project", "unregister_project", "rename_project", "graphify_update", "knowledge_enable", "set_vault", "open_repository", "open_native_obsidian", "operation"}

class AgentError(Exception):
    pass

def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)

def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default

def run_process(command, *, cwd, timeout, env=None):
    """Run a fixed argv and kill its whole process group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        stdout, stderr = process.communicate()
        exc.stdout, exc.stderr = stdout, stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

class HostAgent:
    def __init__(self, runtime: Path, source: Path, reconcile=True, graphify="graphify"):
        self.runtime = runtime.expanduser().resolve()
        self.source = source.expanduser().resolve()
        self.reconcile = reconcile
        self.graphify = graphify
        self.registry_path = self.runtime / "projects.json"
        self.config_path = self.runtime / "config.json"
        self.override_path = self.runtime / "generated" / "compose.generated.yaml"
        self.agent_dir = self.runtime / "agent"
        self.ops_dir = self.runtime / "operations"
        self.audit_path = self.runtime / "logs" / "audit.jsonl"
        self.lock = threading.RLock()
        self.active_updates: set[str] = set()
        for path in (self.runtime, self.agent_dir, self.ops_dir, self.audit_path.parent, self.override_path.parent):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        for token in ("agent.token", "web.token"):
            path = self.agent_dir / token
            if not path.exists():
                path.write_text(secrets.token_urlsafe(36))
                os.chmod(path, 0o600)
        if not self.registry_path.exists(): atomic_json(self.registry_path, [])
        if not self.config_path.exists(): atomic_json(self.config_path, {"vault": None})
        self._render_override(self.registry(), self.config())

    def _validate_rows(self, rows):
        if not isinstance(rows, list):
            raise AgentError("Invalid project registry")
        ids, names = set(), set()
        for row in rows:
            if not isinstance(row, dict):
                raise AgentError("Invalid project registry entry")
            project_id, name, repo_value = row.get("id"), row.get("name"), row.get("repo")
            if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id) or project_id in ids:
                raise AgentError("Invalid or duplicate project ID in registry")
            if not isinstance(name, str) or not NAME.fullmatch(name) or name.strip() != name or name.casefold() in names:
                raise AgentError("Invalid or duplicate project name in registry")
            repo = self._safe_path(repo_value, must_exist=False)
            if str(repo) != repo_value:
                raise AgentError("Project registry paths must be canonical absolute paths")
            graph = row.get("graph")
            if graph is not None and graph != str(repo / "graphify-out"):
                raise AgentError("Invalid graph path in project registry")
            note = row.get("obsidian_note")
            if note is not None:
                note_path = PurePosixPath(note)
                if note_path.is_absolute() or not note_path.parts or any(part in ("", ".", "..") for part in note_path.parts) or note_path.suffix.casefold() != ".md":
                    raise AgentError("Invalid knowledge path in project registry")
            ids.add(project_id)
            names.add(name.casefold())
        return rows

    def registry(self):
        try:
            rows = read_json(self.registry_path, [])
            return self._validate_rows(rows)
        except (OSError, ValueError, TypeError, AgentError) as exc:
            backup_dir = self.runtime / "generated" / "backups"
            for backup in sorted(backup_dir.glob("*-projects.json"), reverse=True):
                try:
                    rows = self._validate_rows(read_json(backup, []))
                    atomic_json(self.registry_path, rows)
                    self._audit("recover_registry", None, "success")
                    return rows
                except (OSError, ValueError, TypeError, AgentError):
                    continue
            raise AgentError("Invalid project registry and no valid backup is available") from exc

    def config(self):
        try:
            config = read_json(self.config_path, {"vault": None})
            return self._validate_config(config)
        except (OSError, ValueError, TypeError, AgentError) as exc:
            backup_dir = self.runtime / "generated" / "backups"
            for backup in sorted(backup_dir.glob("*-config.json"), reverse=True):
                try:
                    config = self._validate_config(read_json(backup, {"vault": None}))
                    atomic_json(self.config_path, config)
                    self._audit("recover_config", None, "success")
                    return config
                except (OSError, ValueError, TypeError, AgentError):
                    continue
            raise AgentError("Invalid Hub configuration and no valid backup is available") from exc

    def _validate_config(self, config):
        if not isinstance(config, dict) or set(config) - {"vault"}:
            raise AgentError("Invalid Hub configuration")
        vault = config.get("vault")
        if vault is not None:
            checked = self._safe_path(vault, must_exist=False)
            if str(checked) != vault:
                raise AgentError("Vault path must be a canonical absolute path")
        return {"vault": vault}

    def _audit(self, operation, project_id, result):
        row = {"time": datetime.now(timezone.utc).isoformat(), "operation": operation, "project_id": project_id, "result": result}
        with open(self.audit_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        os.chmod(self.audit_path, 0o600)

    def _safe_path(self, value, must_exist=True):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise AgentError("A valid absolute directory path is required")
        raw = Path(value).expanduser()
        if not raw.is_absolute() or ".." in raw.parts:
            raise AgentError("Path must be absolute and contain no traversal")
        current = Path(raw.anchor)
        for part in raw.parts[1:]:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise AgentError("Symlink paths are not accepted")
            except FileNotFoundError:
                if must_exist: raise AgentError("Directory does not exist")
        if must_exist and not raw.is_dir(): raise AgentError("Path is not a readable directory")
        if must_exist and not os.access(raw, os.R_OK | os.X_OK): raise AgentError("Directory is not accessible")
        return raw.resolve(strict=must_exist)

    def _git(self, repo):
        def run(*args):
            try:
                p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=5, check=False)
                return p.stdout.strip() if p.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired): return None
        root = run("rev-parse", "--show-toplevel")
        if root and Path(root).resolve() != repo: raise AgentError("Select the Git repository root, not a subdirectory")
        return {"is_git": bool(root), "branch": run("branch", "--show-current") if root else None, "sha": run("rev-parse", "--short=8", "HEAD") if root else None}

    def preview(self, path):
        repo = self._safe_path(path)
        rows = self.registry()
        for p in rows:
            old = Path(p["repo"]).resolve()
            if old == repo: raise AgentError("Project is already registered")
            if repo.is_relative_to(old) or old.is_relative_to(repo):
                raise AgentError("Nested project overlaps an existing project")
        graph = repo / "graphify-out"
        if graph.is_symlink(): raise AgentError("Graph output must not be a symlink")
        git = self._git(repo)
        graph_ready = (graph / "graph.json").is_file() and not (graph / "graph.json").is_symlink()
        config = self.config()
        vault = Path(config["vault"]) if config.get("vault") else None
        suggested = repo.name
        existing = str(Path("Projects") / suggested / "Overview.md") if vault and (vault / "Projects" / suggested / "Overview.md").is_file() else None
        return {"repo": str(repo), "name": suggested, "git": git, "graphify_installed": bool(shutil.which(self.graphify)), "graph_ready": graph_ready, "existing_knowledge": existing, "vault_configured": bool(vault and vault.is_dir())}

    def _get(self, project_id):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id): raise AgentError("Invalid project ID")
        return next((p for p in self.registry() if p["id"] == project_id), None)

    def _render_override(self, rows, config):
        rows = self._validate_rows(rows)
        mounts = [{"type": "bind", "source": str(self.runtime), "target": "/data", "read_only": True},
                  {"type": "bind", "source": str(self.agent_dir), "target": "/run/hub-agent", "read_only": True}]
        vault = config.get("vault")
        try:
            safe_vault = self._safe_path(vault) if vault else None
        except AgentError:
            safe_vault = None
        if safe_vault:
            mounts.append({"type": "bind", "source": str(safe_vault), "target": "/knowledge", "read_only": True})
        else:
            placeholder = self.runtime / "empty-vault"
            placeholder.mkdir(exist_ok=True)
            mounts.append({"type": "bind", "source": str(placeholder), "target": "/knowledge", "read_only": True})
        for p in rows:
            try:
                repo = self._safe_path(p["repo"])
                graph = repo / "graphify-out"
                if graph.is_dir() and not graph.is_symlink() and graph.resolve(strict=True).is_relative_to(repo):
                    mounts.append({"type": "bind", "source": str(graph.resolve(strict=True)), "target": "/graphs/" + p["id"], "read_only": True})
            except (AgentError, OSError):
                continue
        data = {"services": {"hub": {"volumes": mounts}}}
        atomic_json(self.override_path, data)

    def _reconcile(self):
        if not self.reconcile: return
        cmd = ["docker", "compose", "-p", os.environ.get("HUB_COMPOSE_PROJECT", "engineering-knowledge-hub"), "-f", str(self.source / "compose.yaml"), "-f", str(self.override_path), "up", "-d", "--force-recreate"]
        try:
            p = run_process(cmd, cwd=self.source, timeout=300, env={**os.environ, "HUB_UID": str(os.getuid()), "HUB_GID": str(os.getgid())})
        except (OSError, subprocess.TimeoutExpired) as exc: raise AgentError("Hub reconciliation failed: " + str(exc)) from exc
        if p.returncode: raise AgentError("Hub reconciliation failed: " + (p.stderr or p.stdout)[-1000:])

    def _commit_state(self, rows, config=None):
        config = config if config is not None else self.config()
        old_rows, old_config = self.registry(), self.config()
        old_override = self.override_path.read_text() if self.override_path.exists() else None
        backup_dir = self.runtime / "generated" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)
        atomic_json(backup_dir / (stamp + "-projects.json"), old_rows)
        atomic_json(backup_dir / (stamp + "-config.json"), old_config)
        if old_override is not None:
            (backup_dir / (stamp + "-compose.json")).write_text(old_override)
        groups = sorted({p.name.split("-projects.json")[0] for p in backup_dir.glob("*-projects.json")})
        for older in groups[:-5]:
            for f in backup_dir.glob(older + "-*"): f.unlink(missing_ok=True)
        try:
            self._render_override(rows, config)
            atomic_json(self.registry_path, rows)
            atomic_json(self.config_path, config)
            self._reconcile()
        except Exception:
            atomic_json(self.registry_path, old_rows)
            atomic_json(self.config_path, old_config)
            if old_override is not None:
                temp = self.override_path.with_suffix(".rollback")
                temp.write_text(old_override)
                os.replace(temp, self.override_path)
            raise

    def _operation_path(self, op_id):
        if not isinstance(op_id, str) or not PROJECT_ID.fullmatch(op_id): raise AgentError("Invalid operation ID")
        return self.ops_dir / (op_id + ".json")

    def _event(self, op_id, step, status, detail=None):
        path = self._operation_path(op_id)
        op = read_json(path, {"id": op_id, "events": [], "status": "running"})
        op["events"].append({"time": datetime.now(timezone.utc).isoformat(), "step": step, "status": status, "detail": detail})
        if status in ("complete", "failed"): op["status"] = status
        atomic_json(path, op)

    def _run_async(self, operation, payload):
        pid = payload.get("project_id")
        if operation == "graphify_update":
            with self.lock:
                if pid in self.active_updates: raise AgentError("Graph update already running")
                self.active_updates.add(pid)
        op_id = secrets.token_hex(8)
        self._event(op_id, "Queued", "running")
        def work():
            pid = payload.get("project_id")
            try:
                with self.lock:
                    result = self._execute(operation, payload, op_id)
                self._event(op_id, "Ready", "complete", result)
                self._audit(operation, pid or (result or {}).get("project_id"), "success")
            except Exception as exc:
                self._event(op_id, "Failed", "failed", str(exc))
                self._audit(operation, pid, "failed")
            finally:
                if operation == "graphify_update":
                    with self.lock: self.active_updates.discard(pid)
        threading.Thread(target=work, daemon=True).start()
        return {"operation_id": op_id}

    def _graphify(self, repo, update, op_id):
        executable = shutil.which(self.graphify)
        if not executable: raise AgentError("Graphify is not installed on the host")
        repo = self._safe_path(str(repo))
        repo_identity = repo.stat()
        graph_dir = repo / "graphify-out"
        if graph_dir.is_symlink(): raise AgentError("Graph output must not be a symlink")
        self._event(op_id, "Generating code graph" if not update else "Updating code graph", "running")
        cmd = [executable, "update", "."] if update else [executable, ".", "--code-only"]
        try:
            p = run_process(cmd, cwd=repo, timeout=900)
        except subprocess.TimeoutExpired: raise AgentError("Graphify timed out after 15 minutes")
        if p.returncode: raise AgentError("Graphify failed: " + (p.stderr or p.stdout)[-1000:])
        checked_repo = self._safe_path(str(repo))
        checked_identity = checked_repo.stat()
        if (repo_identity.st_dev, repo_identity.st_ino) != (checked_identity.st_dev, checked_identity.st_ino):
            raise AgentError("Repository changed while Graphify was running")
        graph = checked_repo / "graphify-out"
        graph_json = graph / "graph.json"
        if graph.is_symlink() or not graph.is_dir() or graph_json.is_symlink() or not graph_json.is_file() or not graph_json.resolve(strict=True).is_relative_to(checked_repo):
            raise AgentError("Graphify finished without a safe graph.json")
        if not (graph / "graph.html").is_file():
            try:
                run_process([executable, "export", "html", "--graph", "graphify-out/graph.json"], cwd=checked_repo, timeout=120)
            except subprocess.TimeoutExpired:
                raise AgentError("Graphify HTML export timed out after 2 minutes")

    def _knowledge(self, row):
        config = self.config()
        if not config.get("vault"): raise AgentError("Choose a knowledge vault first")
        vault = self._safe_path(config["vault"])
        name = row["name"]
        safe = re.sub(r"[^A-Za-z0-9 ._-]", "", name).strip(" .") or "Project"
        folder = vault / "Projects" / safe
        current = vault
        for part in ("Projects", safe):
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise AgentError("Knowledge path must not contain symlinks")
            except FileNotFoundError:
                pass
        if not folder.resolve(strict=False).is_relative_to(vault):
            raise AgentError("Knowledge path escapes the selected vault")
        path = folder / "Overview.md"
        if path.is_symlink(): raise AgentError("Overview must not be a symlink")
        relative_note = path.relative_to(vault).as_posix()
        if any(existing.get("id") != row.get("id") and existing.get("obsidian_note") == relative_note for existing in self.registry()):
            raise AgentError("Knowledge folder is already mapped to another project")
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink() or not folder.resolve(strict=True).is_relative_to(vault):
            raise AgentError("Knowledge path escapes the selected vault")
        if not path.exists():
            body = f"# {name}\n\n## Purpose\n\n_To be documented._\n\n## Current architecture\n\n_To be documented._\n\n## Major systems\n\n_To be documented._\n\n## Important decisions\n\n_To be documented._\n\n## Current risks / debt\n\n_To be documented._\n\n## Graphify\n\nProject: {row['id']}\n"
            with open(path, "x", encoding="utf-8") as stream: stream.write(body)
        row["obsidian_note"] = relative_note

    def _execute(self, operation, payload, op_id):
        if operation == "set_vault":
            vault = self._safe_path(payload.get("path"))
            config = self.config(); config["vault"] = str(vault)
            self._event(op_id, "Configuring knowledge vault", "running")
            self._commit_state(self.registry(), config)
            return {"vault": str(vault)}
        if operation == "register_project":
            path = payload.get("path")
            check = self.preview(path)
            name = payload.get("name") or check["name"]
            if not isinstance(name, str) or not NAME.fullmatch(name) or name.strip() != name: raise AgentError("Invalid display name")
            rows = self.registry()
            if any(p["name"].casefold() == name.casefold() for p in rows): raise AgentError("Display name is already used")
            row = {"id": secrets.token_hex(8), "name": name, "repo": check["repo"], "graph": str(Path(check["repo"]) / "graphify-out"), "branch": check["git"]["branch"], "sha": check["git"]["sha"], "registered_at": datetime.now(timezone.utc).isoformat(), "obsidian_note": None}
            self._event(op_id, "Repository validated", "running")
            if payload.get("knowledge"):
                self._knowledge(row)
                self._event(op_id, "Knowledge mapping created", "running")
            rows.append(row)
            self._event(op_id, "Registering project", "running")
            self._commit_state(rows)
            if payload.get("graphify") and payload.get("generate_graph") and not check["graph_ready"]:
                try:
                    self._graphify(Path(row["repo"]), False, op_id)
                    self._event(op_id, "Mounting code graph", "running")
                    self._render_override(self.registry(), self.config())
                    self._reconcile()
                except Exception as exc:
                    return {"project_id": row["id"], "warning": str(exc)}
            return {"project_id": row["id"]}
        if operation == "rename_project":
            row = self._get(payload.get("project_id"))
            if not row: raise AgentError("Project is not registered")
            name = payload.get("name")
            if not isinstance(name, str) or not NAME.fullmatch(name) or name.strip() != name: raise AgentError("Invalid display name")
            rows = self.registry()
            if any(p["id"] != row["id"] and p["name"].casefold() == name.casefold() for p in rows): raise AgentError("Display name is already used")
            next(p for p in rows if p["id"] == row["id"])["name"] = name
            self._event(op_id, "Updating project name", "running")
            self._commit_state(rows)
            return {"project_id": row["id"]}
        if operation == "unregister_project":
            row = self._get(payload.get("project_id"))
            if not row: raise AgentError("Project is not registered")
            if payload.get("confirm") != row["name"]: raise AgentError("Confirmation name does not match")
            self._event(op_id, "Unregistering project", "running")
            self._commit_state([p for p in self.registry() if p["id"] != row["id"]])
            return {"project_id": row["id"]}
        if operation == "graphify_update":
            row = self._get(payload.get("project_id"))
            if not row: raise AgentError("Project is not registered")
            repo = self._safe_path(row["repo"])
            graph = repo / "graphify-out" / "graph.json"
            self._graphify(repo, graph.is_file(), op_id)
            self._event(op_id, "Refreshing Hub", "running")
            self._render_override(self.registry(), self.config())
            self._reconcile()
            return {"project_id": row["id"]}
        if operation == "knowledge_enable":
            row = self._get(payload.get("project_id"))
            if not row: raise AgentError("Project is not registered")
            rows = self.registry()
            selected = next(p for p in rows if p["id"] == row["id"])
            self._knowledge(selected)
            self._event(op_id, "Creating knowledge mapping", "running")
            self._commit_state(rows)
            return {"project_id": row["id"]}
        raise AgentError("Operation is not available")

    def run_external(self, operation, payload, op_id, expires_at=None):
        if not PROJECT_ID.fullmatch(op_id): raise AgentError("Invalid operation ID")
        if expires_at is not None and time.time() > expires_at:
            self._event(op_id, "Host request expired before execution", "failed")
            return
        if operation not in ALLOWED or operation in ("status", "preview", "operation", "open_repository", "open_native_obsidian"):
            self._event(op_id, "Operation is not allowed", "failed")
            return
        pid = payload.get("project_id")
        if operation == "graphify_update":
            with self.lock:
                if pid in self.active_updates:
                    self._event(op_id, "Graph update already running", "failed")
                    return
                self.active_updates.add(pid)
        self._event(op_id, "Queued", "running")
        def work():
            pid = payload.get("project_id")
            try:
                with self.lock: result = self._execute(operation, payload, op_id)
                self._event(op_id, "Ready", "complete", result)
                self._audit(operation, pid or (result or {}).get("project_id"), "success")
            except Exception as exc:
                self._event(op_id, "Failed", "failed", str(exc))
                self._audit(operation, pid, "failed")
            finally:
                if operation == "graphify_update":
                    with self.lock: self.active_updates.discard(pid)
        threading.Thread(target=work, daemon=True).start()

    def dispatch(self, operation, payload):
        if operation not in ALLOWED: raise AgentError("Operation is not allowed")
        if not isinstance(payload, dict): raise AgentError("Malformed operation payload")
        if operation == "status":
            return {"graphify_installed": bool(shutil.which(self.graphify)), "vault": self.config().get("vault"), "projects": len(self.registry()), "obsidian_installed": bool(shutil.which("obsidian") or (sys.platform == "darwin" and Path('/Applications/Obsidian.app').exists()))}
        if operation == "preview": return self.preview(payload.get("path"))
        if operation == "operation": return read_json(self._operation_path(payload.get("operation_id")), None)
        if operation in ("open_repository", "open_native_obsidian"):
            row = self._get(payload.get("project_id"))
            if not row: raise AgentError("Project is not registered")
            if operation == "open_repository":
                target = str(self._safe_path(row["repo"]))
                cmd = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"opened": True}
            note = row.get("obsidian_note")
            vault = self.config().get("vault")
            if not note or not vault: raise AgentError("Knowledge is not configured")
            uri = "obsidian://open?vault=" + quote(Path(vault).name) + "&file=" + quote(note)
            cmd = ["open", uri] if sys.platform == "darwin" else ["xdg-open", uri]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"opened": True}
        return self._run_async(operation, payload)

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536: raise AgentError("Request too large")
            data = json.loads(raw)
            expected = self.server.agent.agent_dir.joinpath("agent.token").read_text().strip()
            if not secrets.compare_digest(str(data.get("token", "")), expected): raise AgentError("Authentication required")
            result = self.server.agent.dispatch(data.get("operation"), data.get("payload"))
            response = {"ok": True, "data": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode())

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

def bridge_loop(agent, url):
    base = url.rstrip("/")
    token = agent.agent_dir.joinpath("agent.token").read_text().strip()
    while True:
        try:
            req = urllib.request.Request(base + "/internal/agent/next", headers={"X-Agent-Token": token})
            with urllib.request.urlopen(req, timeout=5) as response:
                ticket = json.load(response).get("ticket")
            if not ticket: continue
            op, payload, op_id = ticket["operation"], ticket["payload"], ticket["id"]
            if ticket["synchronous"]:
                try: result = {"ok": True, "data": agent.dispatch(op, payload)}
                except Exception as exc: result = {"ok": False, "error": str(exc)}
                req = urllib.request.Request(base + "/internal/agent/result/" + op_id, json.dumps(result).encode(), headers={"X-Agent-Token": token, "Content-Type": "application/json", "Origin": base}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as response: response.read()
            else:
                agent.run_external(op, payload, op_id, ticket.get("expires_at"))
        except (OSError, ValueError, KeyError, AgentError) as exc:
            time.sleep(1)

def serve(agent, bridge_url=None):
    sock = agent.agent_dir / "control.sock"
    try: sock.unlink()
    except FileNotFoundError: pass
    with Server(str(sock), Handler) as server:
        server.agent = agent
        os.chmod(sock, 0o600)
        if bridge_url:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            bridge_loop(agent, bridge_url)
        else:
            server.serve_forever()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "init", "status"])
    parser.add_argument("--runtime", default=os.environ.get("HUB_RUNTIME", str(Path.home() / ".engineering-knowledge-hub")))
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--no-reconcile", action="store_true")
    parser.add_argument("--bridge-url", default=None)
    args = parser.parse_args()
    agent = HostAgent(Path(args.runtime), Path(args.source), not args.no_reconcile)
    if args.command == "serve": serve(agent, args.bridge_url)
    elif args.command == "status": print(json.dumps(agent.dispatch("status", {})))
    else: print(f"Initialized {agent.runtime}")

if __name__ == "__main__": main()
