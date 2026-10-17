# ADR 0004: Google Calendar read-only import

- **Status:** accepted
- **Date:** 2026-10-16

## Context

Phase 3 brings real Google calendars into Agenda (spec §10), read-only. Agenda is a personal
tool (ADR 0001), so Google's OAuth app stays in **Testing** mode. Phase 4 adds writes, conflicts,
and Microsoft.

## Decision

- **OAuth client.** The owner creates a Google Cloud OAuth client of type *Desktop app* and pastes
  its ID and secret into the Calendars panel. They are stored in `app_settings`, not in the repo
  or CI. Google documents a desktop client's secret as non-confidential, and nothing relies on it.
- **Sign-in.** The flow uses the system browser, PKCE S256, and a one-time `state` held in memory.
  The redirect goes to `http://127.0.0.1:<sidecar port>/api/v1/integrations/google/callback`. That
  route is the only API route without the bearer secret; the one-time state protects it instead.
  A flow that outlives the sidecar has to start again. The requested scopes are
  `openid email calendar.readonly`.
- **Tokens.** Refresh tokens live in the OS vault through `keyring`; the database stores only a
  reference. Access tokens live only in memory.
  - With no OS vault, release builds refuse to connect accounts and say why.
  - Dev builds (`--dev`) fall back to an owner-only plaintext file in the dev data folder, because
    WSL has no vault. The UI labels it.
  - The Windows CI smoke test fails if the frozen app can't find Windows Credential Manager.
- **Sync.** Polling only; push needs a public HTTPS endpoint. Calendars are polled at launch, every
  5 minutes while running, on window focus after 5 minutes away, and on demand.
  - Each sync fetches every page, outside any transaction, using the stored `syncToken`.
  - The changes are applied in one write transaction: masters and single events first, then
    instance overrides. Matching is by `event_mappings`.
  - A `410 Gone` discards the token and runs a full sync in the same job. The full sync removes
    events Google no longer lists, so nothing duplicates and nothing goes stale.
- **Recurrence mapping.**
  - `RRULE` becomes the series rule (UNTIL converted to local wall time).
  - Each `EXDATE` becomes a cancelled exception row with no provider id of its own; these rows are
    rebuilt whenever the master changes.
  - An instance override (`recurringEventId` + `originalStartTime`) becomes an exception row
    keyed by its original start.
  - Any other recurrence line (`RDATE`, several `RRULE`s, sub-daily frequencies) makes the whole
    event *skipped and counted*, never approximated.
  - Following RFC 5545, a series' first instance is always included, even when it doesn't fall on
    the rule's pattern. Agenda still requires an on-pattern start for its own events.
- **Read-only.** Imported events carry `calendar_source_id`, and editing them returns
  `409 READ_ONLY_SOURCE`. The UI labels them with their calendar and hides editing.
- **Mapping and lifecycle.**
  - Calendars start off. Choosing a space turns one on.
  - Moving a calendar to another space moves its events.
  - Turning a calendar off deletes the imported copies and its sync token.
  - Disconnecting an account revokes the token at Google, deletes it from the vault, and either
    keeps the events as Agenda's own (editable) events or removes them.
- **Failures.**
  - `invalid_grant` marks the account `needs_reauth` and stops retrying. Testing mode does this
    every 7 days.
  - 429 and 5xx responses retry with backoff, honouring `Retry-After`.
  - Only safe error categories are stored or shown; provider responses never are.

## Consequences

- Reconnecting the same Google account (same `sub`) reactivates the existing account and its
  calendar mappings. Nothing has to be set up again.
- Nothing syncs while Agenda is closed, though it normally keeps running in the tray (ADR 0001).
  Changes made in the meantime arrive with the first poll after launch.
- The contract suite in `services/backend/tests/test_google.py` runs the real client and sync
  engine against an in-process fake of Google's endpoints. Phase 4's Microsoft adapter must pass
  the same scenarios.
