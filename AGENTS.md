# Agenda

A personal, local-first desktop dashboard for calendars, email, tasks, chores and lists. Windows
target, developed on Linux/WSL. Tauri (Rust) shell, React + TypeScript UI, FastAPI sidecar on
127.0.0.1 with SQLite.

Read first: `README.md` (layout, commands), `docs/Agenda_Top.html` (spec and phase plan),
`docs/Agenda_Architecture.html` (component and IPC map), and the ADRs in `docs/adr/` for the area
you touch.

## Fresh checkout

Prerequisites (already on the dev machine): Tauri's Linux system packages, Rust, Node LTS with
pnpm, `uv`. No env files are needed.

```bash
pnpm install --frozen-lockfile
uv sync --locked --directory services/backend
```

## Dev server on a given port

The UI needs a running backend. Start the backend by hand, then Vite pointed at it:

```bash
cd services/backend
(python3 -c "print('a'*64)"; cat) | uv run python -m app.main --dev --data-dir .data &
# prints AGENDA_READY port=NNNNN
cd ../..
VITE_AGENDA_BASE_URL=http://127.0.0.1:NNNNN VITE_AGENDA_TOKEN=$(python3 -c "print('a'*64)") \
  pnpm --filter desktop dev --port <port> --strictPort
```

The backend's dev CORS allows only `http://localhost:1420` (`app/core/security.py`), so on any
other port pages render but API calls fail. Use port 1420 when a check needs live data and nothing
else is listening on it; never change the CORS list to make a check pass.

## Validation

`factory/checks.sh` runs all of these, mirroring CI (`.github/workflows/ci.yml`):

```bash
# backend (services/backend)
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
# contracts must match the backend: after changing a route or Pydantic model, run
pnpm contracts            # and commit packages/contracts
# frontend
pnpm typecheck && pnpm --filter desktop test && pnpm --filter desktop build
# shell (apps/desktop/src-tauri)
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo clippy --release -- -D warnings
```

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard: `cd factory && python3 -m http.server`, then
open http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.
