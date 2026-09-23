"""Disposable SQLite FTS5 cache. Markdown and Graphify files remain authoritative."""
from __future__ import annotations
import json
import re
import threading
from pathlib import Path
from sqlalchemy import create_engine, text
from .config import settings
from .knowledge import KnowledgeIndex

TOKEN = re.compile(r"[\w]+", re.UNICODE)

class SearchIndex:
    def __init__(self, knowledge: KnowledgeIndex):
        self.knowledge = knowledge
        self.engine = create_engine(f"sqlite:///{settings.index_db}", connect_args={"check_same_thread": False})
        self.lock = threading.RLock()
        self.signature = None
        with self.engine.begin() as db:
            db.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(kind, project_id UNINDEXED, path UNINDEXED, title, body)"))

    def _signature(self, projects):
        self.knowledge.ensure_current()
        graph_files = []
        for p in projects:
            for name in ("graph.json", "GRAPH_REPORT.md"):
                path = settings.graphs / p["id"] / name
                try:
                    root = (settings.graphs / p["id"]).resolve()
                    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                        continue
                    stat = path.stat()
                    graph_files.append((p["id"], name, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    pass
        return (tuple((n.path, n.mtime_ns, n.size) for n in self.knowledge.notes.values()), tuple(graph_files))

    def refresh(self, projects, force=False):
        with self.lock:
            sig = self._signature(projects)
            if sig == self.signature and not force:
                return
            rows = []
            for note in self.knowledge.notes.values():
                rows.append({"kind": "knowledge", "project_id": "", "path": note.path, "title": note.title, "body": note.plain})
            for p in projects:
                root = settings.graphs / p["id"]
                path = root / "graph.json"
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                    try:
                        graph = json.loads(path.read_text())
                        for node in graph.get("nodes", []):
                            if not isinstance(node, dict):
                                continue
                            title = str(node.get("label") or node.get("id") or "")[:500]
                            body = " ".join(str(node.get(key) or "") for key in ("description", "type", "source_file", "source_location"))[:4000]
                            rows.append({"kind": "code", "project_id": p["id"], "path": str(node.get("source_file") or ""), "title": title, "body": body})
                    except (OSError, ValueError, TypeError):
                        pass
                report = root / "GRAPH_REPORT.md"
                if report.is_file() and not report.is_symlink() and report.resolve().is_relative_to(root.resolve()):
                    try:
                        rows.append({"kind": "report", "project_id": p["id"], "path": "GRAPH_REPORT.md", "title": p["name"] + " architecture report", "body": report.read_text(errors="replace")[:200000]})
                    except OSError:
                        pass
            with self.engine.begin() as db:
                db.execute(text("DELETE FROM entries"))
                if rows:
                    db.execute(text("INSERT INTO entries (kind, project_id, path, title, body) VALUES (:kind, :project_id, :path, :title, :body)"), rows)
            self.signature = sig

    def search(self, query, projects, project_id=None, limit=50):
        self.refresh(projects)
        words = TOKEN.findall(query)[:8]
        if not words:
            return []
        match = " AND ".join('"' + w.replace('"','') + '"' for w in words)
        sql = "SELECT kind, project_id, path, title, snippet(entries, 4, '', '', '…', 24) AS excerpt FROM entries WHERE entries MATCH :q"
        args = {"q": match, "lim": limit}
        if project_id:
            sql += " AND (project_id = :pid OR (kind = 'knowledge' AND path LIKE :prefix))"
            p = next((x for x in projects if x["id"] == project_id), None)
            if not p:
                return []
            args.update(pid=project_id, prefix=str(Path(p.get("obsidian_note") or "").parent).rstrip("/") + "/%")
        # The UI presents code and knowledge separately. Give each group its
        # own result budget so a large graph cannot hide a matching note.
        with self.engine.connect() as db:
            results = []
            for kind_clause in ("kind = 'knowledge'", "kind != 'knowledge'"):
                rows = db.execute(text(sql + " AND " + kind_clause + " ORDER BY rank LIMIT :lim"), args)
                results.extend(dict(row._mapping) for row in rows)
            return results
