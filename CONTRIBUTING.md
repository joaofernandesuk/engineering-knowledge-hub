# Contributing

This is a 0.1.0 local-first preview. Work from a fork or local checkout and open focused pull requests when a public repository exists.

## Setup

Use Python 3.12+, Node 22+, and Docker Compose. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
cd frontend
npm ci
npm run build
```

The host agent itself uses only the Python standard library. Use `demo/` or temporary synthetic repositories for tests, screenshots, and documentation. Never commit a real vault, repository graph, registry, source file from a user project, access token, local path, or customer data.

## Security expectations

Keep the browser container read-only. Host operations must be allow-listed, path-validated, authenticated, audited, and covered by tests. Add negative tests for traversal, symlink escape, CSRF, XSS, and failed reconciliation when changing the control plane. Do not add telemetry or paid AI calls to the default path.

## Pull request checklist

- Explain the user-visible change and its security impact.
- Run Python tests and the frontend build.
- Test with synthetic data and inspect the source for private information.
- Update README, SECURITY, and change notes when behavior changes.
- Keep user-specific configuration outside the source tree.
