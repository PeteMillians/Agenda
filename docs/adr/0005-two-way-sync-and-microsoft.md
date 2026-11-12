# ADR 0005: Two-way sync and the Microsoft adapter

- **Status:** accepted
- **Date:** 2026-10-23

## Context

Phase 4 (spec §17) adds editing provider events from Agenda, conflict handling, and Microsoft
Graph calendars, against the same contract suite as Google (ADR 0004). Phase 3 shipped Google
only, so Outlook accounts could not be connected at all.

## Decision

### Provider adapters

- `integrations/base.py` holds the shared pieces: safe error categories, `Grant`, `CalendarInfo`,
  the parsed `Incoming` event, and the `ProviderClient` protocol. `google/` and `microsoft/` each
  have a client (HTTP) and a mapping (pure translation). `providers.py` pairs each with the
  details the engines need (parse, serialize, insert key, instance ids).
- The sync engine (`integrations/sync.py`, formerly `google/sync.py`) and the new push engine
  (`integrations/push.py`) are provider-neutral.

### Microsoft sign-in

- Agenda is an Entra **public client**: PKCE S256, one-time state, no client secret. The owner
  registers an app under **Public client/native (mobile & desktop)** and pastes its client ID. A
  *Web* registration demands a secret (AADSTS7000218); *SPA* demands a browser origin.
- The redirect is `http://localhost:<port>/api/v1/integrations/microsoft/callback`, registered
  without the port. Entra ignores the port only for `localhost`, not `127.0.0.1`, and the sidecar
  port changes between launches. The host guard accepts `localhost:<port>` on that one public path.
  A DNS-rebinding page can't send that Host header, so the guard still protects everything else.
- Sign-in authority `common` by default, or `consumers`, `organizations`, or a tenant id to match
  how the app was registered. The callback page turns common AADSTS codes into the fix.
- Scopes: `openid email profile offline_access` plus `Calendars.Read`, or `Calendars.ReadWrite`
  when the user asks for editing. Refreshes ask for `.default offline_access`, which keeps whatever
  was consented to. Entra rotates refresh tokens; the vault entry is replaced each time.
- The account's identity is the id_token's `oid` (falling back to `sub`). Its email is the
  `email` claim or `preferred_username`. Entra can't revoke a public client's token, so
  disconnecting deletes it from the vault and tells the user where to remove Agenda's consent.

### Microsoft sync

- `calendarView/delta` per calendar, polled like Google. Graph's delta only returns concrete
  occurrences (single instances, occurrences, exceptions), never series masters. So **each Outlook
  item becomes one Agenda event without a rule.** Editing one in Agenda changes that occurrence in
  Outlook. Creating a repeating event in an Outlook calendar, or adding a rule to one, returns
  `PROVIDER_UNSUPPORTED`; whole series are edited in Outlook.
- The delta window is fixed when a round starts, so the cursor is our own JSON: `{link, until}`.
  The window runs from 60 days back to 540 days ahead. Once less than 365 days remain, the next
  poll starts a fresh window with a reconciling full sync, like an expired token. Expired delta
  tokens (410, `SyncStateNotFound`) do the same.
- Reads send `Prefer: outlook.timezone="UTC", outlook.body-content-type="text"`. All-day items
  are taken as floating dates. Windows time zone names in `originalStartTimeZone` are mapped to
  IANA for the event's label; anything unknown uses the owner's time zone. Writes send IANA zone
  names, which Graph accepts.

### Two-way writes

- A calendar stays read-only until the user switches it to `two_way`. That needs a writable role
  at the provider (`owner`/`writer`; Graph `canEdit`) and the write scope. Without the scope the
  request fails with `WRITE_ACCESS_REQUIRED`, and the UI offers "Allow editing", which signs in
  again with `write=true` (Google uses incremental consent).
- New column `events.needs_push`. An Agenda edit to a two-way calendar's event sets it on the
  changed series' rows. Every `event.*` outbox row fans out to a `calendar.push` job. The job sends
  each pending row: masters and singles first, then Google instance overrides (addressed as
  `<master id>_<original start>`). A row's flag is cleared only if its revision is still the one
  that was sent.
- Each write carries the stored ETag as `If-Match`. Inserts use a deterministic key so a retry
  can't duplicate: the Google event id is derived from Agenda's UUID (409 on a retry means the
  event is adopted, or restored if it was deleted), and Microsoft gets `transactionId`. A poll
  that sees our own insert before its response was recorded maps it instead of importing a copy.
- Retryable failures (offline, 429, 5xx) leave the rows pending, and the job backs off: that is the
  offline write queue. A successful push queues a poll right away (spec §10 cadence).
- A tombstoned override means the series itself changed; the master's write covers it, so no
  separate call is made. "This and following" ends the master with UNTIL (sent as UTC) and inserts
  the new series.

### Conflicts

- New table `sync_conflicts` (spec §10 model), with at most one open conflict per event.
- **No path overwrites silently.** A 412 on a write, or a 404/410 on a write to an event deleted at
  the provider, fetches the provider's copy and opens a conflict. A poll that brings a provider
  change (or deletion) for an event with `needs_push` or an open conflict opens or refreshes the
  conflict instead of applying it. While a conflict is open, neither the poll nor the push touches
  that event or its overrides.
- Resolution, which the UI shows as Agenda's version (as it is now) against the provider's:
  - `keep_local`: adopt the provider's current ETag and send Agenda's version. If the event was
    deleted there, it is sent again as a new event.
  - `keep_remote`: drop the unsent edits and apply the provider's version (undeleting locally if
    needed). If the event was deleted there, delete it here.
  - `keep_both`: only for single events that exist on both sides. Agenda's version becomes a new
    event titled "… (Agenda copy)" with its own mapping, and the original takes the provider's
    version. The two records are distinct, so they can't loop.
- Switching a calendar back to read-only, or turning it off, while edits are unsent or conflicts
  are open fails with `PENDING_CHANGES`, because the next sync would overwrite them.

## Consequences

- Outlook series can't be edited as a whole from Agenda, and Outlook occurrences don't show the
  repeat marker. Revisit if Graph's series-level delta leaves beta.
- Re-timing a whole Google series (scope=all with a new time) moves the master; Google may drop
  or keep earlier instance overrides by its own rules, and the next poll brings back whatever
  Google decided.
- Each Agenda event edit queues a `calendar.push` job, even for Agenda-only events (it ends
  immediately). That's cheap at personal scale.
- The contract suites are `tests/test_google.py`, `tests/test_two_way.py`, and
  `tests/test_microsoft.py`, against in-process fakes. The Microsoft fake follows Graph's
  documented behaviour. Two checks are by hand on a real account: all-day items under the UTC
  preference, and `If-Match` on `/me/events`.
