"""Microsoft (Outlook) contract suite (spec §10, §16): the same scenarios as the Google suite —
OAuth, import, incremental sync, expired-cursor resync, deletions, errors, disconnect — plus
two-way writes and conflicts. The real client and engines run against tests/fake_microsoft.py."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.integrations.vault import MemoryVault
from tests.conftest import AUTH, PORT, complete_setup, rev, running
from tests.fake_microsoft import FakeMicrosoft
from tests.test_google import enable, sources, sync, wait_job, window
from tests.test_two_way import conflicts, force_due, settle

NY = ZoneInfo("America/New_York")
CLIENT = {"client_id": "6731de76-14a6-49ae-97bc-6eba6914391e", "tenant": "common"}
CALLBACK = f"http://localhost:{PORT}/api/v1/integrations/microsoft/callback"


@pytest.fixture
def outlook():
    fake = FakeMicrosoft()
    fake.add_calendar("cal-main", "Calendar", default=True)
    fake.add_calendar("cal-family", "Family")
    fake.add_calendar("cal-holidays", "United States holidays", can_edit=False)
    return fake


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def app(settings, outlook, vault):
    with running(settings, http_transport=outlook.transport(), vault=vault) as client:
        spaces = complete_setup(client)
        res = client.put("/api/v1/integrations/microsoft/client", json=CLIENT, headers=AUTH)
        assert res.status_code == 200, res.text
        yield client, spaces


def start(client, *, write: bool = False) -> str:
    res = client.post(
        f"/api/v1/integrations/microsoft/connect/start?write={str(write).lower()}", headers=AUTH
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]["authorization_url"]


def connect(client, outlook, *, write: bool = False) -> dict:
    url = start(client, write=write)
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert query["redirect_uri"] == CALLBACK  # localhost: Entra ignores its port
    code = outlook.authorize(url)
    # the browser arrives as localhost, which the host guard accepts on this one path
    page = client.get(f"{CALLBACK}?{urlencode({'state': query['state'], 'code': code})}")
    assert page.status_code == 200, page.text
    return client.get("/api/v1/external-accounts", headers=AUTH).json()["data"][0]


def ev(event_id, subject, start, end, **extra):
    """An Outlook item as Graph returns it with Prefer: outlook.timezone="UTC"."""
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": f"{start}.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{end}.0000000", "timeZone": "UTC"},
        **extra,
    }


def local(o) -> str:
    return datetime.fromisoformat(o["start_at"]).astimezone(NY).strftime("%m-%d %H:%M")


@pytest.fixture
def imported(app, outlook):
    client, spaces = app
    outlook.put("cal-family", ev("AAMk-dentist", "Dentist", "2026-10-21T18:30:00", "2026-10-21T19:30:00"))
    # a weekly series arrives as its occurrences, one of them moved (an exception)
    for n, (day, hour) in enumerate([("20", "13"), ("27", "13"), ("03", "14")]):
        month = "11" if day == "03" else "10"
        outlook.put(
            "cal-family",
            ev(
                f"AAMk-standup-{n}",
                "Standup",
                f"2026-{month}-{day}T{hour}:00:00",
                f"2026-{month}-{day}T{hour}:30:00",
                type="occurrence",
                seriesMasterId="AAMk-standup",
            ),
        )
    outlook.put(
        "cal-family",
        {
            "id": "AAMk-trip",
            "subject": "Trip",
            "isAllDay": True,
            "start": {"dateTime": "2026-10-24T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-27T00:00:00.0000000", "timeZone": "UTC"},
        },
    )
    connect(client, outlook)
    source = enable(client, sources(client)["Family"], spaces["Family"]["id"])
    assert sync(client, source["id"])["state"] == "succeeded"
    return client, spaces, sources(client)["Family"]


class TestOAuth:
    def test_connect_stores_token_and_lists_calendars(self, app, outlook, vault):
        client, _ = app
        url = start(client)
        query = parse_qs(urlparse(url).query)
        assert urlparse(url).path == "/common/oauth2/v2.0/authorize"
        assert "https://graph.microsoft.com/Calendars.Read" in query["scope"][0]
        account = connect(client, outlook)
        assert (account["provider"], account["email"], account["status"]) == (
            "microsoft",
            "pete@outlook.com",
            "active",
        )
        assert "https://graph.microsoft.com/Calendars.Read" in account["granted_scopes"]
        assert account["can_write"] is False
        assert vault.get(f"microsoft:{account['id']}") in outlook.refresh_tokens
        listed = sources(client)
        assert set(listed) == {"Calendar", "Family", "United States holidays"}
        assert listed["Calendar"]["is_primary"] and listed["Calendar"]["timezone"] == "America/New_York"
        assert listed["United States holidays"]["access_role"] == "reader"

    def test_tenant_is_used_for_sign_in(self, app):
        client, _ = app
        client.put(
            "/api/v1/integrations/microsoft/client", json={**CLIENT, "tenant": "consumers"}, headers=AUTH
        )
        assert urlparse(start(client)).path == "/consumers/oauth2/v2.0/authorize"

    def test_client_status_shows_the_redirect_uri_to_register(self, app):
        client, _ = app
        status = client.get("/api/v1/integrations/microsoft/client", headers=AUTH).json()["data"]
        assert status["redirect_uri"] == "http://localhost/api/v1/integrations/microsoft/callback"
        assert (status["client_id"], status["tenant"]) == (CLIENT["client_id"], "common")

    def test_client_id_must_be_a_guid(self, app):
        client, _ = app
        res = client.put("/api/v1/integrations/microsoft/client", json={"client_id": "nope"}, headers=AUTH)
        assert res.status_code == 422

    def test_state_is_one_time_and_bound_to_the_provider(self, app, outlook):
        client, _ = app
        url = start(client)
        state = parse_qs(urlparse(url).query)["state"][0]
        code = outlook.authorize(url)
        google = client.get(f"/api/v1/integrations/google/callback?state={state}&code={code}")
        assert google.status_code == 422  # a Microsoft state can't complete a Google sign-in

    def test_localhost_host_is_only_accepted_on_the_callback(self, app):
        client, _ = app
        res = client.get(f"http://localhost:{PORT}/api/v1/external-accounts", headers=AUTH)
        assert res.json()["error"]["code"] == "BAD_HOST"

    def test_entra_errors_are_explained(self, app):
        client, _ = app
        description = "AADSTS50011: The redirect URI does not match"
        query = {"error": "invalid_request", "error_description": description, "state": "x"}
        res = client.get(f"{CALLBACK}?{urlencode(query)}")
        assert res.status_code == 400
        assert "AADSTS50011" in res.text and "Mobile and desktop applications" in res.text

    def test_misregistered_app_is_reported_not_retried(self, app, outlook):
        client, _ = app
        url = start(client)
        state = parse_qs(urlparse(url).query)["state"][0]
        code = outlook.authorize(url)
        outlook.token_error = (
            400,
            {"error": "invalid_client", "error_codes": [7000218]},
        )  # registered as Web
        page = client.get(f"{CALLBACK}?{urlencode({'state': state, 'code': code})}")
        assert page.status_code == 502 and "sign_in_rejected" in page.text


class TestImport:
    def test_events_land_in_the_mapped_space_read_only(self, imported):
        client, spaces, _ = imported
        occurrences = window(client, spaces["Family"]["id"])
        assert sorted({o["title"] for o in occurrences}) == ["Dentist", "Standup", "Trip"]
        assert all(o["read_only"] for o in occurrences)
        assert [local(o) for o in occurrences if o["title"] == "Standup"] == [
            "10-20 09:00",
            "10-27 09:00",
            "11-03 09:00",
        ]
        [trip] = [o for o in occurrences if o["title"] == "Trip"]
        assert (trip["start_date"], trip["end_date"]) == ("2026-10-24", "2026-10-27")
        [dentist] = [o for o in occurrences if o["title"] == "Dentist"]
        assert dentist["timezone"] == "America/New_York"  # from originalStartTimeZone (Windows name)

    def test_incremental_changes_and_removals(self, imported, outlook):
        client, spaces, source = imported
        before = outlook.event_requests
        outlook.put(
            "cal-family", ev("AAMk-dentist", "Dentist (moved)", "2026-10-22T14:00:00", "2026-10-22T15:00:00")
        )
        outlook.delete("cal-family", "AAMk-trip")
        sync(client, source["id"])
        assert sorted({o["title"] for o in window(client, spaces["Family"]["id"])}) == [
            "Dentist (moved)",
            "Standup",
        ]
        assert outlook.event_requests == before + 1

    def test_expired_delta_token_runs_a_reconciling_full_sync(self, imported, outlook):
        client, spaces, source = imported
        outlook.expire_delta_tokens("cal-family")
        del outlook.calendars["cal-family"].events["AAMk-trip"]  # gone while the token was dead
        assert sync(client, source["id"])["state"] == "succeeded"
        assert "Trip" not in {o["title"] for o in window(client, spaces["Family"]["id"])}
        assert sync(client, source["id"])["state"] == "succeeded"

    def test_paging_and_no_duplicates(self, app, outlook):
        client, spaces = app
        outlook.page_size = 2
        for n in range(5):
            outlook.put("cal-main", ev(f"e{n}", f"E{n}", f"2026-10-2{n}T15:00:00", f"2026-10-2{n}T16:00:00"))
        connect(client, outlook)
        source = enable(client, sources(client)["Calendar"], spaces["Personal"]["id"])
        for _ in range(3):
            sync(client, source["id"])
        assert len(window(client, spaces["Personal"]["id"])) == 5

    def test_series_masters_are_not_imported_twice(self, app, outlook):
        client, spaces = app
        outlook.put(
            "cal-main",
            ev("master", "Yoga", "2026-10-21T22:00:00", "2026-10-21T23:00:00", type="seriesMaster"),
        )
        outlook.put(
            "cal-main", ev("occ", "Yoga", "2026-10-21T22:00:00", "2026-10-21T23:00:00", type="occurrence")
        )
        connect(client, outlook)
        source = enable(client, sources(client)["Calendar"], spaces["Personal"]["id"])
        sync(client, source["id"])
        assert [o["title"] for o in window(client, spaces["Personal"]["id"])] == ["Yoga"]


class TestErrors:
    def test_dead_refresh_token_needs_reauth_then_reconnect_recovers(self, imported, outlook):
        client, _, source = imported
        outlook.refresh_tokens.clear()
        client.app.state.integrations.forget(source["external_account_id"])
        job = sync(client, source["id"])
        assert (job["state"], job["last_error_category"]) == ("dead_letter", "needs_reauth")
        connect(client, outlook)
        accounts = client.get("/api/v1/external-accounts", headers=AUTH).json()["data"]
        assert [(a["id"], a["status"]) for a in accounts] == [(source["external_account_id"], "active")]
        assert sync(client, source["id"])["state"] == "succeeded"

    def test_rotated_refresh_tokens_are_kept(self, imported, outlook, vault):
        client, _, source = imported
        client.app.state.integrations.forget(source["external_account_id"])
        assert sync(client, source["id"])["state"] == "succeeded"
        assert vault.get(f"microsoft:{source['external_account_id']}") in outlook.refresh_tokens

    def test_throttling_is_retried(self, imported, outlook):
        client, _, source = imported
        outlook.fail_next = [httpx.Response(429, headers={"Retry-After": "30"})]
        job = sync(client, source["id"])
        assert (job["state"], job["last_error_category"]) == ("queued", "rate_limited")

    def test_disconnect_keeps_events_as_agenda_events(self, imported, vault):
        client, spaces, source = imported
        account_id = source["external_account_id"]
        client.delete(f"/api/v1/external-accounts/{account_id}?keep_events=true", headers=AUTH)
        assert vault.get(f"microsoft:{account_id}") is None
        assert not any(o["read_only"] for o in window(client, spaces["Family"]["id"]))


@pytest.fixture
def writable(imported, outlook):
    client, spaces, source = imported
    connect(client, outlook, write=True)
    res = client.patch(
        f"/api/v1/calendar-sources/{source['id']}",
        json={"sync_mode": "two_way"},
        headers=rev(sources(client)["Family"]["revision"]),
    )
    assert res.status_code == 200, res.text
    return client, spaces, res.json()["data"]


def find(client, space_id, title) -> dict:
    return next(o for o in window(client, space_id) if o["title"] == title)


class TestTwoWay:
    def test_write_scope_comes_from_signing_in_again(self, imported, outlook):
        client, _, source = imported
        res = client.patch(
            f"/api/v1/calendar-sources/{source['id']}",
            json={"sync_mode": "two_way"},
            headers=rev(source["revision"]),
        )
        assert res.json()["error"]["code"] == "WRITE_ACCESS_REQUIRED"
        assert "Calendars.ReadWrite" in parse_qs(urlparse(start(client, write=True)).query)["scope"][0]

    def test_read_only_calendars_stay_read_only(self, writable):
        client, spaces, _ = writable
        holidays = sources(client)["United States holidays"]
        res = client.patch(
            f"/api/v1/calendar-sources/{holidays['id']}",
            json={"space_id": spaces["Personal"]["id"], "enabled": True, "sync_mode": "two_way"},
            headers=rev(holidays["revision"]),
        )
        assert res.json()["error"]["code"] == "READ_ONLY_CALENDAR"

    def test_edit_one_occurrence_with_if_match(self, writable, outlook):
        client, spaces, _ = writable
        standup = [o for o in window(client, spaces["Family"]["id"]) if o["title"] == "Standup"][1]
        etag = outlook.calendars["cal-family"].events["AAMk-standup-1"]["@odata.etag"]
        res = client.patch(
            f"/api/v1/events/{standup['event_id']}",
            json={"start_at": "2026-10-27T10:00:00-04:00"},
            headers=rev(standup["revision"]),
        )
        assert res.status_code == 200, res.text
        settle(client)
        assert ("PATCH", "AAMk-standup-1", etag) in outlook.writes
        item = outlook.calendars["cal-family"].events["AAMk-standup-1"]
        assert item["start"]["dateTime"].startswith("2026-10-27T14:00:00")
        assert "body" not in outlook.last_patch  # an unchanged (possibly rich) body isn't resent

    def test_create_and_delete(self, writable, outlook):
        client, spaces, source = writable
        res = client.post(
            f"/api/v1/spaces/{spaces['Family']['id']}/events",
            json={
                "title": "Picnic",
                "all_day": True,
                "start_date": "2026-10-31",
                "end_date": "2026-11-01",
                "calendar_source_id": source["id"],
            },
            headers=AUTH,
        )
        assert res.status_code == 201, res.text
        settle(client)
        [picnic] = [e for e in outlook.calendars["cal-family"].events.values() if e["subject"] == "Picnic"]
        assert picnic["isAllDay"] and picnic["start"]["dateTime"] == "2026-10-31T00:00:00"
        sync(client, source["id"])
        occurrence = find(client, spaces["Family"]["id"], "Picnic")
        client.delete(f"/api/v1/events/{occurrence['event_id']}", headers=rev(occurrence["revision"]))
        settle(client)
        assert not any(e["subject"] == "Picnic" for e in outlook.calendars["cal-family"].events.values())

    def test_retried_post_is_deduplicated_by_transaction_id(self, writable, outlook):
        client, spaces, source = writable
        outlook.lose_responses = 1  # created in Outlook, but the response is lost
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
        settle(client)
        force_due(client)
        assert sources(client)["Family"]["pending_changes"] == 0
        assert sum(e["subject"] == "Recital" for e in outlook.calendars["cal-family"].events.values()) == 1
        sync(client, source["id"])
        assert [o["title"] for o in window(client, spaces["Family"]["id"])].count("Recital") == 1
        assert res.status_code == 201

    def test_repeating_events_are_refused(self, writable):
        client, spaces, source = writable
        res = client.post(
            f"/api/v1/spaces/{spaces['Family']['id']}/events",
            json={
                "title": "Weekly",
                "start_at": "2026-10-26T18:00:00Z",
                "end_at": "2026-10-26T19:00:00Z",
                "rrule": "FREQ=WEEKLY",
                "calendar_source_id": source["id"],
            },
            headers=AUTH,
        )
        assert res.json()["error"]["code"] == "PROVIDER_UNSUPPORTED"
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        res = client.patch(
            f"/api/v1/events/{dentist['event_id']}?scope=all",
            json={"rrule": "FREQ=WEEKLY"},
            headers=rev(dentist["revision"]),
        )
        assert res.json()["error"]["code"] == "PROVIDER_UNSUPPORTED"

    def test_conflict_keeps_both_versions_until_resolved(self, writable, outlook):
        client, spaces, source = writable
        outlook.fail_writes = [httpx.Response(503, headers={"Retry-After": "0"})]
        dentist = find(client, spaces["Family"]["id"], "Dentist")
        client.patch(
            f"/api/v1/events/{dentist['event_id']}",
            json={"title": "Dentist (Agenda)"},
            headers=rev(dentist["revision"]),
        )
        settle(client)
        outlook.put(
            "cal-family",
            ev("AAMk-dentist", "Dentist (Outlook)", "2026-10-21T18:30:00", "2026-10-21T19:30:00"),
        )
        force_due(client)
        assert outlook.calendars["cal-family"].events["AAMk-dentist"]["subject"] == "Dentist (Outlook)"
        [conflict] = conflicts(client)
        assert (conflict["provider"], conflict["local"]["title"], conflict["remote"]["title"]) == (
            "microsoft",
            "Dentist (Agenda)",
            "Dentist (Outlook)",
        )
        sync(client, source["id"])  # a poll doesn't overwrite either side while it is open
        assert find(client, spaces["Family"]["id"], "Dentist (Agenda)")
        client.post(
            f"/api/v1/sync-conflicts/{conflict['id']}/resolve",
            json={"resolution": "keep_local"},
            headers=AUTH,
        )
        settle(client)
        assert outlook.calendars["cal-family"].events["AAMk-dentist"]["subject"] == "Dentist (Agenda)"
        assert conflicts(client) == []
        job = wait_job(
            client,
            client.post(f"/api/v1/calendar-sources/{source['id']}/sync", headers=AUTH).json()["data"][
                "job_ids"
            ][0],
        )
        assert job["state"] == "succeeded"
        assert conflicts(client) == []
