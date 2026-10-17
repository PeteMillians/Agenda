"""Google Calendar contract suite (spec §16): OAuth, import, incremental sync, 410 resync,
recurrence, deletions, errors. The real client and sync engine run against tests/fake_google.py."""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.integrations.vault import MemoryVault
from tests.conftest import AUTH, complete_setup, rev, running
from tests.fake_google import FakeGoogle

NY = ZoneInfo("America/New_York")
CLIENT = {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-test"}


@pytest.fixture
def google():
    fake = FakeGoogle()
    fake.add_calendar("primary-cal", "Pete", primary=True)
    fake.add_calendar("family-cal", "Family shared")
    return fake


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def app(settings, google, vault):
    with running(settings, http_transport=google.transport(), vault=vault) as client:
        spaces = complete_setup(client)
        client.put("/api/v1/integrations/google/client", json=CLIENT, headers=AUTH)
        yield client, spaces


def connect(client, google) -> dict:
    url = client.post("/api/v1/integrations/google/connect/start", headers=AUTH).json()["data"][
        "authorization_url"
    ]
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    code = google.authorize(url)
    page = client.get(f"/api/v1/integrations/google/callback?state={query['state']}&code={code}")
    assert page.status_code == 200, page.text
    assert "Connected" in page.text
    return client.get("/api/v1/external-accounts", headers=AUTH).json()["data"][0]


def sources(client) -> dict[str, dict]:
    return {s["name"]: s for s in client.get("/api/v1/calendar-sources", headers=AUTH).json()["data"]}


def wait_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()["data"]
        if job["state"] in ("succeeded", "dead_letter") or (job["state"] == "queued" and job["attempts"] > 0):
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.05)


def enable(client, source: dict, space_id: str) -> dict:
    res = client.patch(
        f"/api/v1/calendar-sources/{source['id']}",
        json={"space_id": space_id, "enabled": True},
        headers=rev(source["revision"]),
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]


def sync(client, source_id: str) -> dict:
    res = client.post(f"/api/v1/calendar-sources/{source_id}/sync", headers=AUTH)
    assert res.status_code == 202, res.text
    return wait_job(client, res.json()["data"]["job_ids"][0])


def window(client, space_id, start="2026-10-01", end="2026-12-01"):
    res = client.get(f"/api/v1/spaces/{space_id}/events?from={start}&to={end}", headers=AUTH)
    return res.json()["data"]


def timed(event_id, summary, start, end, **extra):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start, "timeZone": "America/New_York"},
        "end": {"dateTime": end, "timeZone": "America/New_York"},
        **extra,
    }


def local(o) -> str:
    return datetime.fromisoformat(o["start_at"]).astimezone(NY).strftime("%m-%d %H:%M")


@pytest.fixture
def imported(app, google):
    """Connected, with the family calendar mapped to the Family space and synced once."""
    client, spaces = app
    google.put(
        "family-cal", timed("dentist", "Dentist", "2026-10-21T14:30:00-04:00", "2026-10-21T15:30:00-04:00")
    )
    google.put(
        "family-cal",
        timed(
            "standup",
            "Standup",
            "2026-10-20T09:00:00-04:00",
            "2026-10-20T09:30:00-04:00",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=America/New_York:20261103T090000"],
        ),
    )
    google.put(
        "family-cal",
        {"id": "trip", "summary": "Trip", "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-27"}},
    )
    connect(client, google)
    source = enable(client, sources(client)["Family shared"], spaces["Family"]["id"])
    job = wait_job(
        client,
        client.post(f"/api/v1/calendar-sources/{source['id']}/sync", headers=AUTH).json()["data"]["job_ids"][
            0
        ],
    )
    assert job["state"] == "succeeded", job
    return client, spaces, sources(client)["Family shared"]


