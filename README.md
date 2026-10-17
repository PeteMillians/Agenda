# Agenda

A local-first desktop dashboard for calendars, email, tasks, chores, and lists. Personal tool, Windows
target, developed on Linux.

- **Design:** [`docs/Agenda_Top.html`](docs/Agenda_Top.html) (spec) ·
  [`docs/Agenda_Architecture.html`](docs/Agenda_Architecture.html) (component and IPC map) ·
  [`docs/adr/`](docs/adr)
- **Status:** Phase 0, the shell. Tauri launches the Python sidecar, the WebView talks to it, and a
  note survives a restart.

## Layout

| Path | What | Runs in |
|---|---|---|
| `apps/desktop/src-tauri` | Rust shell: window, tray, sidecar supervisor, `get_backend_config` | Tauri main process |
| `apps/desktop/src` | React + TypeScript UI and typed API client | WebView |
| `packages/contracts` | `openapi.json` and the TypeScript types generated from it | build time |
| `services/backend` | FastAPI sidecar: security boundary, SQLite, migrations | sidecar process |
| `packaging` | Freezes the backend into `agenda-backend-<triple>` with PyInstaller | build time |

## Setup (Linux / WSL)

Prerequisites: Tauri's Linux system packages, Rust, Node LTS with pnpm, and `uv`. See the
[Tauri prerequisites](https://tauri.app/start/prerequisites/).

```bash
pnpm install
uv sync --directory services/backend
```

## Everyday commands

| Command (repo root) | Does |
|---|---|
| `pnpm dev` | `tauri dev`: Vite with hot reload, and the backend run from its venv |
| `pnpm contracts` | Regenerate `openapi.json` and the TS types after changing a route or Pydantic model |
| `pnpm typecheck` | `tsc --noEmit` for the UI |
| `uv run --directory services/backend pytest` | Backend tests, including the real stdin/stdout protocol |
| `uv run --directory services/backend ruff check .` | Python lint |
| `cargo clippy` (in `apps/desktop/src-tauri`) | Rust lint |

Dev data lives in `~/.local/share/com.millians.agenda/dev/`. Under WSLg there is no tray, so closing the
window quits the dev app (see ADR 0001).

To poke the backend on its own:

```bash
cd services/backend
python -c "print('a'*64)" | uv run python -m app.main --dev --data-dir .data
# prints AGENDA_READY port=NNNNN; then in another shell:
curl -H "Authorization: Bearer $(python -c "print('a'*64)")" -H "Host: 127.0.0.1:NNNNN" http://127.0.0.1:NNNNN/api/v1/notes
```

## Windows installer

Every push to `main`, every pull request, and manual runs ("Run workflow" on the CI page) produce an
`agenda-windows-installer` artifact from the `windows-installer` job. Download it, run the `.exe`, and
accept the SmartScreen prompt ("More info → Run anyway"); the app is unsigned.

**Phase 0 exit gate:**
1. Install and launch.
2. Add a note.
3. Quit from the tray menu.
4. Relaunch: the note is still there.
5. Close the window: Agenda stays in the tray.
6. Reboot: Agenda starts in the tray.

To build the sidecar locally for your own OS:

```bash
uv run --directory services/backend --group build python ../../packaging/build_sidecar.py
```
