# Agenda

A local-first desktop dashboard for calendars, email, tasks, chores, and lists. Personal tool, Windows
target, developed on Linux.

- **Design:** [`docs/Agenda_Top.html`](docs/Agenda_Top.html) (spec) ·
  [`docs/Agenda_Architecture.html`](docs/Agenda_Architecture.html) (component and IPC map) ·
  [`docs/adr/`](docs/adr)
- **Status:** Phase 4, two-way sync and Microsoft. Dashboards of widgets over your spaces, Google
  and Outlook calendars imported into the space you choose (polled every few minutes while Agenda
  runs), and, per calendar, two-way sync with conflict handling. See
  [ADR 0004](docs/adr/0004-google-calendar-import.md) and [ADR 0005](docs/adr/0005-two-way-sync-and-microsoft.md).

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
   **Connect Google**. Your browser opens for sign-in.
5. Pick a space for each calendar you want to import.

**Phase 3 exit gate (spec §17):** import stays correct through DST, recurrence exceptions,
deletions, and forced resyncs, with no duplicates. Automated in `tests/test_google.py` against a
fake Google. By hand: connect your account, map a calendar, edit and delete a few events (including
one occurrence of a repeating event) in Google, choose **Sync now**, and compare.

### Connecting Outlook (Outlook.com, Hotmail, Microsoft 365)

One-time setup in the [Microsoft Entra admin center](https://entra.microsoft.com/) (or the Azure
portal), signed in with any Microsoft account; a free one works:

1. **App registrations** → **New registration**. Any name.
2. **Supported account types**: *Accounts in any organizational directory and personal Microsoft
   accounts*. Personal addresses (outlook.com, hotmail.com, live.com) need this.
3. **Redirect URI**: platform **Public client/native (mobile & desktop)**, value
   `http://localhost/api/v1/integrations/microsoft/callback`. Entra ignores the port of a
   `localhost` redirect, so this one entry matches whatever port Agenda gets. *Web* or *SPA*
   platforms do not work: their sign-ins end in AADSTS7000218 or AADSTS9002327.
4. Copy the **Application (client) ID** from the app's Overview page. There is no secret.
5. In Agenda, **Calendars** → Outlook: paste the ID, keep "Work, school, and personal accounts"
   (or match whatever you chose in step 2), save, and choose **Connect Outlook**.

If the browser shows an AADSTS error, Agenda's page explains the usual fix (wrong redirect URI or
platform, account type not allowed, single-tenant app).

### Two-way sync

Each calendar is read-only until you tick **Two-way** next to it. That needs write access: use
**Allow editing** on the account to sign in again with the event write scope. Edits made in Agenda
are sent with the provider's ETag, so a change made elsewhere in the meantime is never
overwritten: Agenda shows both versions under **Changed in two places** and you choose. Offline
edits wait and go out when the provider is reachable again. Outlook shows repeating series as
separate occurrences: Agenda edits one occurrence at a time, and whole series stay in Outlook.

**Phase 4 exit gate (spec §17):** both providers pass the contract suite and no tested conflict
path silently overwrites data. Automated in `tests/test_microsoft.py`, `tests/test_two_way.py`
(Google), and `tests/test_google.py`. By hand: connect Outlook, map and sync a calendar, turn on
two-way for a Google and an Outlook calendar, edit an event in Agenda and in the provider while
offline, and resolve the conflict each way.

To build the sidecar locally for your own OS:

```bash
uv run --directory services/backend --group build python ../../packaging/build_sidecar.py
```
