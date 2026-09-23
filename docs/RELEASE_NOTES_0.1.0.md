# Reponary v0.1.0 — early preview

Reponary is a local-first, single-user developer workspace that puts current code structure alongside the engineering knowledge that explains why a project became that way. This is the first public preview, not a production or multi-user release.

## What is included

- Add multiple Git repositories or source folders through a validated browser flow; inspect project status and safely unregister or re-add them without deleting source, graphs, or notes.
- Generate and update code graphs with an optional, independently installed Graphify CLI. Reponary invokes new graphs in local code-only mode and serves the viewer with a pinned local script; Graphify is not bundled.
- Browse Markdown engineering knowledge, including decisions, incidents, wiki links, backlinks, and project context. Any Markdown directory works; Obsidian is an optional editor, not a requirement or bundled component.
- Search code graph content and Markdown notes together using a disposable local SQLite FTS5 index.
- Prepare a prompt for an external coding agent to propose knowledge for human review. Reponary does not call an AI provider or require an AI key.

## Local-first and security model

Reponary listens on localhost and requires a generated local access key. A small native host agent handles validated, allow-listed project operations. The web container has a read-only root, no Docker socket, and read-only graph and Markdown mounts. Repositories, graphs, and notes stay in their own folders. No account, telemetry, analytics, cloud database, external CDN, or automatic update check is included. Do not expose this preview to a LAN or the internet; see [SECURITY.md](https://github.com/joaofernandesuk/reponary/blob/v0.1.0/SECURITY.md).

## Known limitations

- macOS and Linux are the intended hosts; Windows has not been tested. Linux service persistence is less mature than the macOS LaunchAgent setup.
- Onboarding requires an absolute path rather than a native folder picker. Markdown editing happens in an external editor, such as Obsidian.
- This is single-user and local-only; remote or multi-user deployment is unsupported.
- Graphify is optional and independently versioned, so CLI/output compatibility can vary. In the tested code-only flow, Graphify 0.9.65 produced a usable graph and viewer but no separate `GRAPH_REPORT.md`.
- The re-add preview looks for an existing Overview note under the source folder's name. If a project used a different display name, choose that same name on re-add to preserve the note association.
- Knowledge scaffolding is intentionally conservative and contains placeholders; it does not invent architecture history. Search rebuilds its disposable index from mounted files.
- There is no prior public release to upgrade from. Private prototype installations need a separately tested migration and should not be overwritten in place.

## Install

Follow the [installation instructions in the public README](https://github.com/joaofernandesuk/reponary/blob/v0.1.0/README.md#install). Docker Desktop/Compose, Git, and Python are required; Graphify and Obsidian are optional. The installer prints the local access key and URL. No Docker image, PyPI package, or npm package is published for this release.
