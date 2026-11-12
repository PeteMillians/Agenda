# Agenda

A local-first desktop dashboard for calendars, email, tasks, chores, and lists. Personal tool, Windows
target, developed on Linux.

- **Design:** [`docs/Agenda_Top.html`](docs/Agenda_Top.html) (spec) ·
  [`docs/Agenda_Architecture.html`](docs/Agenda_Architecture.html) (component and IPC map) ·
  [`docs/adr/`](docs/adr)
- **Status:** Phase 4, two-way sync and Microsoft. Dashboards of widgets over your spaces, plus
  Google and Outlook.com calendars imported into the space you choose (polled every few minutes
  while Agenda runs). Calendars you switch to **Edit here** sync both ways, with offline edits
  queued and conflicts left for you to decide. See
  [ADR 0004](docs/adr/0004-google-calendar-import.md) and
  [ADR 0005](docs/adr/0005-two-way-sync-and-microsoft.md).

## Layout

| Path | What | Runs in |
|---|---|---|
| `apps/desktop/src-tauri` | Rust shell: window, tray, sidecar supervisor, `get_backend_config` | Tauri main process |
| `apps/desktop/src` | React + TypeScript UI: dashboards (`dashboard/`), widget registry (`widgets/`), typed API client (`api/`) | WebView |
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
| `pnpm --filter desktop test` | UI unit tests (Vitest): widget registry, keyboard layout, crash isolation |
| `uv run --directory services/backend pytest` | Backend tests, including the real stdin/stdout protocol |
| `uv run --directory services/backend ruff check .` | Python lint |
| `cargo clippy` (in `apps/desktop/src-tauri`) | Rust lint |

To work on the UI in a normal browser instead of the Tauri window, start the backend by hand
(below) and run Vite with its address:

```bash
VITE_AGENDA_BASE_URL=http://127.0.0.1:NNNNN VITE_AGENDA_TOKEN=aaaa... pnpm --filter desktop dev
```

This fallback only exists in dev builds and is compiled out of the installer.

Dev data lives in `~/.local/share/com.millians.agenda/dev/`. Under WSLg there is no tray, so closing the
window quits the dev app (see ADR 0001).

To poke the backend on its own:

```bash
cd services/backend
(python3 -c "print('a'*64)"; cat) | uv run python -m app.main --dev --data-dir .data  # cat keeps stdin open; EOF means shut down
# prints AGENDA_READY port=NNNNN; then in another shell:
curl -H "Authorization: Bearer $(python -c "print('a'*64)")" http://127.0.0.1:NNNNN/api/v1/setup/status
```

## Windows installer

Every push to `main`, every pull request, and manual runs ("Run workflow" on the CI page) produce an
`agenda-windows-installer` artifact from the `windows-installer` job. Download it, run the `.exe`, and
accept the SmartScreen prompt ("More info → Run anyway"); the app is unsigned.

### Connecting Google Calendar

One-time setup in [Google Cloud Console](https://console.cloud.google.com/):

1. Create a project and enable the **Google Calendar API**.
2. **OAuth consent screen**: choose External, keep it in **Testing**, and add your Google address
   as a test user. Testing mode means signing in again every 7 days; Agenda says when.
3. **Credentials** → Create credentials → OAuth client ID → **Desktop app**.
4. In Agenda, open **Calendars** in the sidebar, paste the client ID and secret, and choose
   **Connect Google Calendar**. Your browser opens for sign-in.
5. Pick a space for each calendar you want to import.

To edit a calendar's events from Agenda, turn on **Edit here** next to it. The first time, your
browser asks you to let Agenda edit your events.

### Connecting an Outlook.com calendar (personal Microsoft account)

One-time setup in the [Azure portal](https://portal.azure.com/):

1. **App registrations** → **New registration**. Under supported account types, choose
   **Personal Microsoft accounts only**.
2. Add a redirect URI of platform **Public client/native (mobile & desktop)**:
   `http://localhost/api/v1/integrations/microsoft/callback`.
3. Copy the **Application (client) ID** from the overview page. There is no secret.
4. In Agenda, open **Calendars**, paste the ID under **Outlook.com calendar**, and connect.

**Phase 4 exit gate (spec §17):** both providers pass the contract suite, and no tested conflict
path silently overwrites data. This is automated in `tests/test_calendar_contract.py`, which runs
the same scenarios against a fake Google and a fake Microsoft: import, every recurring edit scope,
offline edits, and each conflict path with each resolution.

To check by hand, for each provider:

1. Turn on **Edit here**.
2. Edit an event, one occurrence of a repeating event, and "this and following" in Agenda. Check
   them in the provider.
3. Change the same event in both places while Agenda is offline (or quit), then reconnect. Agenda
   should show a conflict under **Calendars**.
4. Try each choice and compare both sides.

To build the sidecar locally for your own OS:

```bash
uv run --directory services/backend --group build python ../../packaging/build_sidecar.py
```
