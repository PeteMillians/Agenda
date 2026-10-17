"""Events, recurring series, exceptions, and scoped edits (ADR 0002). Owner zone: America/New_York;
DST ends Sunday 2026-11-01, so Tuesday 09:00 is 13:00Z before and 14:00Z after."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import AUTH, rev

NY = ZoneInfo("America/New_York")
OCT20 = "2026-10-20T13:00:00+00:00"  # Tue 09:00 EDT
OCT27 = "2026-10-27T13:00:00+00:00"
NOV03 = "2026-11-03T14:00:00+00:00"  # Tue 09:00 EST
NOV10 = "2026-11-10T14:00:00+00:00"


@pytest.fixture
def space(spaces):
    return spaces["Personal"]["id"]


def create(client, space, **body):
    res = client.post(f"/api/v1/spaces/{space}/events", json={"title": "Event", **body}, headers=AUTH)
    assert res.status_code == 201, res.text
    return res.json()["data"]


def weekly(client, space, **extra):
    body = {"title": "Standup", "start_at": OCT20, "end_at": "2026-10-20T13:30:00+00:00"}
    return create(client, space, **{**body, "rrule": "FREQ=WEEKLY;BYDAY=TU", **extra})


def window(client, space, start="2026-10-19", end="2026-11-16"):
    res = client.get(f"/api/v1/spaces/{space}/events?from={start}&to={end}", headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def patch(client, event_id, revision, body, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items()).replace("+", "%2B")
    return client.patch(f"/api/v1/events/{event_id}?{query}", json=body, headers=rev(revision))


def delete(client, event_id, revision, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items()).replace("+", "%2B")
    return client.delete(f"/api/v1/events/{event_id}?{query}", headers=rev(revision))


def local_times(occurrences):
    return [datetime.fromisoformat(o["start_at"]).astimezone(NY).strftime("%m-%d %H:%M") for o in occurrences]


class TestSingleEvents:
    def test_create_list_get(self, client, space):
        event = create(client, space, start_at="2026-10-21T16:00:00Z", end_at="2026-10-21T17:00:00Z")
        assert event["timezone"] == "America/New_York"  # owner default
        [occurrence] = window(client, space)
        assert (occurrence["event_id"], occurrence["is_recurring"], occurrence["occurrence_start"]) == (
            event["id"],
            False,
            None,
        )
        assert client.get(f"/api/v1/events/{event['id']}", headers=AUTH).json()["data"]["title"] == "Event"

    def test_moving_start_keeps_duration(self, client, space):
        event = create(client, space, start_at="2026-10-21T16:00:00Z", end_at="2026-10-21T17:30:00Z")
        moved = patch(client, event["id"], 1, {"start_at": "2026-10-22T12:00:00Z"}).json()["data"]
        assert (moved["start_at"], moved["end_at"]) == ("2026-10-22T12:00:00Z", "2026-10-22T13:30:00Z")

    def test_all_day_multi_day_overlaps_window_start(self, client, space):
        create(client, space, all_day=True, start_date="2026-10-17", end_date="2026-10-20")
        assert len(window(client, space, "2026-10-19", "2026-10-20")) == 1
        assert window(client, space, "2026-10-20", "2026-10-21") == []  # end_date is exclusive

    def test_delete_tombstones(self, client, space):
        event = create(client, space, start_at="2026-10-21T16:00:00Z", end_at="2026-10-21T17:00:00Z")
        assert delete(client, event["id"], 1).status_code == 204
        assert window(client, space) == []
        assert client.get(f"/api/v1/events/{event['id']}", headers=AUTH).status_code == 404

    def test_scope_on_single_event_is_rejected(self, client, space):
        event = create(client, space, start_at="2026-10-21T16:00:00Z", end_at="2026-10-21T17:00:00Z")
        res = patch(client, event["id"], 1, {"title": "x"}, scope="this")
        assert res.json()["error"]["code"] == "NOT_RECURRING"

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"start_at": "2026-10-21T17:00:00Z", "end_at": "2026-10-21T16:00:00Z"}, "INVALID_TIMING"),
            ({"start_at": OCT20, "end_at": "2026-10-20T14:00:00Z", "rrule": "FREQ=HOURLY"}, "INVALID_RRULE"),
            # 2026-10-20 is a Tuesday, so BYDAY=WE does not include the start
            (
                {"start_at": OCT20, "end_at": "2026-10-20T14:00:00Z", "rrule": "FREQ=WEEKLY;BYDAY=WE"},
                "INVALID_RRULE",
            ),
        ],
    )
    def test_validation(self, client, space, body, code):
        res = client.post(f"/api/v1/spaces/{space}/events", json={"title": "x", **body}, headers=AUTH)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == code

    def test_window_limit(self, client, space):
        res = client.get(f"/api/v1/spaces/{space}/events?from=2026-01-01&to=2027-06-01", headers=AUTH)
        assert res.json()["error"]["code"] == "INVALID_WINDOW"


class TestSeriesExpansion:
    def test_weekly_keeps_wall_clock_across_dst(self, client, space):
        weekly(client, space)
        occurrences = window(client, space)
        assert local_times(occurrences) == ["10-20 09:00", "10-27 09:00", "11-03 09:00", "11-10 09:00"]
        assert [o["occurrence_start"] for o in occurrences] == [
            "2026-10-20T13:00:00.000000+00:00",
            "2026-10-27T13:00:00.000000+00:00",
            "2026-11-03T14:00:00.000000+00:00",
            "2026-11-10T14:00:00.000000+00:00",
        ]

    def test_recurring_edits_need_scope_and_occurrence(self, client, space):
        series = weekly(client, space)
        assert patch(client, series["id"], 1, {"title": "x"}).json()["error"]["code"] == "SCOPE_REQUIRED"
        res = patch(client, series["id"], 1, {"title": "x"}, scope="this")
        assert res.json()["error"]["code"] == "OCCURRENCE_REQUIRED"
        res = patch(
            client, series["id"], 1, {"title": "x"}, scope="this", occurrence_start="2026-10-21T13:00:00Z"
        )
        assert res.status_code == 404  # a Wednesday is not an occurrence


class TestScopeThis:
    def test_edit_one_occurrence(self, client, space):
        series = weekly(client, space)
        res = patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        assert res.status_code == 200, res.text
        exception = res.json()["data"]
        assert (exception["series_id"], exception["original_start"]) == (
            series["id"],
            OCT27.replace("+00:00", ".000000+00:00"),
        )
        titles = [(o["title"], o["is_exception"]) for o in window(client, space)]
        assert titles == [("Standup", False), ("Retro", True), ("Standup", False), ("Standup", False)]

    def test_any_change_bumps_the_series_revision(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        stale = patch(client, series["id"], 1, {"title": "x"}, scope="this", occurrence_start=NOV03)
        assert stale.status_code == 409
        assert {o["revision"] for o in window(client, space)} == {2}

    def test_move_one_occurrence(self, client, space):
        series = weekly(client, space)
        patch(
            client,
            series["id"],
            1,
            {"start_at": "2026-10-28T15:00:00Z"},
            scope="this",
            occurrence_start=OCT27,
        )
        assert local_times(window(client, space)) == [
            "10-20 09:00",
            "10-28 11:00",
            "11-03 09:00",
            "11-10 09:00",
        ]

    def test_occurrence_moved_into_window_from_outside(self, client, space):
        series = weekly(client, space)
        patch(
            client,
            series["id"],
            1,
            {"start_at": "2026-10-23T15:00:00Z"},
            scope="this",
            occurrence_start=NOV10,
        )
        assert local_times(window(client, space, "2026-10-22", "2026-10-24")) == ["10-23 11:00"]

    def test_editing_the_exception_row_directly_means_this(self, client, space):
        series = weekly(client, space)
        exception = patch(
            client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27
        ).json()["data"]
        res = patch(client, exception["id"], 2, {"location": "Room 4"})
        assert res.json()["data"]["location"] == "Room 4"
        assert (
            patch(client, exception["id"], 3, {"title": "x"}, scope="all").json()["error"]["code"]
            == "INVALID_SCOPE"
        )

    def test_cancel_one_occurrence(self, client, space):
        series = weekly(client, space)
        assert delete(client, series["id"], 1, scope="this", occurrence_start=OCT27).status_code == 204
        assert local_times(window(client, space)) == ["10-20 09:00", "11-03 09:00", "11-10 09:00"]


class TestScopeAll:
    def test_rename_series(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Daily sync"}, scope="all")
        assert {o["title"] for o in window(client, space)} == {"Daily sync"}

    def test_time_change_from_a_later_occurrence_moves_whole_series(self, client, space):
        series = weekly(client, space)
        res = patch(
            client, series["id"], 1, {"start_at": "2026-11-03T15:00:00Z"}, scope="all", occurrence_start=NOV03
        )
        assert res.status_code == 200, res.text
        # 10:00 EST on Nov 3 -> every occurrence at 10:00 local; the series still starts Oct 20
        assert local_times(window(client, space)) == [
            "10-20 10:00",
            "10-27 10:00",
            "11-03 10:00",
            "11-10 10:00",
        ]

    def test_unmoved_exception_follows_series_moved_one_keeps_its_time(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        patch(
            client,
            series["id"],
            2,
            {"start_at": "2026-11-10T19:00:00Z"},
            scope="this",
            occurrence_start=NOV10,
        )
        patch(client, series["id"], 3, {"start_at": "2026-10-20T14:00:00Z"}, scope="all")
        occurrences = window(client, space)
        assert local_times(occurrences) == ["10-20 10:00", "10-27 10:00", "11-03 10:00", "11-10 14:00"]
        assert occurrences[1]["title"] == "Retro"  # the exception's override survives

    def test_rule_change_drops_exceptions_off_the_new_pattern(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        patch(client, series["id"], 2, {"rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"}, scope="all")
        occurrences = window(client, space)
        assert local_times(occurrences) == ["10-20 09:00", "11-03 09:00"]
        assert not any(o["is_exception"] for o in occurrences)

    def test_removing_the_rule_makes_a_single_event(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        patch(client, series["id"], 2, {"rrule": None}, scope="all")
        [only] = window(client, space)
        assert (only["is_recurring"], local_times([only])) == (False, ["10-20 09:00"])


class TestScopeFollowing:
    def test_split_series(self, client, space):
        series = weekly(client, space)
        res = patch(
            client, series["id"], 1, {"title": "New standup"}, scope="following", occurrence_start=NOV03
        )
        tail = res.json()["data"]
        assert tail["id"] != series["id"]
        assert tail["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
        head = client.get(f"/api/v1/events/{series['id']}", headers=AUTH).json()["data"]
        assert head["rrule"] == "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261103T085959"
        titles = [o["title"] for o in window(client, space)]
        assert titles == ["Standup", "Standup", "New standup", "New standup"]

    def test_split_divides_count(self, client, space):
        series = weekly(client, space, rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=4")
        patch(client, series["id"], 1, {"title": "Later"}, scope="following", occurrence_start=NOV03)
        assert len(window(client, space, "2026-10-01", "2027-03-01")) == 4

    def test_from_first_occurrence_is_all(self, client, space):
        series = weekly(client, space)
        res = patch(client, series["id"], 1, {"title": "Renamed"}, scope="following", occurrence_start=OCT20)
        assert res.json()["data"]["id"] == series["id"]
        assert {o["title"] for o in window(client, space)} == {"Renamed"}

    def test_later_exceptions_move_to_the_new_series(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Demo day"}, scope="this", occurrence_start=NOV10)
        tail = patch(
            client, series["id"], 2, {"location": "Zoom"}, scope="following", occurrence_start=NOV03
        ).json()["data"]
        last = window(client, space)[-1]
        assert (last["event_id"], last["title"], last["is_exception"]) == (tail["id"], "Demo day", True)

    def test_time_change_for_following(self, client, space):
        series = weekly(client, space)
        patch(
            client,
            series["id"],
            1,
            {"start_at": "2026-11-03T16:00:00Z"},
            scope="following",
            occurrence_start=NOV03,
        )
        assert local_times(window(client, space)) == [
            "10-20 09:00",
            "10-27 09:00",
            "11-03 11:00",
            "11-10 11:00",
        ]

    def test_delete_following(self, client, space):
        series = weekly(client, space)
        assert delete(client, series["id"], 1, scope="following", occurrence_start=NOV03).status_code == 204
        assert local_times(window(client, space)) == ["10-20 09:00", "10-27 09:00"]


class TestDeleteAll:
    def test_delete_series_and_exceptions(self, client, space):
        series = weekly(client, space)
        patch(client, series["id"], 1, {"title": "Retro"}, scope="this", occurrence_start=OCT27)
        assert delete(client, series["id"], 2, scope="all").status_code == 204
        assert window(client, space) == []


class TestAllDaySeries:
    def test_monthly_all_day(self, client, space):
        series = create(
            client, space, all_day=True, start_date="2026-10-15", end_date="2026-10-16", rrule="FREQ=MONTHLY"
        )
        days = [o["start_date"] for o in window(client, space, "2026-10-01", "2027-01-01")]
        assert days == ["2026-10-15", "2026-11-15", "2026-12-15"]
        patch(client, series["id"], 1, {"title": "Rent (late)"}, scope="this", occurrence_start="2026-11-15")
        assert [o["title"] for o in window(client, space, "2026-11-01", "2026-12-01")] == ["Rent (late)"]
