# ADR 0005: Two-way calendar sync, conflicts, and Microsoft

- **Status:** accepted
- **Date:** 2026-10-19

## Context

Phase 4 (spec §10, §17) lets imported events be edited from Agenda and adds Microsoft calendars.
The exit gate: both providers pass one contract suite, and no tested conflict path silently
overwrites data. The owner uses a personal Microsoft account (outlook.com), not Microsoft 365.

## Decision

### Provider adapters

- The sync engine is provider-neutral (`app/integrations/sync.py` pulls, `push.py` pushes,
  `state.py` compares). Each provider is an adapter (`google/client.py`, `microsoft/client.py`)
  that translates its JSON to and from neutral shapes in `model.py`.
- The same scenarios run against an in-process fake of each provider
  (`tests/test_calendar_contract.py`, parametrised over both).

### What counts as a local change

- Each `event_mappings` row keeps `synced_snapshot`: canonical JSON of the content both sides last
  agreed on (title, notes, location, time zone, timing, rule, status, cancelled occurrences, and
  for an occurrence its series and original start).
- A row whose current snapshot differs has a change waiting to be pushed. A row with no mapping
  is new here. This **is** the offline queue: it is plain data, so it survives restarts and is
  retried on every sync.
- Comparing content rather than revisions means bookkeeping writes (an exception edit bumping
  its series' revision, say) never look like edits.
- `null` means no baseline yet (rows imported before 0005); such rows are treated as clean.
  Turning two-way on takes a baseline of every row.

### Sync order: pull, then push

- **Pull.** A row with a local change is never overwritten. The provider's version is compared
  with the synced snapshot:
  - Unchanged, or only a new ETag: adopt the ETag.
  - Already equal to our change: mark the row clean.
  - Otherwise: record a conflict.
  - A remote deletion of a series in which any occurrence has a local change is a conflict on the
    series.
- **Push.** Each changed row is sent with the mapping's ETag as `If-Match`. On 412 the adapter
  fetches the provider's version:
  - Only the ETag moved (content still matches the synced snapshot): retry once with the new ETag.
  - The provider already has our change: mark the row clean.
  - Otherwise: record a conflict.
  - A 404 or 410 on an update is a conflict with a deletion.
- Each successful write is recorded in its own short transaction, so an interrupted push resumes.
- Local writes reach the push straight away through the outbox: `event.*` fans out to a
  `calendar.push` job.
- A series with an open conflict is frozen. Its events can't be edited (409 `SYNC_CONFLICT`) and
  aren't pushed until the conflict is resolved.

### Recurring edits

| Agenda edit | Google | Microsoft |
|---|---|---|
| This occurrence | PATCH instance `<series>_<start>` | PATCH the occurrence found via `/instances` |
| Cancel one occurrence | `EXDATE` added to the master | DELETE that occurrence |
| This and following | master gets `UNTIL`; new series inserted; later overrides re-written on the new series | same, with `range.endDate` |
| All | PATCH master | PATCH master |

- Bodies are PATCHes of the fields Agenda owns, so attendees, reminders and colours set in the
  provider survive.

### Conflict resolution

- **Keep local:** the provider's version becomes the last-seen one. The next conditional write
  replaces exactly that version and nothing newer. When the provider deleted the event, it is
  created again.
- **Keep remote:** the row becomes the provider's version and is marked clean.
- **Keep both:** our version is copied to a new, unmapped event in the same calendar, so the next
  push creates it as a separate provider event; then the original keeps the provider's version.
  Separate ids and mappings mean no duplicate loop. It isn't offered when either side deleted the
  event.
- A resolution commits in one transaction, then queues a sync to reconcile.

### Two-way mode and scopes

- Calendars stay read-only until the owner turns on **Edit here**. That needs a writable calendar
  and the write scope, so the narrowest scope applies per stage.
  - Google: `calendar.events` added to the existing scopes, with incremental consent.
  - Microsoft: `Calendars.ReadWrite` instead of `Calendars.Read`.
  - Without the write scope, the UI re-runs sign-in asking for it.
- Turning **Edit here** off discards unpushed edits after a confirmation, and a full sync restores
  the provider's version.
- New events can be created directly in a two-way calendar mapped to the chosen space.

### Microsoft specifics

- **App registration.** The owner registers an app in Azure for *personal Microsoft accounts
  only*, with a public-client redirect URI of
  `http://localhost/api/v1/integrations/microsoft/callback`.
  - Sign-in uses the `consumers` endpoint with PKCE and no client secret.
  - Microsoft ignores the port of a localhost redirect, but only accepts the name `localhost`. So
    the Host check lets `localhost:<port>` through on that one callback path.
- **Tokens.** Refresh tokens rotate and are written back to the vault. There is no revocation
  endpoint for personal accounts: disconnecting forgets the token, and the owner can remove the
  app at account.live.com/consent/Manage.
- **No delta query.** Graph's delta query (`calendarView/delta`) returns expanded occurrences, so
  it can't rebuild a series. Each sync instead:
  - lists the calendar's single events and series masters;
  - for each master, lists `/instances` in a window from 180 days back to 400 days ahead;
  - treats an expected occurrence that is missing as cancelled, and an `exception` as an override.

  Every Microsoft sync is therefore a full reconcile. Cancellations and overrides outside the
  window keep their last-seen state.
- **Recurrence patterns.** `patternedRecurrence` converts to and from RRULE for daily, weekly,
  absolute and relative monthly, and absolute and relative yearly patterns, with end date, count,
  or no end.
  - Anything else is skipped on import.
  - It's refused on edit with 422 `UNSUPPORTED_RRULE`.
- **Time zones.** Windows time zone names are mapped to IANA with `tzlocal`'s CLDR table.

### Google specifics

- Occurrences are addressed by Google's `<series id>_<original start>` naming
  (`…_20261027T130000Z`, or `…_20261024` for all-day).
- `UNTIL` is sent in UTC, as RFC 5545 requires when DTSTART has a time zone. EXDATEs are sent in
  the event's own time zone.

## Consequences

- Phase 3's `READ_ONLY_SOURCE` now applies only to calendars without two-way mode.
- A Microsoft sync costs one request per series. That is fine at personal scale, but it is the
  first thing to revisit if Graph throttles.
- If the provider changes between our 412 refetch and the retry, the retry fails again and becomes
  a conflict. A write is never forced.
- Changing a series' start time in a two-way calendar re-writes its overrides against the new
  occurrences. The provider may keep orphaned overrides from the old times, and Agenda ignores
  occurrences its rule no longer generates.
