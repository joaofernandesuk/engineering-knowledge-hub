from __future__ import annotations
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from .config import settings
from .control import ControlError, request as agent_request, bridge_next, bridge_result
from .index import SearchIndex
from .knowledge import KnowledgeIndex, safe_relative

app = FastAPI(title="Engineering Knowledge Hub", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
knowledge = KnowledgeIndex(settings.vault, refresh_seconds=0)
search_index = SearchIndex(knowledge)
GRAPH_ASSETS = {".html", ".json", ".md", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".woff", ".woff2"}

class Login(BaseModel):
    token: str

class Preview(BaseModel):
    path: str = Field(min_length=1)

class AddProject(BaseModel):
    path: str
    name: str
    graphify: bool = True
    generate_graph: bool = True
    knowledge: bool = True

class Confirm(BaseModel):
    confirm: str

class Rename(BaseModel):
    name: str

class Vault(BaseModel):
    path: str

def _key():
    try: return settings.web_token.read_bytes().strip()
    except OSError: return b""

def _session_cookie():
    nonce = secrets.token_urlsafe(16)
    stamp = str(int(time.time()))
    raw = f"{stamp}.{nonce}"
    signature = hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"

def _authenticated(request: Request):
    key = _key()
    if not key:
        return False
    cookie = request.cookies.get("hub_session", "")
    parts = cookie.split(".")
    if len(parts) != 3: return False
    stamp, nonce, signature = parts
    try: valid_time = 0 <= time.time() - int(stamp) < 30 * 86400
    except ValueError: return False
    raw = f"{stamp}.{nonce}"
    return valid_time and hmac.compare_digest(signature, hmac.new(key, raw.encode(), hashlib.sha256).hexdigest())

def _csrf(cookie): return hmac.new(_key(), ("csrf:" + cookie).encode(), hashlib.sha256).hexdigest()

def _project_rows():
    try: rows = json.loads(settings.registry.read_text())
    except (OSError, ValueError): rows = []
    return rows if isinstance(rows, list) else []

def _project(project_id):
    return next((p for p in _project_rows() if p.get("id") == project_id), None)

def _graph_state(p):
    path = settings.graphs / p["id"] / "graph.json"
    state = {"ready": False, "nodes": 0, "edges": 0, "updated": None, "html": False, "report": False}
    if path.is_file() and not path.is_symlink():
        try:
            graph = json.loads(path.read_text())
            state.update(ready=True, nodes=len(graph.get("nodes", [])), edges=len(graph.get("links", graph.get("edges", []))), updated=path.stat().st_mtime, html=(path.parent / "graph.html").is_file(), report=(path.parent / "GRAPH_REPORT.md").is_file())
        except (OSError, ValueError, TypeError): pass
    return state

def _public_project(p):
    note = p.get("obsidian_note")
    summary = knowledge.project_summary(note) if note else None
    return {"id": p.get("id"), "name": p.get("name"), "repo": p.get("repo"), "branch": p.get("branch"), "sha": p.get("sha"), "registered_at": p.get("registered_at"), "obsidian_note": note, "graph": _graph_state(p), "knowledge": {"count": summary["count"], "systems": summary["systems"], "adrs": summary["adrs"], "incidents": summary["incidents"], "recent": [{"title": n.title, "path": n.path} for n in summary["recent"]]} if summary else None}

def _agent(operation, **payload):
    try: return agent_request(operation, **payload)
    except ControlError as exc: raise HTTPException(503, str(exc)) from exc

@app.middleware("http")
async def security(request: Request, call_next):
    if os.environ.get("HUB_TESTING") != "1":
        host = request.headers.get("host", "")
        valid_hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}
        if host not in valid_hosts:
            return JSONResponse({"detail": "Invalid Host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin", "")
            if origin not in {f"http://{h}" for h in valid_hosts}:
                return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
    if request.method == "OPTIONS": return JSONResponse({"detail": "CORS is disabled"}, status_code=403)
    if request.url.path.startswith("/api/") and request.url.path not in ("/api/session", "/api/login"):
        if not _authenticated(request): return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if request.method not in ("GET", "HEAD"):
            expected = _csrf(request.cookies.get("hub_session", ""))
            if not hmac.compare_digest(request.headers.get("x-hub-csrf", ""), expected):
                return JSONResponse({"detail": "CSRF token required"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _agent_auth(request: Request):
    try: expected = settings.agent_token.read_text().strip()
    except OSError: raise HTTPException(503, "Agent key unavailable")
    if not secrets.compare_digest(request.headers.get("x-agent-token", ""), expected):
        raise HTTPException(403, "Agent authentication required")

@app.get("/internal/agent/next")
def agent_next(request: Request):
    _agent_auth(request)
    return {"ticket": bridge_next()}

@app.post("/internal/agent/result/{ticket_id}")
async def agent_result(ticket_id: str, request: Request):
    _agent_auth(request)
    try: bridge_result(ticket_id, await request.json())
    except ControlError as exc: raise HTTPException(404, str(exc)) from exc
    return {"accepted": True}

@app.get("/api/session")
def session(request: Request):
    valid = _authenticated(request)
    return {"authenticated": valid, "csrf": _csrf(request.cookies.get("hub_session", "")) if valid else None}

@app.post("/api/login")
def login(body: Login):
    if not _key() or not secrets.compare_digest(body.token, _key().decode()):
        raise HTTPException(401, "Invalid access key")
    cookie = _session_cookie()
    response = JSONResponse({"authenticated": True, "csrf": _csrf(cookie)})
    response.set_cookie("hub_session", cookie, httponly=True, samesite="strict", secure=False, max_age=30*86400, path="/")
    return response

@app.post("/api/logout")
def logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("hub_session", path="/")
    return response

@app.get("/api/status")
def status():
    return {"agent": _agent("status"), "knowledge": knowledge.status(), "projects": len(_project_rows())}

@app.get("/api/projects")
def projects():
    return [_public_project(p) for p in _project_rows()]

@app.get("/api/projects/{project_id}")
def project(project_id: str):
    p = _project(project_id)
    if not p: raise HTTPException(404, "Project not found")
    return _public_project(p)

@app.post("/api/preview")
def preview(body: Preview): return _agent("preview", path=body.path)

@app.post("/api/projects")
def add_project(body: AddProject): return _agent("register_project", **body.model_dump())

@app.post("/api/projects/{project_id}/graph")
def update_graph(project_id: str):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    return _agent("graphify_update", project_id=project_id)

@app.post("/api/projects/{project_id}/knowledge")
def enable_knowledge(project_id: str):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    return _agent("knowledge_enable", project_id=project_id)

@app.post("/api/projects/{project_id}/rename")
def rename_project(project_id: str, body: Rename):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    return _agent("rename_project", project_id=project_id, name=body.name)

@app.post("/api/projects/{project_id}/unregister")
def unregister(project_id: str, body: Confirm):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    return _agent("unregister_project", project_id=project_id, confirm=body.confirm)

@app.post("/api/projects/{project_id}/open/{kind}")
def open_project(project_id: str, kind: str):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    if kind not in ("repository", "obsidian"): raise HTTPException(400, "Unknown action")
    return _agent("open_repository" if kind == "repository" else "open_native_obsidian", project_id=project_id)

@app.post("/api/vault")
def set_vault(body: Vault): return _agent("set_vault", path=body.path)

@app.get("/api/operations/{operation_id}")
def operation(operation_id: str):
    result = _agent("operation", operation_id=operation_id)
    if not result: raise HTTPException(404, "Operation not found")
    return result

@app.get("/api/operations/{operation_id}/events")
async def events(operation_id: str):
    async def stream():
        previous = -1
        for _ in range(600):
            try: result = agent_request("operation", operation_id=operation_id)
            except ControlError as exc:
                yield "event: error\ndata: " + json.dumps({"error": str(exc)}) + "\n\n"
                return
            if not result:
                yield "event: error\ndata: {\"error\":\"Operation not found\"}\n\n"
                return
            if len(result["events"]) != previous:
                previous = len(result["events"])
                yield "data: " + json.dumps(result) + "\n\n"
            if result["status"] in ("complete", "failed"): return
            import asyncio
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

@app.get("/api/knowledge")
def knowledge_home():
    knowledge.ensure_current()
    return {"status": knowledge.status(), "areas": knowledge.top_areas(), "recent": [{"path": n.path, "title": n.title, "modified": n.modified} for n in sorted(knowledge.notes.values(), key=lambda n: n.mtime_ns, reverse=True)[:20]]}

@app.get("/api/knowledge/notes")
def notes(project_id: str | None = None, area: str | None = None):
    knowledge.ensure_current()
    p = _project(project_id) if project_id else None
    if project_id and not p: raise HTTPException(404, "Project not found")
    root = str(PurePosixPath(p["obsidian_note"]).parent).rstrip("/") + "/" if p and p.get("obsidian_note") else None
    result = []
    for n in knowledge.notes.values():
        if root and not n.path.startswith(root): continue
        if area and PurePosixPath(n.path).parts[0] != area: continue
        result.append({"path": n.path, "title": n.title, "kind": n.kind, "modified": n.modified})
    return sorted(result, key=lambda n: n["path"])

@app.get("/api/knowledge/note/{note_path:path}")
def note(note_path: str):
    safe = safe_relative(note_path)
    if not safe: raise HTTPException(400, "Invalid note path")
    n = knowledge.get(safe)
    if not n: raise HTTPException(404, "Note not found")
    return {"path": n.path, "title": n.title, "kind": n.kind, "modified": n.modified, "frontmatter": n.frontmatter, "html": knowledge.render(n), "backlinks": [{"path": b.path, "title": b.title} for b in knowledge.backlinks_for(n.path)], "related": [{"path": x.path, "title": x.title} for x in knowledge.related(n)]}

@app.get("/api/knowledge/resolve")
def resolve_note(name: str):
    return [{"path": path, "title": knowledge.notes[path].title} for path in knowledge.resolve(name)]

@app.get("/api/search")
def search(q: str = "", project: str | None = None):
    rows = _project_rows()
    if project and not _project(project): raise HTTPException(404, "Project not found")
    return search_index.search(q, rows, project_id=project)

@app.post("/api/index/refresh")
def refresh_index():
    knowledge.ensure_current(force=True)
    search_index.refresh(_project_rows(), force=True)
    return {"knowledge": knowledge.status()}

@app.get("/api/projects/{project_id}/bootstrap-prompt")
def bootstrap_prompt(project_id: str):
    p = _project(project_id)
    if not p: raise HTTPException(404, "Project not found")
    return {"prompt": f"Review project {p['name']} ({p['id']}) at {p['repo']}. Its Engineering Knowledge mapping is {p.get('obsidian_note') or 'not configured'}. Use Graphify first if present, then verify every important claim against current source and configuration. Read README, AGENTS.md, CLAUDE.md, architecture docs and relevant Git history. Do not modify application source. Propose conservative notes in the vault Inbox for human review. Create retrospective ADRs only with evidence of an actual decision, and incident notes only with evidence of a real failure. Do not invent history or treat Graphify inferences as confirmed facts."}

@app.get("/api/projects/{project_id}/graph/{asset:path}")
def graph_asset(project_id: str, asset: str):
    if not _project(project_id): raise HTTPException(404, "Project not found")
    if not asset or asset.startswith("/") or ".." in PurePosixPath(asset).parts or "\\" in asset:
        raise HTTPException(400, "Invalid graph asset")
    target = settings.graphs / project_id / asset
    root = (settings.graphs / project_id).resolve()
    if target.suffix.lower() not in GRAPH_ASSETS or target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise HTTPException(404, "Graph asset not found")
    response = FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    if target.suffix.lower() in (".html", ".svg"):
        response.headers["Content-Security-Policy"] = "sandbox allow-scripts; default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval'; connect-src 'none'; form-action 'none'"
    return response

@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"): raise HTTPException(404, "API route not found")
    target = settings.static / path
    if path and target.is_file() and target.resolve().is_relative_to(settings.static.resolve()):
        return FileResponse(target)
    index = settings.static / "index.html"
    if not index.is_file(): return PlainTextResponse("Frontend has not been built", status_code=503)
    response = FileResponse(index)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'self'"
    return response
