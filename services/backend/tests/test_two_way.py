"""Two-way sync contract suite for Google (spec §10, §16): provider writes with ETag preconditions,
the offline write queue, recurring edit scopes, and conflicts that never overwrite silently.
The real client, sync, and push engines run against tests/fake_google.py."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from sqlalchemy import and_, func, or_, select, update

from app.db.tables import jobs, outbox
from app.domain.clock import now_iso
from tests.conftest import AUTH, rev
from tests.test_google import app, google, imported, local, sources, sync, timed, vault, window

WRITE = "https://www.googleapis.com/auth/calendar.events"


def connect_for_write(client, google) -> dict:
    """Sign in again asking for the event write scope (incremental consent)."""
    url = client.post("/api/v1/integrations/google/connect/start?write=true", headers=AUTH).json()["data"][
        "authorization_url"
    ]
    assert WRITE in parse_qs(urlparse(url).query)["scope"][0]
    state = parse_qs(urlparse(url).query)["state"][0]
    code = google.authorize(url)
    assert client.get(f"/api/v1/integrations/google/callback?state={state}&code={code}").status_code == 200
    return client.get("/api/v1/external-accounts", headers=AUTH).json()["data"][0]


def two_way(client, name="Family shared") -> dict:
    source = sources(client)[name]
    res = client.patch(
        f"/api/v1/calendar-sources/{source['id']}",
        json={"sync_mode": "two_way"},
        headers=rev(source["revision"]),
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]


def settle(client) -> None:
    """Wait until the worker has drained the outbox and run every job that is due now."""
    deadline = time.monotonic() + 10
    while client.portal.call(_busy, client.app.state.engine):
        assert time.monotonic() < deadline, "worker never settled"
        client.app.state.worker.wake()
        time.sleep(0.05)
    time.sleep(0.05)


async def _busy(engine) -> bool:
    async with engine.connect() as conn:
        unsent = await conn.scalar(
            select(func.count()).select_from(outbox).where(outbox.c.processed_at.is_(None))
        )
        due = await conn.scalar(
            select(func.count())
            .select_from(jobs)
            .where(
                or_(
                    jobs.c.state == "leased", and_(jobs.c.state == "queued", jobs.c.available_at <= now_iso())
                )
            )
        )
    return bool(unsent or due)


def force_due(client) -> None:
    """Make jobs waiting out a backoff due now, instead of sleeping in tests."""

    async def run(engine):
        async with engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.state == "queued").values(available_at=now_iso()))

    client.portal.call(run, client.app.state.engine)
    settle(client)


def find(client, space_id, title, start="2026-10-01", end="2026-12-01") -> dict:
    return next(o for o in window(client, space_id, start, end) if o["title"] == title)


def conflicts(client) -> list[dict]:
    return client.get("/api/v1/sync-conflicts", headers=AUTH).json()["data"]


def edit(client, occurrence, body, **query) -> httpx.Response:
    params = urlencode(query)
    return client.patch(
        f"/api/v1/events/{occurrence['event_id']}?{params}", json=body, headers=rev(occurrence["revision"])
    )


@pytest.fixture
def writable(imported, google):
    client, spaces, _ = imported
    connect_for_write(client, google)
    source = two_way(client)
    return client, spaces, source


class TestSwitching:
    def test_two_way_needs_the_write_scope(self, imported):
        client, _, source = imported
        res = client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"sync_mode": "two_way"},
            headers=rev(source["revision"]),
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "WRITE_ACCESS_REQUIRED"

    def test_upgrade_keeps_the_account_and_unlocks_editing(self, imported, google):
        client, spaces, source = imported
        before = client.get("/api/v1/external-accounts", headers=AUTH).json()["data"][0]
        assert before["can_write"] is False
        after = connect_for_write(client, google)
        assert (after["id"], after["can_write"]) == (before["id"], True)
        assert find(client, spaces["Family"]["id"], "Dentist")["read_only"] is True
        two_way(client)
        assert find(client, spaces["Family"]["id"], "Dentist")["read_only"] is False

    def test_reader_calendars_stay_read_only(self, app, google):
        client, spaces = app
        google.access_role = "reader"
        connect_for_write(client, google)
        source = sources(client)["Pete"]
        res = client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"space_id": spaces["Personal"]["id"], "enabled": True, "sync_mode": "two_way"},
            headers=rev(source["revision"]),
        )
        assert res.json()["error"]["code"] == "READ_ONLY_CALENDAR"


class TestWrites:
    def test_edit_is_sent_with_if_match(self, writable, google):
        client, spaces, _ = writable
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        etag = google.calendars["family-cal"].events["dentist"]["etag"]
        assert edit(client, dentist, {"title": "Dentist (Dr. Lee)"}).status_code == 200
        settle(client)
        assert ("PATCH", "dentist", etag) in google.writes
        assert google.calendars["family-cal"].events["dentist"]["summary"] == "Dentist (Dr. Lee)"
        assert sources(client)["Family shared"]["pending_changes"] == 0
        # reading back our own write changes nothing and opens no conflict
        sync(client, sources(client)["Family shared"]["id"])
        assert find(client, spaces["Family"]["id"], "Dentist (Dr. Lee)")
        assert conflicts(client) == []

    def test_create_in_a_two_way_calendar(self, writable, google):
        client, spaces, source = writable
        res = client.post(
            f"/api/v1/spaces/{spaces['Family']['id']}/events",
            json={
                "title": "Picnic",
                "start_at": "2026-10-25T12:00:00-04:00",
                "end_at": "2026-10-25T14:00:00-04:00",
                "calendar_source_id": source["id"],
            },
            headers=AUTH,
        )
        assert res.status_code == 201, res.text
        settle(client)
        [created] = [
            e for e in google.calendars["family-cal"].events.values() if e.get("summary") == "Picnic"
        ]
        assert created["id"] == res.json()["data"]["id"].replace("-", "")  # deterministic id
        assert created["start"] == {"dateTime": "2026-10-25T12:00:00-04:00", "timeZone": "America/New_York"}
        sync(client, source["id"])
        assert [o["title"] for o in window(client, spaces["Family"]["id"])].count("Picnic") == 1

    def test_create_into_another_spaces_calendar_is_refused(self, writable):
        client, spaces, source = writable
        res = client.post(
            f"/api/v1/spaces/{spaces['Work']['id']}/events",
            json={
                "title": "x",
                "start_at": "2026-10-25T12:00:00Z",
                "end_at": "2026-10-25T13:00:00Z",
                "calendar_source_id": source["id"],
            },
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "CALENDAR_SPACE_MISMATCH"

    def test_retried_insert_does_not_duplicate(self, writable, google):
        client, spaces, source = writable
        # the first POST "succeeds" at Google but its response is lost
        google.fail_writes = [httpx.Response(503)]
        res = client.post(
            f"/api/v1/spaces/{spaces['Family']['id']}/events",
            json={
                "title": "Recital",
                "start_at": "2026-10-26T18:00:00Z",
                "end_at": "2026-10-26T19:00:00Z",
                "calendar_source_id": source["id"],
            },
            headers=AUTH,
        )
        event_id = res.json()["data"]["id"]
        google.put(
            "family-cal",
            {
                "id": event_id.replace("-", ""),
                "summary": "Recital",
                "start": {"dateTime": "2026-10-26T18:00:00Z"},
                "end": {"dateTime": "2026-10-26T19:00:00Z"},
            },
        )
        settle(client)
        sync(client, source["id"])  # sees the event before Agenda knows it was created
        force_due(client)
        assert sources(client)["Family shared"]["pending_changes"] == 0
        assert [o["title"] for o in window(client, spaces["Family"]["id"])].count("Recital") == 1
        recitals = [
            e for e in google.calendars["family-cal"].events.values() if e.get("summary") == "Recital"
        ]
        assert len(recitals) == 1

    def test_delete_is_sent(self, writable, google):
        client, spaces, _ = writable
        trip = find(client, spaces["Family"]["id"], "Trip")
        assert (
            client.delete(f"/api/v1/events/{trip['event_id']}", headers=rev(trip["revision"])).status_code
            == 204
        )
        settle(client)
        assert google.calendars["family-cal"].events["trip"]["status"] == "cancelled"
        assert not any(o["title"] == "Trip" for o in window(client, spaces["Family"]["id"]))

    def test_offline_edits_queue_and_go_out_later(self, writable, google):
        client, spaces, source = writable
        google.fail_writes = [httpx.Response(503, headers={"Retry-After": "0"})]
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        edit(client, dentist, {"location": "Main St"})
        settle(client)
        assert sources(client)["Family shared"]["pending_changes"] == 1  # kept, waiting to retry
        # meanwhile a poll must not overwrite the unsent edit
        sync(client, source["id"])
        assert find(client, spaces["Family"]["id"], "Dentist")["location"] == "Main St"
        force_due(client)
        assert google.calendars["family-cal"].events["dentist"]["location"] == "Main St"
        assert sources(client)["Family shared"]["pending_changes"] == 0


class TestRecurring:
    def test_this_occurrence_patches_the_instance(self, writable, google):
        client, spaces, _ = writable
        standup = [o for o in window(client, spaces["Family"]["id"]) if o["title"] == "Standup"][1]  # Oct 27
        res = edit(
            client,
            standup,
            {"title": "Standup (demo)"},
            scope="this",
            occurrence_start=standup["occurrence_start"],
        )
        assert res.status_code == 200, res.text
        settle(client)
        instance = google.calendars["family-cal"].events["standup_20261027T130000Z"]
        assert instance["summary"] == "Standup (demo)"
        assert instance["recurringEventId"] == "standup"

    def test_cancel_one_occurrence(self, writable, google):
        client, spaces, _ = writable
        standup = [o for o in window(client, spaces["Family"]["id"]) if o["title"] == "Standup"][1]
        res = client.delete(
            f"/api/v1/events/{standup['event_id']}?"
            + urlencode({"scope": "this", "occurrence_start": standup["occurrence_start"]}),
            headers=rev(standup["revision"]),
        )
        assert res.status_code == 204
        settle(client)
        assert google.calendars["family-cal"].events["standup_20261027T130000Z"]["status"] == "cancelled"

    def test_all_occurrences_sends_the_rule_with_utc_until(self, writable, google):
        client, spaces, _ = writable
        standup = find(client, spaces["Family"]["id"], "Standup")
        res = edit(client, standup, {"rrule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T140000Z"}, scope="all")
        assert res.status_code == 200, res.text
        settle(client)
        master = google.calendars["family-cal"].events["standup"]
        assert master["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T140000Z"]

    def test_this_and_following_splits_at_google_too(self, writable, google):
        client, spaces, source = writable
        standups = [o for o in window(client, spaces["Family"]["id"]) if o["title"] == "Standup"]
        later = standups[2]  # Nov 10
        res = edit(
            client,
            later,
            {"title": "Late standup"},
            scope="following",
            occurrence_start=later["occurrence_start"],
        )
        assert res.status_code == 200, res.text
        settle(client)
        events = google.calendars["family-cal"].events
        assert "UNTIL=" in events["standup"]["recurrence"][0]
        [tail] = [e for e in events.values() if e.get("summary") == "Late standup"]
        assert tail["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
        sync(client, source["id"])
        titles = [
            (local(o), o["title"])
            for o in window(client, spaces["Family"]["id"], "2026-10-19", "2026-11-25")
            if "tandup" in o["title"]
        ]
        assert titles == [
            ("10-20 09:00", "Standup"),
            ("10-27 09:00", "Standup"),
            ("11-10 09:00", "Late standup"),
            ("11-17 09:00", "Late standup"),
            ("11-24 09:00", "Late standup"),
        ]
        assert conflicts(client) == []


class TestConflicts:
    def _both_change(self, client, google, spaces):
        """Agenda edits Dentist while offline; meanwhile it changes at Google."""
        google.fail_writes = [httpx.Response(503, headers={"Retry-After": "0"})]
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        edit(client, dentist, {"title": "Dentist (Agenda)"})
        settle(client)
        google.put(
            "family-cal",
            timed("dentist", "Dentist (Google)", "2026-10-21T16:00:00-04:00", "2026-10-21T17:00:00-04:00"),
        )

    def test_push_with_a_stale_etag_opens_a_conflict(self, writable, google):
        client, spaces, _ = writable
        self._both_change(client, google, spaces)
        force_due(client)
        assert google.writes[-1][0] == "PATCH" and google.writes[-1][2] is not None  # sent with If-Match
        assert google.calendars["family-cal"].events["dentist"]["summary"] == "Dentist (Google)"  # untouched
        [conflict] = conflicts(client)
        assert conflict["local"]["title"] == "Dentist (Agenda)"
        assert conflict["remote"]["title"] == "Dentist (Google)"
        assert conflict["can_keep_both"] is True
        assert find(client, spaces["Family"]["id"], "Dentist (Agenda)")  # Agenda's copy untouched too

    def test_poll_before_push_opens_a_conflict_instead_of_overwriting(self, writable, google):
        client, spaces, source = writable
        self._both_change(client, google, spaces)
        sync(client, source["id"])
        assert find(client, spaces["Family"]["id"], "Dentist (Agenda)")
        [conflict] = conflicts(client)
        assert conflict["remote"]["title"] == "Dentist (Google)"
        # and the queued push now holds back instead of overwriting Google
        force_due(client)
        assert google.calendars["family-cal"].events["dentist"]["summary"] == "Dentist (Google)"

    def test_keep_local(self, writable, google):
        client, spaces, _ = writable
        self._both_change(client, google, spaces)
        force_due(client)
        [conflict] = conflicts(client)
        res = client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_local"},
            headers=AUTH,
        )
        assert res.status_code == 204
        settle(client)
        assert google.calendars["family-cal"].events["dentist"]["summary"] == "Dentist (Agenda)"
        assert conflicts(client) == []

    def test_keep_remote(self, writable, google):
        client, spaces, _ = writable
        self._both_change(client, google, spaces)
        force_due(client)
        [conflict] = conflicts(client)
        client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_remote"},
            headers=AUTH,
        )
        settle(client)
        dentist = find(client, spaces["Family"]["id"], "Dentist (Google)")
        assert local(dentist) == "10-21 16:00"
        assert sources(client)["Family shared"]["pending_changes"] == 0

    def test_keep_both_makes_two_distinct_events_without_a_loop(self, writable, google):
        client, spaces, source = writable
        self._both_change(client, google, spaces)
        force_due(client)
        [conflict] = conflicts(client)
        client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve", json={"resolution": "keep_both"}, headers=AUTH
        )
        settle(client)
        for _ in range(2):
            sync(client, source["id"])
            settle(client)
        titles = sorted(o["title"] for o in window(client, spaces["Family"]["id"]) if "Dentist" in o["title"])
        assert titles == ["Dentist (Agenda) (Agenda copy)", "Dentist (Google)"]
        at_google = sorted(
            e["summary"]
            for e in google.calendars["family-cal"].events.values()
            if "Dentist" in (e.get("summary") or "")
        )
        assert at_google == titles
        assert conflicts(client) == []

    def test_deleted_at_google_while_edited_here(self, writable, google):
        client, spaces, source = writable
        google.fail_writes = [httpx.Response(503, headers={"Retry-After": "0"})]
        trip = find(client, spaces["Family"]["id"], "Trip")
        edit(client, trip, {"title": "Trip (packed)"})
        settle(client)
        google.delete("family-cal", "trip")
        sync(client, source["id"])
        assert find(client, spaces["Family"]["id"], "Trip (packed)")  # not silently removed
        [conflict] = conflicts(client)
        assert conflict["remote"] is None and conflict["can_keep_both"] is False
        client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_local"},
            headers=AUTH,
        )
        settle(client)
        # sent again as a new Google event, and read back without duplicating
        [again] = [
            e for e in google.calendars["family-cal"].events.values() if e.get("summary") == "Trip (packed)"
        ]
        assert again["status"] == "confirmed"
        sync(client, source["id"])
        assert [o["title"] for o in window(client, spaces["Family"]["id"])].count("Trip (packed)") == 1

    def test_deleted_here_while_changed_at_google(self, writable, google):
        client, spaces, source = writable
        google.fail_writes = [httpx.Response(503, headers={"Retry-After": "0"})]
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        client.delete(f"/api/v1/events/{dentist['event_id']}", headers=rev(dentist["revision"]))
        settle(client)
        google.put(
            "family-cal",
            timed("dentist", "Dentist (moved)", "2026-10-22T10:00:00-04:00", "2026-10-22T11:00:00-04:00"),
        )
        force_due(client)
        assert google.calendars["family-cal"].events["dentist"]["status"] == "confirmed"  # not deleted
        [conflict] = conflicts(client)
        assert conflict["local"]["deleted"] is True
        client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_remote"},
            headers=AUTH,
        )
        settle(client)
        assert find(client, spaces["Family"]["id"], "Dentist (moved)")

    def test_switching_back_to_read_only_with_unsent_edits_is_refused(self, writable, google):
        client, spaces, _ = writable
        google.fail_writes = [httpx.Response(503, headers={"Retry-After": "600"})]
        edit(client, find(client, spaces["Family"]["id"], "Dentist"), {"title": "x"})
        settle(client)
        source = sources(client)["Family shared"]
        res = client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"sync_mode": "read_only"},
            headers=rev(source["revision"]),
        )
        assert res.json()["error"]["code"] == "PENDING_CHANGES"
