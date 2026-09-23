from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, Field

class Settings(BaseModel):
    runtime: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_RUNTIME", "/data")))
    registry: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_PROJECTS", "/data/projects.json")))
    vault: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_VAULT", "/knowledge")))
    graphs: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_GRAPHS", "/graphs")))
    socket: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_AGENT_SOCKET", "/run/hub-agent/control.sock")))
    agent_token: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_AGENT_TOKEN", "/run/hub-agent/agent.token")))
    web_token: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_WEB_TOKEN", "/run/hub-agent/web.token")))
    host: str = Field(default_factory=lambda: os.environ.get("HUB_HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: int(os.environ.get("HUB_PORT", "8765")))
    index_db: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_INDEX_DB", "/tmp/hub-index.db")))
    static: Path = Field(default_factory=lambda: Path(os.environ.get("HUB_STATIC", "/app/static")))

settings = Settings()
