# Security policy

This is a single-user local developer tool. Do not expose it to a LAN or the public internet. Report suspected vulnerabilities privately to the repository maintainer; do not post exploit details in a public issue. No dedicated security contact has been established yet.

## Boundaries

- Reponary binds to `127.0.0.1` by default. Loopback alone is not authorization.
- The browser requires a local access key; the session cookie is HttpOnly and SameSite Strict. Mutations require POST, a CSRF header, and exact Origin and Host checks. No CORS is enabled.
- The native agent accepts only explicit operations. Its Unix socket and token are private to the local user. On Docker Desktop the agent initiates an authenticated localhost polling connection to Reponary because host Unix sockets cannot be bind-mounted for use by Linux containers on the tested macOS setup. The token is stored outside the source tree with mode 600.
- The web container has a read-only root, dropped capabilities, no privileged mode, read-only vault and graph mounts, no Docker socket, and no repository source mounts. SQLite FTS5 lives in tmpfs and can be rebuilt.
- The host agent validates paths, rejects traversal and symlink paths, checks duplicate/nested projects, uses structured subprocess argument lists without a shell, and only acts on registered project IDs for updates and native opening.
- Registry/config writes use a temporary file, fsync, and atomic rename. A failed Compose reconciliation restores the previous working registry and generated mount config. A small number of private backups are retained.
- The audit log records time, operation, project ID, and success/failure. It never records source, vault content, or access keys.

## Threats and mitigations

| Threat | Mitigation / residual risk |
|---|---|
| Hostile webpage sends localhost requests | Session, CSRF header, Origin and Host checks; no CORS. A compromised browser profile or same-user process remains a local risk. |
| DNS rebinding | Exact Host validation. |
| Another local user invokes host agent | Private runtime directory/socket permissions plus random agent token. Same-user malware can read user-owned files. |
| Malicious repository name or path | Validated absolute directories, no `..` or symlink components, safe random project ID, display name escaped by React. Local filesystem race remains possible if another process replaces a validated path. |
| Arbitrary command injection | Operation allow-list and subprocess argument arrays; no browser-supplied command string. |
| Cross-project access | Graph routes require registered IDs and confined asset paths. Vault notes remain globally browseable to the authenticated local user by design. |
| Malicious Markdown | Markdown HTML is sanitized with Bleach and restricted schemes; scripts and event handlers are stripped. |
| Malicious Graphify HTML/SVG | Served with a restrictive sandbox Content Security Policy, no same-origin access granted to embedded script. Graphify HTML may execute inline viewer code and load only the pinned local vis-network asset. The asset is public static JavaScript with CORS enabled solely so its existing SRI attribute works from the sandbox's opaque origin; it cannot access the authenticated app. Viewer `connect-src` remains `none`. |
| Docker escape or Docker socket exposure | No Docker socket, privileged mode, or write mounts in the web container. Host agent remains the narrow privileged boundary. |
| Local token disclosure | Access keys live in a private runtime folder, outside source control. Protect backups of that folder. |
| Untrusted dependencies | Build from declared packages; review dependency changes and licenses before release. |

No telemetry, analytics, crash upload, external CDN, or automatic update checks are present in the app. Graphify's generated HTML contains a pinned vis-network CDN reference, which Reponary substitutes with an SRI-identical local copy only when serving the graph viewer. Graphify is invoked with `--code-only` for a new graph to avoid optional semantic network calls. An externally installed Graphify CLI has its own security model and should be obtained from its official source.