class TestOAuth:
    def test_connect_stores_token_in_vault_and_lists_calendars(self, app, google, vault):
        client, _ = app
        account = connect(client, google)
        assert (account["email"], account["status"]) == ("pete@example.com", "active")
        assert "https://www.googleapis.com/auth/calendar.readonly" in account["granted_scopes"]
        assert vault.get(f"google:{account['id']}") in google.refresh_tokens
        listed = sources(client)
        assert set(listed) == {"Pete", "Family shared"}
        assert not any(s["enabled"] for s in listed.values())  # nothing imports until mapped
        assert listed["Pete"]["is_primary"]

    def test_state_is_one_time(self, app, google):
        client, _ = app
        url = client.post("/api/v1/integrations/google/connect/start", headers=AUTH).json()["data"][
            "authorization_url"
        ]
        state = parse_qs(urlparse(url).query)["state"][0]
        code = google.authorize(url)
        assert (
            client.get(f"/api/v1/integrations/google/callback?state={state}&code={code}").status_code == 200
        )
        replay = client.get(f"/api/v1/integrations/google/callback?state={state}&code={code}")
        assert replay.status_code == 422
        assert "expired or was already used" in replay.text

    def test_unknown_state_is_refused(self, app):
        client, _ = app
        assert client.get("/api/v1/integrations/google/callback?state=forged&code=x").status_code == 422

    def test_callback_is_html_and_needs_no_secret(self, app):
        client, _ = app
        res = client.get("/api/v1/integrations/google/callback?error=access_denied&state=x")
        assert res.headers["content-type"].startswith("text/html")
        assert "cancelled" in res.text

    def test_requires_client_config(self, settings, google, vault):
        with running(settings, http_transport=google.transport(), vault=vault) as client:
            complete_setup(client)
            res = client.post("/api/v1/integrations/google/connect/start", headers=AUTH)
            assert res.json()["error"]["code"] == "OAUTH_CLIENT_MISSING"
            assert (
                client.get("/api/v1/integrations/google/client", headers=AUTH).json()["data"]["configured"]
                is False
            )

    def test_refuses_to_connect_without_a_vault(self, settings, google, monkeypatch):
        monkeypatch.setattr("app.integrations.vault.os_vault_available", lambda: False)
        with running(
            settings, http_transport=google.transport()
        ) as client:  # release build: no file fallback
            complete_setup(client)
            client.put("/api/v1/integrations/google/client", json=CLIENT, headers=AUTH)
            res = client.post("/api/v1/integrations/google/connect/start", headers=AUTH)
            assert res.status_code == 503
            assert res.json()["error"]["code"] == "VAULT_UNAVAILABLE"

    def test_client_id_format_is_checked(self, app):
        client, _ = app
        res = client.put(
            "/api/v1/integrations/google/client", json={**CLIENT, "client_id": "nope"}, headers=AUTH
        )
        assert res.status_code == 422


class TestMapping:
    def test_enable_requires_a_space(self, app, google):
        client, _ = app
        connect(client, google)
        source = sources(client)["Pete"]
        res = client.patch(f"/api/v1/calendar-sources/{source['id']}", json={"enabled": True}, headers=rev(1))
        assert res.json()["error"]["code"] == "SPACE_REQUIRED"

    def test_imported_events_land_in_the_mapped_space_read_only(self, imported):
        client, spaces, _ = imported
        assert window(client, spaces["Personal"]["id"]) == []
        occurrences = window(client, spaces["Family"]["id"])
        assert {o["title"] for o in occurrences} == {"Dentist", "Standup", "Trip"}
        assert all(o["read_only"] for o in occurrences)
        dentist = next(o for o in occurrences if o["title"] == "Dentist")
        res = client.patch(
            f"/api/v1/events/{dentist['event_id']}", json={"title": "x"}, headers=rev(dentist["revision"])
        )
        assert res.json()["error"]["code"] == "READ_ONLY_SOURCE"

    def test_moving_a_calendar_moves_its_events(self, imported):
        client, spaces, source = imported
        res = client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"space_id": spaces["Work"]["id"]},
            headers=rev(source["revision"]),
        )
        assert res.status_code == 200
        assert window(client, spaces["Family"]["id"]) == []
        assert len(window(client, spaces["Work"]["id"])) > 0

    def test_disabling_removes_imported_copies(self, imported):
        client, spaces, source = imported
        client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"enabled": False},
            headers=rev(source["revision"]),
        )
        assert window(client, spaces["Family"]["id"]) == []
        assert sources(client)["Family shared"]["last_synced_at"] is None


