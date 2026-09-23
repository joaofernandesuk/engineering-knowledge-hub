"""Read-only, cached view of a portable Markdown vault."""
from __future__ import annotations

import html
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

import bleach
import markdown
import yaml

WIKI_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
USEFUL_META = ("type", "project", "status", "date", "recorded", "reviewed", "tags")
ALLOWED_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "li", "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
ALLOWED_ATTRS = {"a": ["href", "title", "class"], "span": ["class"], "code": ["class"], "th": ["align"], "td": ["align"]}


def _scalar(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_scalar(v) for v in value if isinstance(v, (str, int, float, bool, date, datetime))]
    return str(value)


def _plain(markdown_text: str) -> str:
    text = WIKI_RE.sub(lambda m: m.group(2) or m.group(1), markdown_text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?(?:\[([^]]*)\])\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*~|]", " ", text)
    return " ".join(text.split())


def safe_relative(value: str) -> str | None:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ("", ".", "..") or p.startswith(".") for p in path.parts):
        return None
    if path.parts[0].casefold() == "templates" or path.suffix.casefold() != ".md":
        return None
    return path.as_posix()


@dataclass(frozen=True)
class Note:
    path: str
    title: str
    basename: str
    frontmatter: dict
    body: str
    plain: str
    outgoing: tuple[str, ...]
    mtime_ns: int
    size: int

    @property
    def modified(self) -> str:
        return datetime.fromtimestamp(self.mtime_ns / 1_000_000_000).astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def kind(self) -> str:
        raw = self.frontmatter.get("type") or Path(self.path).parent.name.rstrip("s") or "note"
        return str(raw).replace("-", " ").title()


