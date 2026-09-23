# Dependency and license audit

This source uses dependencies installed from their own package registries at build time. Python runtime packages are resolved in `requirements.lock`; npm packages are resolved in `frontend/package-lock.json`; and Docker base images are pinned by digest. Debian security updates are applied while the runtime image is built, so a build still depends on the availability and current state of the Debian package repository. The following audit is based on the declared packages and installed package metadata for the 0.1.0 preview. Transitive packages and container OS components should be rechecked for each published image.

| Dependency | License | Use | Redistributed in built image? | Concern |
|---|---|---|---|---|
| FastAPI | MIT | HTTP API | Yes | Compatible |
| Pydantic | MIT | Request/config models | Yes | Compatible |
| SQLAlchemy | MIT | SQLite FTS5 access | Yes | Compatible |
| Uvicorn | BSD-3-Clause | HTTP server | Yes | Compatible; retain notice |
| Python-Markdown | BSD-3-Clause | Markdown rendering | Yes | Compatible; retain notice |
| Bleach | Apache-2.0 | HTML sanitization | Yes | Compatible; retain Apache terms/notice |
| PyYAML | MIT | Optional frontmatter parsing | Yes | Compatible |
| React / React DOM | MIT | Browser UI | Yes, compiled bundle | Compatible |
| React Router | MIT | Browser routing | Yes, compiled bundle | Compatible |
| TanStack Query | MIT | Browser data state | Yes, compiled bundle | Compatible |
| Vite | MIT | Build tool | No, build stage only | Compatible |
| TypeScript | Apache-2.0 | Build tool | No, build stage only | Compatible |
| vis-network 9.1.6 | MIT | Graphify viewer rendering; exact SRI-pinned standalone UMD file vendored in `backend/vendor/` | Yes | [MIT notice](backend/vendor/LICENSE-MIT) retained; SHA-256 `576bb887733eb01bb52ee75b90ef46d818454de5fddb5b616fb8a298d307ca12` |
| Graphify / `graphifyy` | Apache-2.0 for current 0.9.65 package | User-installed external CLI | No | Optional, independent project; do not bundle |
| Obsidian desktop | Proprietary terms | Optional editor/protocol | No | Never redistribute or imply affiliation |
| Node 22 Alpine image | Multiple upstream licenses | Build stage | No in final image | Review image bill of materials before image publication |
| Python 3.12 slim image | Multiple upstream licenses | Runtime base | Yes if image published | Review image bill of materials before image publication |

Our own source is MIT. MIT permits use with the listed permissive dependencies; it does not relicense them. Retain their license and notice files in distribution artifacts. No Graphify or Obsidian code, logos, or private data are copied into this source tree.

Sources: installed Python wheel/npm package metadata; [Graphify package license expression](https://pypi.org/project/graphifyy/); [Obsidian terms](https://obsidian.md/terms).