class TestImport:
    def test_recurrence_keeps_wall_clock_across_dst_and_honours_exdate(self, imported):
        client, spaces, _ = imported
        standups = [
            o
            for o in window(client, spaces["Family"]["id"], "2026-10-19", "2026-11-16")
            if o["title"] == "Standup"
        ]
        # Nov 3 is excluded by EXDATE; Nov 10 is after the DST change and still 09:00
        assert [local(o) for o in standups] == ["10-20 09:00", "10-27 09:00", "11-10 09:00"]

    def test_all_day_multi_day(self, imported):
        client, spaces, _ = imported
        [trip] = [o for o in window(client, spaces["Family"]["id"]) if o["title"] == "Trip"]
        assert (trip["start_date"], trip["end_date"], trip["all_day"]) == ("2026-10-24", "2026-10-27", True)

    def test_moved_and_cancelled_instances(self, imported, google):
        client, spaces, source = imported
        google.put(
            "family-cal",
            timed(
                "standup_20261027T130000Z",
                "Standup (moved)",
                "2026-10-28T11:00:00-04:00",
                "2026-10-28T11:30:00-04:00",
                recurringEventId="standup",
                originalStartTime={"dateTime": "2026-10-27T09:00:00-04:00", "timeZone": "America/New_York"},
            ),
        )
        google.put(
            "family-cal",
            timed(
                "standup_20261110T140000Z",
                "Standup",
                "2026-11-10T09:00:00-05:00",
                "2026-11-10T09:30:00-05:00",
                recurringEventId="standup",
                originalStartTime={"dateTime": "2026-11-10T09:00:00-05:00", "timeZone": "America/New_York"},
            ),
        )
        google.delete("family-cal", "standup_20261110T140000Z")
        assert sync(client, source["id"])["state"] == "succeeded"
        standups = [
            o
            for o in window(client, spaces["Family"]["id"], "2026-10-19", "2026-11-16")
            if "Standup" in o["title"]
        ]
        assert [(local(o), o["title"]) for o in standups] == [
            ("10-20 09:00", "Standup"),
            ("10-28 11:00", "Standup (moved)"),
        ]

    def test_override_before_its_series_in_the_response(self, app, google):
        client, spaces = app
        google.order_reversed = True
        google.put(
            "primary-cal",
            timed(
                "yoga",
                "Yoga",
                "2026-10-21T18:00:00-04:00",
                "2026-10-21T19:00:00-04:00",
                recurrence=["RRULE:FREQ=WEEKLY"],
            ),
        )
        google.put(
            "primary-cal",
            timed(
                "yoga_x",
                "Hot yoga",
                "2026-10-28T18:00:00-04:00",
                "2026-10-28T19:00:00-04:00",
                recurringEventId="yoga",
                originalStartTime={"dateTime": "2026-10-28T18:00:00-04:00"},
            ),
        )
        connect(client, google)
        source = enable(client, sources(client)["Pete"], spaces["Personal"]["id"])
        assert sync(client, source["id"])["state"] == "succeeded"
        titles = [o["title"] for o in window(client, spaces["Personal"]["id"], "2026-10-20", "2026-11-01")]
        assert titles == ["Yoga", "Hot yoga"]

    def test_unsupported_events_are_skipped_not_guessed(self, app, google):
        client, spaces = app
        google.put(
            "primary-cal",
            timed(
                "odd",
                "Odd",
                "2026-10-21T18:00:00Z",
                "2026-10-21T19:00:00Z",
                recurrence=["RDATE:20261030T180000Z"],
            ),
        )
        google.put("primary-cal", timed("fine", "Fine", "2026-10-22T18:00:00Z", "2026-10-22T19:00:00Z"))
        connect(client, google)
        source = enable(client, sources(client)["Pete"], spaces["Personal"]["id"])
        assert sync(client, source["id"])["state"] == "succeeded"
        assert [o["title"] for o in window(client, spaces["Personal"]["id"])] == ["Fine"]

    def test_off_pattern_start_is_the_first_occurrence(self, app, google):
        client, spaces = app
        # starts Monday, repeats Tuesdays: RFC 5545 still counts the Monday
        google.put(
            "primary-cal",
            timed(
                "x",
                "Class",
                "2026-10-19T10:00:00-04:00",
                "2026-10-19T11:00:00-04:00",
                recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=3"],
            ),
        )
        connect(client, google)
        source = enable(client, sources(client)["Pete"], spaces["Personal"]["id"])
        sync(client, source["id"])
        days = [local(o)[:5] for o in window(client, spaces["Personal"]["id"])]
        assert days[:2] == ["10-19", "10-20"]

    def test_paging(self, app, google):
        client, spaces = app
        google.page_size = 2
        for n in range(5):
            google.put(
                "primary-cal", timed(f"e{n}", f"E{n}", f"2026-10-2{n}T10:00:00Z", f"2026-10-2{n}T11:00:00Z")
            )
        connect(client, google)
        source = enable(client, sources(client)["Pete"], spaces["Personal"]["id"])
        sync(client, source["id"])
        assert len(window(client, spaces["Personal"]["id"])) == 5


