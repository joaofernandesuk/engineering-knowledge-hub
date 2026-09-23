#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
HUB_DATA_DIR="${HUB_DATA_DIR:-$HOME/.engineering-knowledge-hub}"
HUB_INSTALL_DIR="${HUB_INSTALL_DIR:-$HOME/.local/share/engineering-knowledge-hub}"
HUB_PORT="${HUB_PORT:-8765}"
HUB_COMPOSE_PROJECT="${HUB_COMPOSE_PROJECT:-engineering-knowledge-hub}"
if [[ ! "$HUB_PORT" =~ ^[0-9]+$ ]] || (( HUB_PORT < 1024 || HUB_PORT > 65535 )); then
  echo "HUB_PORT must be an unprivileged port number" >&2; exit 1
fi
command -v python3 >/dev/null || { echo "Python 3 is required for the host agent" >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit("Python 3.9 or newer is required for the host agent") if sys.version_info < (3, 9) else None'
docker compose version >/dev/null
docker info >/dev/null 2>&1 || { echo "Docker is installed but its daemon is not running" >&2; exit 1; }
SOURCE_DIR="$SOURCE_DIR" HUB_INSTALL_DIR="$HUB_INSTALL_DIR" python3 - <<'PYCOPY'
import os, shutil
from pathlib import Path
source = Path(os.environ['SOURCE_DIR']).resolve()
destination = Path(os.environ['HUB_INSTALL_DIR']).expanduser().resolve()
if source == destination or destination.is_relative_to(source):
    raise SystemExit('HUB_INSTALL_DIR must be separate from the source tree')
destination.mkdir(parents=True, exist_ok=True)
for name in ('backend', 'host', 'frontend'):
    shutil.copytree(source / name, destination / name, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('node_modules', 'dist', '__pycache__', '*.pyc', '*.tsbuildinfo'))
for name in ('Dockerfile', 'compose.yaml', '.dockerignore', 'pyproject.toml', 'requirements.lock'):
    shutil.copy2(source / name, destination / name)
PYCOPY
SOURCE_DIR="$HUB_INSTALL_DIR"
mkdir -p "$HUB_DATA_DIR/logs"
chmod 700 "$HUB_DATA_DIR" "$HUB_DATA_DIR/logs"
install_complete=0
cleanup_failed_install() {
  status=$?
  if (( status != 0 && install_complete == 0 )); then
    if [[ -f "$HUB_DATA_DIR/generated/compose.generated.yaml" ]]; then
      docker compose -p "$HUB_COMPOSE_PROJECT" -f "$SOURCE_DIR/compose.yaml" -f "$HUB_DATA_DIR/generated/compose.generated.yaml" down >/dev/null 2>&1 || true
    fi
    if [[ -f "$HUB_DATA_DIR/agent.service" ]]; then
      failed_plist="$(cat "$HUB_DATA_DIR/agent.service")"
      launchctl bootout "gui/$(id -u)" "$failed_plist" >/dev/null 2>&1 || true
      rm -f "$failed_plist" "$HUB_DATA_DIR/agent.service"
    fi
    if [[ -f "$HUB_DATA_DIR/agent.pid" ]]; then
      failed_pid="$(cat "$HUB_DATA_DIR/agent.pid")"
      if [[ "$failed_pid" =~ ^[0-9]+$ ]]; then kill "$failed_pid" 2>/dev/null || true; fi
      rm -f "$HUB_DATA_DIR/agent.pid"
    fi
    echo "Installation failed; partial container and host-agent state was stopped. Runtime retained at $HUB_DATA_DIR." >&2
  fi
  exit "$status"
}
trap cleanup_failed_install EXIT
python3 "$SOURCE_DIR/host/agent.py" init --runtime "$HUB_DATA_DIR" --source "$SOURCE_DIR" --no-reconcile
if [[ "$(uname -s)" == "Darwin" ]]; then
  label="dev.local.$HUB_COMPOSE_PROJECT"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  HUB_AGENT_LABEL="$label" HUB_AGENT_PLIST="$plist" HUB_AGENT_PYTHON="$(command -v python3)" HUB_AGENT_SOURCE="$SOURCE_DIR" HUB_AGENT_RUNTIME="$HUB_DATA_DIR" HUB_AGENT_PORT="$HUB_PORT" HUB_AGENT_PROJECT="$HUB_COMPOSE_PROJECT" python3 - <<'PYPLIST'
import os, plistlib
from pathlib import Path
p=Path(os.environ['HUB_AGENT_PLIST'])
root=os.environ['HUB_AGENT_SOURCE']; runtime=os.environ['HUB_AGENT_RUNTIME']; port=os.environ['HUB_AGENT_PORT']
value={'Label':os.environ['HUB_AGENT_LABEL'],'ProgramArguments':[os.environ['HUB_AGENT_PYTHON'],root+'/host/agent.py','serve','--runtime',runtime,'--source',root,'--bridge-url','http://127.0.0.1:'+port],'EnvironmentVariables':{'HUB_PORT':port,'HUB_COMPOSE_PROJECT':os.environ['HUB_AGENT_PROJECT'],'PATH':os.environ.get('PATH','')},'RunAtLoad':True,'KeepAlive':True,'StandardOutPath':runtime+'/logs/agent.log','StandardErrorPath':runtime+'/logs/agent.log'}
p.write_bytes(plistlib.dumps(value));p.chmod(0o600)
PYPLIST
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  printf '%s
' "$plist" > "$HUB_DATA_DIR/agent.service"
  chmod 600 "$HUB_DATA_DIR/agent.service"
else
  if [[ -f "$HUB_DATA_DIR/agent.pid" ]] && kill -0 "$(cat "$HUB_DATA_DIR/agent.pid")" 2>/dev/null; then
    echo "Host agent already running"
  else
    HUB_PORT="$HUB_PORT" HUB_COMPOSE_PROJECT="$HUB_COMPOSE_PROJECT" nohup python3 "$SOURCE_DIR/host/agent.py" serve --runtime "$HUB_DATA_DIR" --source "$SOURCE_DIR" --bridge-url "http://127.0.0.1:$HUB_PORT" > "$HUB_DATA_DIR/logs/agent.log" 2>&1 &
    echo $! > "$HUB_DATA_DIR/agent.pid"
    chmod 600 "$HUB_DATA_DIR/agent.pid"
  fi
fi
export HUB_PORT HUB_COMPOSE_PROJECT
export HUB_UID="$(id -u)" HUB_GID="$(id -g)"
docker compose -p "$HUB_COMPOSE_PROJECT" -f "$SOURCE_DIR/compose.yaml" -f "$HUB_DATA_DIR/generated/compose.generated.yaml" up -d --build
for attempt in $(seq 1 30); do
  if curl --silent --fail "http://127.0.0.1:$HUB_PORT/api/session" >/dev/null; then break; fi
  sleep 1
done
if ! curl --silent --fail "http://127.0.0.1:$HUB_PORT/api/session" >/dev/null; then
  echo "Hub did not become healthy; inspect $HUB_DATA_DIR/logs/agent.log" >&2; exit 1
fi
printf '\nHub: http://127.0.0.1:%s\n' "$HUB_PORT"
printf 'Local access key: %s\n' "$(cat "$HUB_DATA_DIR/agent/web.token")"
printf 'Keep this key private. It is also stored in %s/agent/web.token (mode 600).\n' "$HUB_DATA_DIR"
install_complete=1
trap - EXIT
