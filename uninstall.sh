#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
HUB_DATA_DIR="${HUB_DATA_DIR:-$HOME/.engineering-knowledge-hub}"
HUB_COMPOSE_PROJECT="${HUB_COMPOSE_PROJECT:-engineering-knowledge-hub}"
if [[ -f "$HUB_DATA_DIR/generated/compose.generated.yaml" ]]; then
  docker compose -p "$HUB_COMPOSE_PROJECT" -f "$SOURCE_DIR/compose.yaml" -f "$HUB_DATA_DIR/generated/compose.generated.yaml" down
fi
if [[ -f "$HUB_DATA_DIR/agent.service" ]]; then
  plist="$(cat "$HUB_DATA_DIR/agent.service")"
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  rm -f "$plist" "$HUB_DATA_DIR/agent.service"
fi
if [[ -f "$HUB_DATA_DIR/agent.pid" ]]; then
  pid="$(cat "$HUB_DATA_DIR/agent.pid")"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then kill "$pid" 2>/dev/null || true; fi
  rm -f "$HUB_DATA_DIR/agent.pid"
fi
printf 'Hub stopped. Runtime remains at %s. Repositories, Graphify output, and Markdown vault were not removed.\n' "$HUB_DATA_DIR"