class TestIncremental:
    def test_changes_additions_and_deletions(self, imported, google):
        client, spaces, source = imported
        requests_before = google.event_requests
        google.put(
            "family-cal",
            timed(
                "dentist", "Dentist (rescheduled)", "2026-10-22T10:00:00-04:00", "2026-10-22T11:00:00-04:00"
            ),
        )
        google.put(
            "family-cal", timed("party", "Party", "2026-10-30T19:00:00-04:00", "2026-10-30T22:00:00-04:00")
        )
        google.delete("family-cal", "trip")
        sync(client, source["id"])
        titles = sorted({o["title"] for o in window(client, spaces["Family"]["id"])})
        assert titles == ["Dentist (rescheduled)", "Party", "Standup"]
        assert google.event_requests == requests_before + 1  # one incremental request, not a re-download

    def test_deleting_a_series_removes_its_exceptions(self, imported, google):
        client, spaces, source = imported
        google.delete("family-cal", "standup")
        sync(client, source["id"])
        assert not any("Standup" in o["title"] for o in window(client, spaces["Family"]["id"]))

    def test_repeated_syncs_never_duplicate(self, imported):
        client, spaces, source = imported
        before = window(client, spaces["Family"]["id"])
        for _ in range(3):
            sync(client, source["id"])
        assert len(window(client, spaces["Family"]["id"])) == len(before)


class TestFullResync:
    def test_expired_sync_token_triggers_reconciling_full_sync(self, imported, google):
        client, spaces, source = imported
        google.expire_sync_tokens("family-cal")
        google.put(
            "family-cal",
            timed("dentist", "Dentist v2", "2026-10-21T14:30:00-04:00", "2026-10-21T15:30:00-04:00"),
        )
        # removed outright while our token was invalid: only a full listing reveals it is gone
        del google.calendars["family-cal"].events["trip"]
        job = sync(client, source["id"])
        assert job["state"] == "succeeded"
        titles = sorted(o["title"] for o in window(client, spaces["Family"]["id"]) if o["title"] != "Standup")
        assert titles == ["Dentist v2"]
        assert sync(client, source["id"])["state"] == "succeeded"  # the fresh token works


class TestErrors:
    def test_revoked_refresh_token_needs_reauth_then_reconnect_recovers(self, imported, google, vault):
        client, _, source = imported
        google.refresh_tokens.clear()  # e.g. the 7-day Testing-mode expiry
        client.app.state.integrations.forget(source["external_account_id"])  # drop the cached access token
        job = sync(client, source["id"])
        assert (job["state"], job["last_error_category"]) == ("dead_letter", "needs_reauth")
        account = client.get("/api/v1/external-accounts", headers=AUTH).json()["data"][0]
        assert account["status"] == "needs_reauth"
        assert sources(client)["Family shared"]["sync_state"] == "needs_reauth"
        connect(client, google)  # same Google account -> same Agenda account, reactivated
        accounts = client.get("/api/v1/external-accounts", headers=AUTH).json()["data"]
        assert [(a["id"], a["status"]) for a in accounts] == [(account["id"], "active")]
        assert sync(client, source["id"])["state"] == "succeeded"

    def test_rate_limit_is_retried_later(self, imported, google):
        client, _, source = imported
        google.fail_next = [httpx.Response(429, headers={"Retry-After": "120"})]
        job = sync(client, source["id"])
        assert (job["state"], job["last_error_category"]) == ("queued", "rate_limited")
        assert sources(client)["Family shared"]["last_error"] == "rate_limited"


class TestDisconnect:
    def test_keep_events_turns_them_into_local_events(self, imported, google, vault):
        client, spaces, source = imported
        account_id = source["external_account_id"]
        res = client.delete(f"/api/v1/external-accounts/{account_id}?keep_events=true", headers=AUTH)
        assert res.status_code == 204
        assert google.revoked and vault.get(f"google:{account_id}") is None
        assert sources(client) == {}
        occurrences = window(client, spaces["Family"]["id"])
        assert occurrences and not any(o["read_only"] for o in occurrences)

    def test_default_removes_imported_events(self, imported):
        client, spaces, source = imported
        client.delete(f"/api/v1/external-accounts/{source['external_account_id']}", headers=AUTH)
        assert window(client, spaces["Family"]["id"]) == []
