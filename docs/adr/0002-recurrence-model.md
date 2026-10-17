# ADR 0002: Recurrence model for events and chores

- **Status:** accepted
- **Date:** 2026-10-16

## Context

Phase 1 builds local recurring events and repeating chores. Phase 3 imports Google and Microsoft
events, so the stored model has to match theirs: an RFC 5545 series plus per-occurrence exceptions.
Retrofitting it later would mean a data migration. The owner chose the full edit model ("this",
"this and following", "all") and two kinds of chore repetition.

## Decision

### Events: one series row plus exception rows

| Row | `rrule` | `series_id` | `original_start` | Meaning |
|---|---|---|---|---|
| Single event | null | null | null | One-off |
| Series master | set | null | null | First occurrence = DTSTART; `rrule` generates the rest |
| Exception | null | master id | key of the replaced occurrence | Overrides one occurrence; `status = cancelled` removes it |

- **Time.** Timed events store `start_at`/`end_at` as UTC ISO 8601, plus an IANA `timezone`.
  All-day events store `start_date`/`end_date`, with `end_date` exclusive (as Google does).
- **Expansion in local wall time.** Recurrence is expanded in the event's own time zone, so a 09:00
  weekly meeting stays at 09:00 across DST changes. Each local time is converted to UTC afterwards.
  Occurrences are generated per requested window and never stored.
- **Occurrence key.** `original_start` is the occurrence's *unmodified* start: UTC ISO for timed
  events, `YYYY-MM-DD` for all-day. Exceptions are unique on `(series_id, original_start)`.
- **Aggregate.** The series is one aggregate. Every change to it, or to any of its exceptions, bumps
  the master's `revision`, and `If-Match` is always checked against the master.
- **Allowed rules.** `FREQ` must be DAILY, WEEKLY, MONTHLY, or YEARLY; finer rules are refused.
  `UNTIL` is stored as local wall time without `Z`. Expansion is capped at 1,000 occurrences and a
  400-day window per request.

### Edit and delete scopes

| Scope | Edit | Delete |
|---|---|---|
| `this` | Create or update the exception for that occurrence | Exception with `status = cancelled` |
| `following` | **Split.** End the master before the occurrence (`UNTIL`, or a reduced `COUNT`). Create a new master from the occurrence with the remaining rule. Move later exceptions to the new master. Apply the edit to the new master as `all`. | End the master before the occurrence. Tombstone later exceptions. |
| `all` | Update the master. A time change shifts DTSTART by the same delta as the edited occurrence and re-keys exceptions by that delta. Exceptions that no longer match the rule are dropped. | Tombstone the master and its exceptions |

`following` on the first occurrence is treated as `all`.

### Chores: completion spawns the next task

A task's `repeat_mode` is `none`, `schedule`, or `after_completion`. Completing a repeating task
creates its successor in the same transaction. The two rows share `chain_id`, and the successor
links back through `previous_task_id`.

- **`schedule`:** RRULE anchored at the current due date. The next due date is the first occurrence
  after both the current due date and the day it was completed. A late chore therefore lands on the
  next scheduled day, without a backlog of overdue copies.
- **`after_completion`:** next due = completion date in the owner's time zone + `repeat_interval_days`.
- **Undo:** reopening a completed task deletes its successor if that successor is still open and
  unedited (revision 1).

## Consequences

- Phase 3 maps Google's `recurringEventId`/`originalStartTime` onto `series_id`/`original_start`, and
  cancelled instances onto cancelled exceptions, without schema changes.
- "Keep both" conflict resolution (Phase 4) creates a single event or an exception. Both are already
  representable.
- Moving a series' start time can drop exceptions that no longer line up with the rule. This matches
  Google Calendar's behavior for "all events" edits.
