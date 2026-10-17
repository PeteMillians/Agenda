"""Tasks and repeating chores (ADR 0002)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import AUTH, rev

NY = ZoneInfo("America/New_York")


def today() -> date:
    return datetime.now(NY).date()


def next_weekday(after: date, weekday: int) -> date:
    """First date strictly after `after` falling on `weekday` (Mon=0)."""
    days = (weekday - after.weekday() - 1) % 7 + 1
    return after + timedelta(days=days)


@pytest.fixture
def space(spaces):
    return spaces["Family"]["id"]


def create(client, space, **body):
    res = client.post(f"/api/v1/spaces/{space}/tasks", json={"title": "Task", **body}, headers=AUTH)
    assert res.status_code == 201, res.text
    return res.json()["data"]


def complete(client, task):
    res = client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=rev(task["revision"]))
    assert res.status_code == 200, res.text
    return res.json()["data"]


def open_tasks(client, space):
    return client.get(f"/api/v1/spaces/{space}/tasks", headers=AUTH).json()["data"]


class TestBasics:
    def test_create_defaults(self, client, space):
        task = create(client, space, title="Call plumber")
        assert task["status"] == "open"
        assert task["repeat"] == {"mode": "none", "rrule": None, "interval_days": None}
        assert task["chain_id"] == task["id"]

    def test_due_date_and_due_at_are_exclusive(self, client, space):
        res = client.post(
            f"/api/v1/spaces/{space}/tasks",
            json={"title": "x", "due_date": "2026-10-20", "due_at": "2026-10-20T10:00:00Z"},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_setting_due_at_clears_due_date(self, client, space):
        task = create(client, space, due_date="2026-10-20")
        res = client.patch(
            f"/api/v1/tasks/{task['id']}", json={"due_at": "2026-10-21T14:00:00Z"}, headers=rev(1)
        )
        data = res.json()["data"]
        assert (data["due_date"], data["due_at"][:16]) == (None, "2026-10-21T14:00")

    def test_explicit_null_clears_field(self, client, space):
        task = create(client, space, due_date="2026-10-20")
        res = client.patch(f"/api/v1/tasks/{task['id']}", json={"due_date": None}, headers=rev(1))
        assert res.json()["data"]["due_date"] is None

    def test_archive(self, client, space):
        task = create(client, space)
        assert client.delete(f"/api/v1/tasks/{task['id']}", headers=rev(1)).status_code == 204
        assert open_tasks(client, space) == []
        archived = client.get(f"/api/v1/spaces/{space}/tasks?status=archived", headers=AUTH).json()["data"]
        assert [t["id"] for t in archived] == [task["id"]]
        edit = client.patch(f"/api/v1/tasks/{task['id']}", json={"title": "y"}, headers=rev(2))
        assert edit.json()["error"]["code"] == "TASK_ARCHIVED"

    def test_ordering_by_due_then_undated(self, client, space):
        create(client, space, title="undated")
        create(client, space, title="later", due_date="2026-12-01")
        create(client, space, title="sooner", due_at="2026-11-01T15:00:00Z")
        assert [t["title"] for t in open_tasks(client, space)] == ["sooner", "later", "undated"]

    def test_cursor_paging_visits_every_task_once(self, client, space):
        for n in range(7):
            create(client, space, title=f"t{n}", due_date=f"2026-11-0{n % 3 + 1}")
        seen, cursor = [], None
        while True:
            url = f"/api/v1/spaces/{space}/tasks?limit=3" + (f"&cursor={cursor}" if cursor else "")
            page = client.get(url, headers=AUTH).json()
            seen += [t["title"] for t in page["data"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert sorted(seen) == [f"t{n}" for n in range(7)]

    def test_bad_cursor(self, client, space):
        res = client.get(f"/api/v1/spaces/{space}/tasks?cursor=nonsense", headers=AUTH)
        assert res.json()["error"]["code"] == "BAD_CURSOR"


class TestAssignees:
    def test_assignee_from_another_space_is_rejected(self, client, spaces):
        other = client.post(
            f"/api/v1/spaces/{spaces['Work']['id']}/people", json={"display_name": "Boss"}, headers=AUTH
        ).json()["data"]
        res = client.post(
            f"/api/v1/spaces/{spaces['Family']['id']}/tasks",
            json={"title": "x", "assignee_person_id": other["id"]},
            headers=AUTH,
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "ASSIGNEE_NOT_IN_SPACE"

    def test_filter_and_unassign_on_person_delete(self, client, space):
        kid = client.post(
            f"/api/v1/spaces/{space}/people", json={"display_name": "Kid"}, headers=AUTH
        ).json()["data"]
        mine = create(client, space, title="dishes", assignee_person_id=kid["id"])
        create(client, space, title="unassigned")
        filtered = client.get(f"/api/v1/spaces/{space}/tasks?assignee_person_id={kid['id']}", headers=AUTH)
        assert [t["title"] for t in filtered.json()["data"]] == ["dishes"]

        client.delete(f"/api/v1/people/{kid['id']}", headers=rev(1))
        after = client.get(f"/api/v1/tasks/{mine['id']}", headers=AUTH).json()["data"]
        assert after["assignee_person_id"] is None
        assert after["revision"] == 2  # unassignment is a visible change


class TestScheduledChores:
    def test_completing_spawns_next_scheduled_occurrence(self, client, space):
        due = today()
        weekday = due.strftime("%a")[:2].upper()  # e.g. "TU"
        chore = create(
            client,
            space,
            title="Recycling",
            due_date=due.isoformat(),
            repeat={"mode": "schedule", "rrule": f"FREQ=WEEKLY;BYDAY={weekday}"},
        )
        done = complete(client, chore)
        assert done["status"] == "done"
        [successor] = open_tasks(client, space)
        assert successor["due_date"] == (due + timedelta(days=7)).isoformat()
        assert successor["chain_id"] == chore["id"]
        assert successor["previous_task_id"] == chore["id"]
        assert successor["repeat"]["rrule"] == f"FREQ=WEEKLY;BYDAY={weekday}"

    def test_late_completion_skips_missed_weeks(self, client, space):
        three_weeks_ago = today() - timedelta(days=21)
        weekday = three_weeks_ago.strftime("%a")[:2].upper()
        chore = create(
            client,
            space,
            due_date=three_weeks_ago.isoformat(),
            repeat={"mode": "schedule", "rrule": f"FREQ=WEEKLY;BYDAY={weekday}"},
        )
        complete(client, chore)
        [successor] = open_tasks(client, space)
        assert successor["due_date"] == (today() + timedelta(days=7)).isoformat()

    def test_until_ends_the_chain(self, client, space):
        due = today()
        chore = create(
            client,
            space,
            due_date=due.isoformat(),
            repeat={"mode": "schedule", "rrule": f"FREQ=DAILY;UNTIL={due.strftime('%Y%m%d')}"},
        )
        complete(client, chore)
        assert open_tasks(client, space) == []

    def test_count_is_rejected(self, client, space):
        res = client.post(
            f"/api/v1/spaces/{space}/tasks",
            json={
                "title": "x",
                "due_date": "2026-10-20",
                "repeat": {"mode": "schedule", "rrule": "FREQ=DAILY;COUNT=3"},
            },
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "INVALID_RRULE"

    def test_schedule_needs_due(self, client, space):
        res = client.post(
            f"/api/v1/spaces/{space}/tasks",
            json={"title": "x", "repeat": {"mode": "schedule", "rrule": "FREQ=DAILY"}},
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "DUE_REQUIRED"

    def test_timed_chore_keeps_local_time(self, client, space):
        # Tuesdays 18:00 New York, first due in the past so the next is relative to now
        first = datetime(2026, 1, 6, 18, tzinfo=NY)
        chore = create(
            client,
            space,
            due_at=first.isoformat(),
            repeat={"mode": "schedule", "rrule": "FREQ=WEEKLY;BYDAY=TU"},
        )
        complete(client, chore)
        [successor] = open_tasks(client, space)
        local = datetime.fromisoformat(successor["due_at"]).astimezone(NY)
        assert (local.weekday(), local.hour, local.minute) == (1, 18, 0)
        assert local.date() > today() - timedelta(days=1)


class TestAfterCompletionChores:
    def test_next_due_counts_from_completion(self, client, space):
        chore = create(
            client,
            space,
            title="Change filter",
            due_date="2026-01-01",
            repeat={"mode": "after_completion", "interval_days": 30},
        )
        complete(client, chore)
        [successor] = open_tasks(client, space)
        assert successor["due_date"] == (today() + timedelta(days=30)).isoformat()

    def test_works_without_initial_due(self, client, space):
        chore = create(client, space, repeat={"mode": "after_completion", "interval_days": 7})
        complete(client, chore)
        [successor] = open_tasks(client, space)
        assert successor["due_date"] == (today() + timedelta(days=7)).isoformat()


class TestUndo:
    def test_reopen_removes_untouched_successor(self, client, space):
        chore = create(client, space, repeat={"mode": "after_completion", "interval_days": 7})
        done = complete(client, chore)
        reopened = client.patch(
            f"/api/v1/tasks/{chore['id']}", json={"status": "open"}, headers=rev(done["revision"])
        )
        assert reopened.json()["data"]["completed_at"] is None
        assert [t["id"] for t in open_tasks(client, space)] == [chore["id"]]

    def test_reopen_keeps_edited_successor(self, client, space):
        chore = create(client, space, repeat={"mode": "after_completion", "interval_days": 7})
        done = complete(client, chore)
        [successor] = open_tasks(client, space)
        client.patch(f"/api/v1/tasks/{successor['id']}", json={"title": "edited"}, headers=rev(1))
        client.patch(f"/api/v1/tasks/{chore['id']}", json={"status": "open"}, headers=rev(done["revision"]))
        assert len(open_tasks(client, space)) == 2


class TestConcurrency:
    def test_stale_completion_is_rejected(self, client, space):
        task = create(client, space)
        client.patch(f"/api/v1/tasks/{task['id']}", json={"title": "renamed"}, headers=rev(1))
        stale = client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=rev(1))
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["current_revision"] == 2
