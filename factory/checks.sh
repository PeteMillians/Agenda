#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites files.
#
# Mirrors .github/workflows/ci.yml (backend, frontend, shell jobs). The Windows
# installer job only runs in CI.
set -euo pipefail

root=$(pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "== install"
pnpm install --frozen-lockfile --prefer-offline
uv sync --locked --directory services/backend

echo "== backend: ruff, pytest"
(
  cd services/backend
  uv run ruff check .
  uv run ruff format --check .
  uv run pytest -q
)

echo "== contracts match the backend"
# Same as `pnpm contracts`, but generated into a temp dir so nothing is rewritten.
uv run --directory services/backend python -c '
import json, sys
from pathlib import Path
from app.api.app import create_app
from app.config import Settings
app = create_app(Settings(data_dir=Path("unused"), token="unused", port=0))
sys.stdout.write(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
' >"$tmp/openapi.json"
diff -u packages/contracts/openapi.json "$tmp/openapi.json"
pnpm --filter @agenda/contracts exec openapi-typescript "$tmp/openapi.json" \
  --default-non-nullable false -o "$tmp/schema.d.ts" >/dev/null
diff -u packages/contracts/src/schema.d.ts "$tmp/schema.d.ts"

echo "== frontend: typecheck, test, build"
pnpm typecheck
pnpm --filter desktop test
pnpm --filter desktop build

echo "== shell: cargo fmt, clippy"
(
  cd "$root/apps/desktop/src-tauri"
  cargo fmt --check
  cargo clippy --all-targets -- -D warnings
  cargo clippy --release -- -D warnings
)

echo "== all checks passed"
