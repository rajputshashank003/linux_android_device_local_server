# Repository guidance for coding agents

## Project shape

MyForever is a Python HTTP file server with a single-page frontend in `ui.html`. It is designed to run on Android through Termux, as well as ordinary Python environments. Core server code uses the Python standard library; optional document and local inference features are isolated in modules such as `agent.py`, `local_ai.py`, `rag.py`, and `health.py`.

## Preserve existing behavior

- Keep the server on its established port and preserve existing routes, authentication, file operations, and deployment scripts.
- Treat `shared/` as user data. Never overwrite, delete, or commit its contents.
- Keep model weights, API keys, tunnel tokens, local settings, databases, logs, and process state out of Git.
- Local inference must remain loopback-only and must not silently fall back to a remote provider.
- Prefer focused, backward-compatible edits and reuse the existing APIs and UI patterns.
- Do not change DNS, Cloudflare Tunnel, router, or hosting configuration as part of ordinary application work.

## Frontend

- `ui.html` contains the frontend markup, styles, and scripts. Preserve mobile layouts and existing themes.
- Use existing CSS tokens and route/tab behavior. Avoid adding dependencies for small UI changes.
- Keep status and loading indicators tied to real backend state; do not fabricate progress or model activity.
- Ensure controls remain keyboard accessible and respect reduced-motion preferences.

## Backend and data

- Use the existing authentication checks for new endpoints.
- Validate identifiers and file access server-side; never accept arbitrary filesystem paths from the browser.
- Do not log prompts, file contents, credentials, authorization headers, or private keys.
- Database work must use the existing local SQLite service and migrations. Keep the database outside `shared/` and do not introduce destructive migrations.

## Verification

- Run `python -m unittest discover -s tests -q` for the full suite when Python behavior or routes change.
- Run `python -m py_compile` on modified Python files.
- For inline JavaScript edits, extract scripts from `ui.html` and run `node --check` when Node is available.
- Report browser/device checks only when actually performed. Do not claim public access or model inference was tested unless it was tested against the real service.
