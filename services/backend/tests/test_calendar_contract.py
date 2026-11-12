"""Calendar contract suite (spec §16, Phase 4 exit gate): the same scenarios against Google and
Microsoft. Import through DST, recurrence exceptions, deletions and repeated syncs; two-way
edits for every recurring scope; the offline queue; and every conflict path, each checking
that neither side was silently overwritten."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.integrations.microsoft.client import MicrosoftCalendar
from app.integrations.vault import MemoryVault
from tests.calendar_harness import WINDOW, Harness, Remote, make
from tests.conftest import AUTH, complete_setup, rev, running

NY = ZoneInfo("America/New_York")


@pytest.fixture(params=["google", "microsoft"])
def harness(request, monkeypatch) -> Harness:
    monkeypatch.setattr(MicrosoftCalendar, "instance_window", lambda self: WINDOW)
    return make(request.param)


@pytest.fixture
def app(settings, harness):
    with running(settings, http_transport=harness.fake.transport(), vault=MemoryVault()) as client:
        spaces = complete_setup(client)
        res = client.put(
            f"/api/v1/integrations/{harness.provider}/client", json=harness.client_config, headers=AUTH
        )
        assert res.status_code == 200, res.text
        yield client, spaces


# --------------------------------------------------------------------------- helpers


def wait_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()["data"]
        if job["state"] in ("succeeded", "dead_letter") or (job["state"] == "queued" and job["attempts"] > 0):
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.02)


def source(client) -> dict:
    return next(
        s
        for s in client.get("/api/v1/calendar-sources", headers=AUTH).json()["data"]
        if s["name"] == "Family shared"
    )


def sync(client) -> dict:
    res = client.post(f"/api/v1/calendar-sources/{source(client)['id']}/sync", headers=AUTH)
    assert res.status_code == 202, res.text
    return wait_job(client, res.json()["data"]["job_ids"][0])


def window(client, space_id, start="2026-10-01", end="2026-12-01") -> list[dict]:
    res = client.get(f"/api/v1/spaces/{space_id}/events?from={start}&to={end}", headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def local(o: dict) -> str:
    return o["start_date"] or datetime.fromisoformat(o["start_at"]).astimezone(NY).strftime("%m-%d %H:%M")


def find(client, space_id, title: str, start: str | None = None) -> dict:
    matches = [
        o for o in window(client, space_id) if o["title"] == title and (start is None or local(o) == start)
    ]
    assert len(matches) == 1, [(o["title"], local(o)) for o in window(client, space_id)]
    return matches[0]


def at(hour: str, day: str) -> str:
    return datetime.fromisoformat(f"{day}T{hour}").replace(tzinfo=NY).isoformat()


def _scope(o: dict, scope: str | None) -> dict:
    return {"scope": scope, "occurrence_start": o["occurrence_start"]} if scope else {}


def patch(client, o: dict, body: dict, scope: str | None = None):
    return client.patch(
        f"/api/v1/events/{o['event_id']}", params=_scope(o, scope), json=body, headers=rev(o["revision"])
    )


def delete(client, o: dict, scope: str | None = None):
    return client.delete(
        f"/api/v1/events/{o['event_id']}", params=_scope(o, scope), headers=rev(o["revision"])
    )


def conflicts(client) -> list[dict]:
    return client.get("/api/v1/sync-conflicts", headers=AUTH).json()["data"]


def resolve(client, conflict: dict, resolution: str) -> dict:
    res = client.post(
        f"/api/v1/sync-conflicts/{conflict['id']}/resolve", json={"resolution": resolution}, headers=AUTH
    )
    assert res.status_code == 200, res.text
    job = sync(client)  # the queued reconcile may have run already; a manual sync settles either way
    assert job["state"] == "succeeded", job
    return res.json()["data"]


def seed(harness: Harness) -> None:
    harness.single("dentist", "Dentist", "2026-10-21T14:30", "2026-10-21T15:30")
    harness.weekly(
        "standup", "Standup", "2026-10-20T09:00", "2026-10-20T09:30", exdates=("2026-11-03T09:00",)
    )
    harness.all_day("trip", "Trip", "2026-10-24", "2026-10-27")


def import_family(client, spaces, harness: Harness, *, write: bool) -> dict:
    harness.connect(client, write=write)
    src = source(client)
    res = client.patch(
        f"/api/v1/calendar-sources/{src['id']}",
        json={"space_id": spaces["Family"]["id"], "enabled": True},
        headers=rev(src["revision"]),
    )
    assert res.status_code == 200, res.text
    assert sync(client)["state"] == "succeeded"
    return source(client)


def two_way_on(client) -> dict:
    src = source(client)
    res = client.patch(
        f"/api/v1/calendar-sources/{src['id']}", json={"sync_mode": "two_way"}, headers=rev(src["revision"])
    )
    assert res.status_code == 200, res.text
    assert sync(client)["state"] == "succeeded"
    return source(client)


@pytest.fixture
def imported(app, harness):
    client, spaces = app
    seed(harness)
    import_family(client, spaces, harness, write=False)
    return client, spaces["Family"]["id"]


@pytest.fixture
def two_way(app, harness):
    client, spaces = app
    seed(harness)
    import_family(client, spaces, harness, write=True)
    two_way_on(client)
    return client, spaces["Family"]["id"]


def settled(client) -> None:
    """Nothing waiting to be pushed and nothing contested."""
    src = source(client)
    assert (src["pending_changes"], src["open_conflicts"], src["last_error"]) == (0, 0, None), src


# --------------------------------------------------------------------------- import


class TestImport:
    def test_series_keeps_wall_clock_across_dst_and_honours_cancellations(self, imported):
        client, family = imported
        standups = [
            local(o) for o in window(client, family, "2026-10-19", "2026-11-16") if o["title"] == "Standup"
        ]
        # Nov 3 is cancelled; Nov 10 is after the DST change and still 09:00
        assert standups == ["10-20 09:00", "10-27 09:00", "11-10 09:00"]

    def test_all_day_and_single(self, imported):
        client, family = imported
        trip = find(client, family, "Trip")
        assert (trip["start_date"], trip["end_date"], trip["all_day"]) == ("2026-10-24", "2026-10-27", True)
        assert local(find(client, family, "Dentist")) == "10-21 14:30"

    def test_overridden_and_cancelled_occurrences(self, imported, harness):
        client, family = imported
        moved = harness.occurrence("standup", "2026-10-27T09:00")
        harness.rename(moved, "Standup (moved)")
        harness.move(moved, "2026-10-28T11:00", "2026-10-28T11:30")
        harness.remove(harness.occurrence("standup", "2026-11-10T09:00"))
        assert sync(client)["state"] == "succeeded"
        standups = [
            (local(o), o["title"])
            for o in window(client, family, "2026-10-19", "2026-11-16")
            if "Standup" in o["title"]
        ]
        assert standups == [("10-20 09:00", "Standup"), ("10-28 11:00", "Standup (moved)")]

    def test_changes_additions_and_deletions(self, imported, harness):
        client, family = imported
        harness.rename("dentist", "Dentist (rescheduled)")
        harness.single("party", "Party", "2026-10-30T19:00", "2026-10-30T22:00")
        harness.remove("trip")
        sync(client)
        assert sorted({o["title"] for o in window(client, family)}) == [
            "Dentist (rescheduled)",
            "Party",
            "Standup",
        ]

    def test_deleting_a_series_removes_its_occurrences(self, imported, harness):
        client, family = imported
        harness.rename(harness.occurrence("standup", "2026-10-27T09:00"), "Special standup")
        sync(client)
        harness.remove("standup")
        sync(client)
        assert not any("tandup" in o["title"] for o in window(client, family))

    def test_repeated_syncs_never_duplicate(self, imported):
        client, family = imported
        before = window(client, family)
        for _ in range(3):
            sync(client)
        assert window(client, family) == before

    def test_read_only_until_two_way_is_on(self, imported):
        client, family = imported
        dentist = find(client, family, "Dentist")
        assert dentist["read_only"]
        assert patch(client, dentist, {"title": "x"}).json()["error"]["code"] == "READ_ONLY_SOURCE"

    def test_two_way_needs_write_access(self, imported, harness):
        client, family = imported
        src = source(client)
        assert not src["can_write"]
        res = client.patch(
            f"/api/v1/calendar-sources/{src['id']}",
            json={"sync_mode": "two_way"},
            headers=rev(src["revision"]),
        )
        assert res.json()["error"]["code"] == "WRITE_ACCESS_REQUIRED"
        harness.connect(client, write=True)  # the same account, now allowed to edit
        assert source(client)["can_write"]
        two_way_on(client)
        assert not find(client, family, "Dentist")["read_only"]


# --------------------------------------------------------------------------- two-way


class TestTwoWay:
    def test_edit_a_single_event(self, two_way, harness):
        client, family = two_way
        res = patch(
            client,
            find(client, family, "Dentist"),
            {"title": "Dentist (moved)", "start_at": at("16:00", "2026-10-21")},
        )
        assert res.status_code == 200, res.text
        sync(client)
        assert harness.remote("dentist") == Remote("Dentist (moved)", "10-21 16:00", "10-21 17:00")
        settled(client)
        assert local(find(client, family, "Dentist (moved)")) == "10-21 16:00"

    def test_create_in_a_provider_calendar(self, two_way, harness):
        client, family = two_way
        res = client.post(
            f"/api/v1/spaces/{family}/events",
            json={
                "title": "Bake sale",
                "start_at": at("10:00", "2026-10-31"),
                "end_at": at("12:00", "2026-10-31"),
                "calendar_source_id": source(client)["id"],
            },
            headers=AUTH,
        )
        assert res.status_code == 201, res.text
        for _ in range(3):
            sync(client)
        [created] = harness.ids_titled("Bake sale")
        assert harness.remote(created) == Remote("Bake sale", "10-31 10:00", "10-31 12:00")
        assert len([o for o in window(client, family) if o["title"] == "Bake sale"]) == 1  # no echo duplicate
        settled(client)

    def test_delete_a_single_event(self, two_way, harness):
        client, family = two_way
        assert delete(client, find(client, family, "Dentist")).status_code == 204
        sync(client)
        assert harness.remote("dentist") is None
        assert not any(o["title"] == "Dentist" for o in window(client, family))
        settled(client)

    def test_edit_all_occurrences(self, two_way, harness):
        client, family = two_way
        first = find(client, family, "Standup", "10-20 09:00")
        assert patch(client, first, {"title": "Daily sync"}, scope="all").status_code == 200
        sync(client)
        assert harness.remote("standup").title == "Daily sync"
        titles = [
            (local(o), o["title"]) for o in window(client, family, "2026-10-19", "2026-11-16") if o["rrule"]
        ]
        assert titles == [
            ("10-20 09:00", "Daily sync"),
            ("10-27 09:00", "Daily sync"),
            ("11-10 09:00", "Daily sync"),
        ]
        settled(client)

    def test_edit_one_occurrence(self, two_way, harness):
        client, family = two_way
        second = find(client, family, "Standup", "10-27 09:00")
        res = patch(
            client,
            second,
            {
                "title": "Standup (late)",
                "start_at": at("10:00", "2026-10-27"),
                "end_at": at("10:30", "2026-10-27"),
            },
            scope="this",
        )
        assert res.status_code == 200, res.text
        sync(client)
        assert harness.remote(harness.occurrence("standup", "2026-10-27T09:00")) == Remote(
            "Standup (late)", "10-27 10:00", "10-27 10:30"
        )
        assert harness.remote("standup").title == "Standup"
        settled(client)
        assert local(find(client, family, "Standup (late)")) == "10-27 10:00"

    def test_delete_one_occurrence(self, two_way, harness):
        client, family = two_way
        assert delete(client, find(client, family, "Standup", "10-27 09:00"), scope="this").status_code == 204
        sync(client)
        assert harness.remote(harness.occurrence("standup", "2026-10-27T09:00")) is None
        assert harness.remote(harness.occurrence("standup", "2026-10-20T09:00")) is not None
        sync(client)
        standups = [
            local(o) for o in window(client, family, "2026-10-19", "2026-11-16") if o["title"] == "Standup"
        ]
        assert standups == ["10-20 09:00", "11-10 09:00"]
        settled(client)

    def test_edit_this_and_following(self, two_way, harness):
        client, family = two_way
        third = find(client, family, "Standup", "11-10 09:00")
        assert patch(client, third, {"title": "Standup v2"}, scope="following").status_code == 200
        sync(client)
        ends = harness.series_ends("standup")  # the original series now stops before Nov 10, 09:00
        assert ends is not None and str(ends) <= "2026-11-10"
        [tail] = harness.ids_titled("Standup v2")
        assert harness.remote(tail).start == "11-10 09:00"
        sync(client)
        titles = [
            (local(o), o["title"]) for o in window(client, family, "2026-10-19", "2026-11-25") if o["rrule"]
        ]
        assert titles == [
            ("10-20 09:00", "Standup"),
            ("10-27 09:00", "Standup"),
            ("11-10 09:00", "Standup v2"),
            ("11-17 09:00", "Standup v2"),
            ("11-24 09:00", "Standup v2"),
        ]
        settled(client)

    def test_offline_edits_queue_and_push_later(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        assert (
            patch(client, find(client, family, "Dentist"), {"title": "Dentist (offline)"}).status_code == 200
        )
        job = sync(client)
        assert (job["state"], job["last_error_category"]) == ("queued", "provider_unreachable")
        assert source(client)["pending_changes"] == 1
        assert harness.remote("dentist") is None or harness.remote("dentist").title == "Dentist"
        harness.offline = False
        assert sync(client)["state"] == "succeeded"
        assert harness.remote("dentist").title == "Dentist (offline)"
        settled(client)

    def test_switching_back_to_read_only_discards_unpushed_edits(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        patch(client, find(client, family, "Dentist"), {"title": "never sent"})
        harness.offline = False
        src = source(client)
        res = client.patch(
            f"/api/v1/calendar-sources/{src['id']}",
            json={"sync_mode": "read_only"},
            headers=rev(src["revision"]),
        )
        assert res.status_code == 200, res.text
        sync(client)
        assert find(client, family, "Dentist")["read_only"]
        assert harness.remote("dentist").title == "Dentist"


# --------------------------------------------------------------------------- conflicts


class TestConflicts:
    def _both_edit(self, client, family, harness) -> dict:
        harness.offline = True  # keep our edit local until the provider has changed too
        assert patch(client, find(client, family, "Dentist"), {"title": "Dentist (ours)"}).status_code == 200
        harness.rename("dentist", "Dentist (theirs)")
        harness.offline = False
        sync(client)
        [conflict] = conflicts(client)
        return conflict

    def test_both_sides_changed_neither_is_overwritten(self, two_way, harness):
        client, family = two_way
        conflict = self._both_edit(client, family, harness)
        assert (conflict["local"]["title"], conflict["remote"]["title"]) == (
            "Dentist (ours)",
            "Dentist (theirs)",
        )
        assert conflict["choices"] == ["keep_local", "keep_remote", "keep_both"]
        for _ in range(2):
            sync(client)
        assert harness.remote("dentist").title == "Dentist (theirs)"
        dentist = find(client, family, "Dentist (ours)")
        assert dentist["sync_conflict"]
        assert patch(client, dentist, {"title": "again"}).json()["error"]["code"] == "SYNC_CONFLICT"
        assert len(conflicts(client)) == 1
        assert source(client)["open_conflicts"] == 1

    def test_keep_local(self, two_way, harness):
        client, family = two_way
        resolve(client, self._both_edit(client, family, harness), "keep_local")
        assert harness.remote("dentist").title == "Dentist (ours)"
        find(client, family, "Dentist (ours)")
        settled(client)

    def test_keep_remote(self, two_way, harness):
        client, family = two_way
        resolve(client, self._both_edit(client, family, harness), "keep_remote")
        assert harness.remote("dentist").title == "Dentist (theirs)"
        find(client, family, "Dentist (theirs)")
        sync(client)
        settled(client)

    def test_keep_both_makes_two_distinct_events_and_no_loop(self, two_way, harness):
        client, family = two_way
        resolve(client, self._both_edit(client, family, harness), "keep_both")
        for _ in range(3):
            sync(client)
        assert harness.remote("dentist").title == "Dentist (theirs)"
        assert len(harness.ids_titled("Dentist (ours)")) == 1
        assert sorted(o["title"] for o in window(client, family) if "Dentist" in o["title"]) == [
            "Dentist (ours)",
            "Dentist (theirs)",
        ]
        settled(client)

    def test_the_same_change_on_both_sides_is_not_a_conflict(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        patch(client, find(client, family, "Dentist"), {"title": "Dentist (same)"})
        harness.rename("dentist", "Dentist (same)")
        harness.offline = False
        sync(client)
        assert conflicts(client) == []
        settled(client)

    def test_local_edit_against_remote_delete(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        patch(client, find(client, family, "Dentist"), {"title": "Dentist (ours)"})
        harness.remove("dentist")
        harness.offline = False
        sync(client)
        [conflict] = conflicts(client)
        assert conflict["remote"] == {"deleted": True}
        assert conflict["choices"] == ["keep_local", "keep_remote"]
        find(client, family, "Dentist (ours)")  # not removed under us
        resolve(client, conflict, "keep_local")
        [recreated] = harness.ids_titled("Dentist (ours)")
        assert harness.remote(recreated).start == "10-21 14:30"
        settled(client)

    def test_local_edit_against_remote_delete_keep_remote(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        patch(client, find(client, family, "Dentist"), {"title": "Dentist (ours)"})
        harness.remove("dentist")
        harness.offline = False
        sync(client)
        resolve(client, conflicts(client)[0], "keep_remote")
        assert not any("Dentist" in o["title"] for o in window(client, family))
        assert harness.ids_titled("Dentist (ours)") == []
        settled(client)

    def test_local_delete_against_remote_edit(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        delete(client, find(client, family, "Dentist"))
        harness.rename("dentist", "Dentist (theirs)")
        harness.offline = False
        sync(client)
        [conflict] = conflicts(client)
        assert conflict["local"] == {"deleted": True}
        assert harness.remote("dentist").title == "Dentist (theirs)"  # our delete was not applied over it
        resolve(client, conflict, "keep_remote")
        find(client, family, "Dentist (theirs)")
        settled(client)

    def test_local_delete_against_remote_edit_keep_local(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        delete(client, find(client, family, "Dentist"))
        harness.rename("dentist", "Dentist (theirs)")
        harness.offline = False
        sync(client)
        resolve(client, conflicts(client)[0], "keep_local")
        assert harness.remote("dentist") is None
        settled(client)

    def test_provider_changes_between_pull_and_push(self, two_way, harness):
        """The If-Match path: the pull saw nothing new, but the write itself is refused."""
        client, family = two_way
        raced = []

        def change_first(event_id: str) -> None:
            if event_id == "dentist" and not raced:
                raced.append(event_id)
                harness.rename("dentist", "Dentist (theirs)")

        harness.on_write(change_first)
        patch(client, find(client, family, "Dentist"), {"title": "Dentist (ours)"})
        sync(client)
        harness.on_write(None)
        assert raced
        [conflict] = conflicts(client)
        assert (conflict["local"]["title"], conflict["remote"]["title"]) == (
            "Dentist (ours)",
            "Dentist (theirs)",
        )
        assert harness.remote("dentist").title == "Dentist (theirs)"
        find(client, family, "Dentist (ours)")

    def test_one_occurrence_changed_on_both_sides(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        second = find(client, family, "Standup", "10-27 09:00")
        assert patch(client, second, {"title": "Standup (ours)"}, scope="this").status_code == 200
        harness.rename(harness.occurrence("standup", "2026-10-27T09:00"), "Standup (theirs)")
        harness.offline = False
        sync(client)
        [conflict] = conflicts(client)
        assert conflict["kind"] == "occurrence"
        assert (conflict["local"]["title"], conflict["remote"]["title"]) == (
            "Standup (ours)",
            "Standup (theirs)",
        )
        resolve(client, conflict, "keep_remote")
        sync(client)
        titles = [o["title"] for o in window(client, family, "2026-10-19", "2026-11-16") if o["rrule"]]
        assert titles == ["Standup", "Standup (theirs)", "Standup"]
        settled(client)

    def test_a_settled_conflict_stays_settled(self, two_way, harness):
        client, family = two_way
        conflict = self._both_edit(client, family, harness)
        resolve(client, conflict, "keep_remote")
        res = client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_local"},
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "CONFLICT_CLOSED"
        assert harness.remote("dentist").title == "Dentist (theirs)"

    def test_keep_both_needs_two_versions(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        patch(client, find(client, family, "Dentist"), {"title": "Dentist (ours)"})
        harness.remove("dentist")
        harness.offline = False
        sync(client)
        res = client.post(
            f"/api/v1/sync-conflicts/{conflicts(client)[0]['id']}/resolve",
            json={"resolution": "keep_both"},
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "KEEP_BOTH_UNAVAILABLE"

    def test_deleting_a_series_remotely_keeps_a_local_occurrence_edit(self, two_way, harness):
        client, family = two_way
        harness.offline = True
        second = find(client, family, "Standup", "10-27 09:00")
        patch(client, second, {"title": "Standup (ours)"}, scope="this")
        harness.remove("standup")
        harness.offline = False
        sync(client)
        [conflict] = conflicts(client)
        assert (conflict["kind"], conflict["remote"]) == ("series", {"deleted": True})
        find(client, family, "Standup (ours)")
        resolve(client, conflict, "keep_local")
        [recreated] = harness.ids_titled("Standup")
        assert harness.remote(recreated).start == "10-20 09:00"
        assert harness.remote(harness.occurrence(recreated, "2026-10-27T09:00")).title == "Standup (ours)"
        settled(client)
