# Render hotfix: template loading crash

Issue fixed:
- Scanner and other HTML pages could crash on newer Starlette/FastAPI builds with:
  `TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')`

Root cause:
- `Jinja2Templates.TemplateResponse(...)` was being called using the older positional style.
- On newer Starlette versions, the safer call style is explicit: `request=...`, `name=...`, `context=...`.

Fix applied:
- Updated all template responses in `app/main.py` to use explicit keyword arguments.
- Added `PYTHON_VERSION=3.12.9` to `render.yaml` to reduce platform drift on Render.

Action:
- Redeploy using this ZIP.
- No code changes are required on your side beyond replacing the app bundle.