class KnowledgeIndex:
    def __init__(self, root: Path, refresh_seconds: float = 3.0):
        self.root = root
        self.refresh_seconds = refresh_seconds
        self.notes: dict[str, Note] = {}
        self.by_basename: dict[str, list[str]] = {}
        self.backlinks: dict[str, list[str]] = {}
        self.last_indexed: datetime | None = None
        self.last_vault_mtime_ns = 0
        self._signature: tuple = ()
        self._next_check = 0.0
        self._lock = threading.RLock()
        self.rebuild()

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        files = []
        for path in self.root.rglob("*.md"):
            try:
                rel = path.relative_to(self.root)
            except ValueError:
                continue
            if path.is_symlink() or any(part.startswith(".") for part in rel.parts) or rel.parts[0].casefold() == "templates":
                continue
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(self.root.resolve()) or not path.is_file():
                    continue
            except OSError:
                continue
            files.append(path)
        return sorted(files)

    def _scan_signature(self) -> tuple:
        rows = []
        for path in self._files():
            try:
                stat = path.stat()
                rows.append((path.relative_to(self.root).as_posix(), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(rows)

    def _parse(self, path: Path) -> Note | None:
        try:
            raw = path.read_text(encoding="utf-8")
            stat = path.stat()
        except (OSError, UnicodeError):
            return None
        rel = path.relative_to(self.root).as_posix()
        meta = {}
        body = raw
        match = FRONT_RE.match(raw)
        if match:
            try:
                parsed = yaml.safe_load(match.group(1)) or {}
                if isinstance(parsed, dict):
                    meta = {k: _scalar(parsed[k]) for k in USEFUL_META if k in parsed}
            except yaml.YAMLError:
                meta = {}
            body = raw[match.end():]
        title_match = H1_RE.search(body)
        title = (title_match.group(1).strip() if title_match else path.stem).replace(" — ", " — ")
        outgoing = tuple(dict.fromkeys(m.group(1).strip() for m in WIKI_RE.finditer(body)))
        searchable = " ".join([title, rel, " ".join(f"{k} {_scalar(v)}" for k, v in meta.items()), _plain(body)]).casefold()
        return Note(rel, title, path.stem, meta, body, searchable, outgoing, stat.st_mtime_ns, stat.st_size)

    def rebuild(self) -> None:
        with self._lock:
            parsed = [self._parse(p) for p in self._files()]
            self.notes = {n.path: n for n in parsed if n is not None}
            by_name: dict[str, list[str]] = defaultdict(list)
            for note in self.notes.values():
                by_name[note.basename.casefold()].append(note.path)
            self.by_basename = {k: sorted(v) for k, v in by_name.items()}
            links: dict[str, set[str]] = defaultdict(set)
            for source in self.notes.values():
                for target in source.outgoing:
                    matches = self.resolve(target, source.path)
                    if len(matches) == 1:
                        links[matches[0]].add(source.path)
            self.backlinks = {k: sorted(v) for k, v in links.items()}
            self._signature = self._scan_signature()
            self.last_vault_mtime_ns = max((n.mtime_ns for n in self.notes.values()), default=0)
            self.last_indexed = datetime.now().astimezone()
            self._next_check = time.monotonic() + self.refresh_seconds

    def ensure_current(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now >= self._next_check:
            signature = self._scan_signature()
            if force or signature != self._signature:
                self.rebuild()
            else:
                self._next_check = now + self.refresh_seconds

    def changed(self) -> bool:
        return self._scan_signature() != self._signature

    def status(self) -> dict:
        self.ensure_current()
        return {
            "state": "changed since index" if self.changed() else "current",
            "last_indexed": self.last_indexed.strftime("%Y-%m-%d %H:%M:%S %Z") if self.last_indexed else "never",
            "last_modified": datetime.fromtimestamp(self.last_vault_mtime_ns / 1_000_000_000).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if self.last_vault_mtime_ns else "none",
            "count": len(self.notes),
        }

    def get(self, path: str) -> Note | None:
        self.ensure_current()
        safe = safe_relative(path)
        return self.notes.get(safe) if safe else None

    def resolve(self, target: str, current: str | None = None) -> list[str]:
        target = target.strip().replace("\\", "/")
        if target.casefold().endswith(".md"):
            target = target[:-3]
        candidates = []
        exact = safe_relative(target + ".md")
        if exact and exact in self.notes:
            candidates.append(exact)
        if current and "/" not in target:
            local = (PurePosixPath(current).parent / f"{target}.md").as_posix()
            if safe_relative(local) and local in self.notes and local not in candidates:
                candidates.append(local)
        for path in self.by_basename.get(PurePosixPath(target).name.casefold(), []):
            if path not in candidates:
                candidates.append(path)
        return candidates

    def search(self, query: str, project_root: str | None = None, limit: int = 50) -> list[Note]:
        self.ensure_current()
        terms = [t.casefold() for t in query.split() if t]
        if not terms:
            return []
        scored = []
        root = project_root.rstrip("/") + "/" if project_root else None
        for note in self.notes.values():
            if root and not note.path.startswith(root):
                continue
            if not all(term in note.plain for term in terms):
                continue
            title = note.title.casefold()
            score = sum(8 if term in title else 1 for term in terms)
            score += sum(2 for term in terms if term in note.basename.casefold())
            scored.append((score, note.mtime_ns, note))
        return [x[2] for x in sorted(scored, key=lambda x: (-x[0], -x[1], x[2].title))[:limit]]

    def project_notes(self, overview_path: str) -> list[Note]:
        note = self.get(overview_path)
        if not note:
            return []
        root = str(PurePosixPath(note.path).parent)
        prefix = root.rstrip("/") + "/"
        return sorted((n for n in self.notes.values() if n.path.startswith(prefix)), key=lambda n: n.path)

    def project_summary(self, overview_path: str) -> dict | None:
        notes = self.project_notes(overview_path)
        if not notes:
            return None
        categories = defaultdict(list)
        root = PurePosixPath(overview_path).parent
        for note in notes:
            rel = PurePosixPath(note.path).relative_to(root)
            category = rel.parts[0] if len(rel.parts) > 1 else ("Risks" if "risk" in note.title.casefold() else "Overview")
            categories[category].append(note)
        recent = sorted(notes, key=lambda n: n.mtime_ns, reverse=True)[:3]
        return {
            "count": len(notes),
            "adrs": len(categories.get("Decisions", [])),
            "incidents": len(categories.get("Incidents", [])),
            "systems": len(categories.get("Systems", [])),
            "categories": dict(categories),
            "recent": recent,
        }

    def top_areas(self) -> list[tuple[str, int]]:
        counts = defaultdict(int)
        for note in self.notes.values():
            counts[PurePosixPath(note.path).parts[0]] += 1
        desired = ["Projects", "Architecture", "Patterns", "Career", "Ideas", "Inbox"]
        return [(name, counts.get(name, 0)) for name in desired]

    def backlinks_for(self, path: str) -> list[Note]:
        return [self.notes[p] for p in self.backlinks.get(path, []) if p in self.notes]

    def related(self, note: Note, limit: int = 6) -> list[Note]:
        weighted: dict[str, int] = defaultdict(int)
        for target in note.outgoing:
            matches = self.resolve(target, note.path)
            if len(matches) == 1:
                weighted[matches[0]] += 10
        for path in self.backlinks.get(note.path, []):
            weighted[path] += 8
        project = str(note.frontmatter.get("project", "")).casefold()
        tags = {str(t).casefold() for t in note.frontmatter.get("tags", [])} if isinstance(note.frontmatter.get("tags"), list) else set()
        for other in self.notes.values():
            if other.path == note.path:
                continue
            if project and str(other.frontmatter.get("project", "")).casefold() == project:
                weighted[other.path] += 1
            other_tags = {str(t).casefold() for t in other.frontmatter.get("tags", [])} if isinstance(other.frontmatter.get("tags"), list) else set()
            weighted[other.path] += len(tags & other_tags) * 2
        return [self.notes[p] for p, score in sorted(weighted.items(), key=lambda x: (-x[1], self.notes[x[0]].title)) if score > 0][:limit]

    def wiki_html(self, note: Note, target: str, label: str | None) -> str:
        label = label or target
        matches = self.resolve(target, note.path)
        if len(matches) == 1:
            href = "/knowledge/note/" + quote(matches[0], safe="/")
            return f'<a class="wiki-link" href="{href}">{html.escape(label)}</a>'
        if len(matches) > 1:
            href = "/knowledge/resolve?" + urlencode({"name": target})
            return f'<a class="wiki-link ambiguous" href="{href}" title="Multiple matching notes">{html.escape(label)}</a>'
        return f'<span class="wiki-link unresolved" title="Unresolved note">{html.escape(label)}</span>'

    def render(self, note: Note) -> str:
        source = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", note.body, flags=re.IGNORECASE | re.DOTALL)
        source = WIKI_RE.sub(lambda m: self.wiki_html(note, m.group(1).strip(), m.group(2).strip() if m.group(2) else None), source)
        source = re.sub(r"^(\s*[-*+]\s+)\[ \]\s+", r"\1☐ ", source, flags=re.MULTILINE)
        source = re.sub(r"^(\s*[-*+]\s+)\[[xX]\]\s+", r"\1☑ ", source, flags=re.MULTILINE)
        rendered = markdown.markdown(source, extensions=["extra", "sane_lists"], output_format="html5")
        return bleach.clean(rendered, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, protocols={"http", "https", "mailto", "obsidian"}, strip=True)
