"""Microsoft-specific behaviour (ADR 0005). The shared scenarios live in test_calendar_contract.py."""

from __future__ import annotations

import pytest

from app.integrations.microsoft.client import MicrosoftCalendar
from app.integrations.vault import MemoryVault
from tests.calendar_harness import WINDOW, MicrosoftHarness
from tests.conftest import AUTH, PORT, complete_setup, rev, running
from tests.test_calendar_contract import find, import_family, local, source, sync, two_way_on, window


@pytest.fixture
def harness(monkeypatch) -> MicrosoftHarness:
    monkeypatch.setattr(MicrosoftCalendar, "instance_window", lambda self: WINDOW)
    return MicrosoftHarness()


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def app(settings, harness, vault):
    with running(settings, http_transport=harness.fake.transport(), vault=vault) as client:
        spaces = complete_setup(client)
        client.put("/api/v1/integrations/microsoft/client", json=harness.client_config, headers=AUTH)
        yield client, spaces


class TestSignIn:
    def test_public_client_with_pkce_and_a_localhost_redirect(self, app, harness, vault):
        client, _ = app
        account = harness.connect(client)  # the fake refuses a client secret or a mismatched redirect
        assert (account["provider"], account["email"], account["status"]) == (
            "microsoft",
            "pete@outlook.com",
            "active",
        )
        assert "Calendars.Read" in account["granted_scopes"]
        assert vault.get(f"microsoft:{account['id']}") in harness.fake.refresh_tokens
        listed = {s["name"]: s for s in client.get("/api/v1/calendar-sources", headers=AUTH).json()["data"]}
        assert set(listed) == {"Calendar", "Family shared"}
        assert listed["Calendar"]["is_primary"] and not listed["Calendar"]["can_write"]

    def test_client_status_tells_what_to_register(self, app):
        client, _ = app
        status = client.get("/api/v1/integrations/microsoft/client", headers=AUTH).json()["data"]
        assert status["configured"]
        assert status["redirect_uri"] == "http://localhost/api/v1/integrations/microsoft/callback"

    def test_client_id_must_be_an_application_id(self, app):
        client, _ = app
        res = client.put("/api/v1/integrations/microsoft/client", json={"client_id": "nope"}, headers=AUTH)
        assert res.json()["error"]["code"] == "INVALID_CLIENT_ID"

    def test_localhost_is_accepted_only_for_the_microsoft_callback(self, app):
        client, _ = app
        res = client.get(f"http://localhost:{PORT}/api/v1/spaces", headers=AUTH)
        assert res.json()["error"]["code"] == "BAD_HOST"
        res = client.get(f"http://localhost:{PORT}/api/v1/integrations/google/callback?state=x&code=y")
        assert res.json()["error"]["code"] == "BAD_HOST"

    def test_rotated_refresh_tokens_are_kept(self, app, harness, vault):
        client, spaces = app
        import_family(client, spaces, harness, write=False)
        account_id = source(client)["external_account_id"]
        before = vault.get(f"microsoft:{account_id}")
        client.app.state.integrations.forget(account_id)  # force a refresh
        assert sync(client)["state"] == "succeeded"
        assert vault.get(f"microsoft:{account_id}") not in (None, before)

    def test_revoked_consent_needs_reauth(self, app, harness):
        client, spaces = app
        import_family(client, spaces, harness, write=False)
        harness.fake.refresh_tokens.clear()
        client.app.state.integrations.forget(source(client)["external_account_id"])
        job = sync(client)
        assert (job["state"], job["last_error_category"]) == ("dead_letter", "needs_reauth")
        assert source(client)["sync_state"] == "needs_reauth"


class TestPatterns:
    def test_patterns_outlook_has_that_agenda_cannot_hold_are_skipped(self, app, harness):
        client, spaces = app
        harness.single("ok", "Fine", "2026-10-22T10:00", "2026-10-22T11:00")
        harness.weekly("odd", "Odd", "2026-10-21T10:00", "2026-10-21T11:00")
        harness.fake.calendars["family-cal"].events["odd"]["recurrence"]["range"]["type"] = "weird"
        import_family(client, spaces, harness, write=False)
        assert [o["title"] for o in window(client, spaces["Family"]["id"])] == ["Fine"]

    def test_monthly_by_weekday_round_trips(self, app, harness):
        client, spaces = app
        harness.weekly("book", "Book club", "2026-10-13T19:00", "2026-10-13T20:00")
        harness.fake.calendars["family-cal"].events["book"]["recurrence"]["pattern"] = {
            "type": "relativeMonthly",
            "interval": 1,
            "daysOfWeek": ["tuesday"],
            "index": "second",
        }
        import_family(client, spaces, harness, write=True)
        days = [local(o) for o in window(client, spaces["Family"]["id"], "2026-10-01", "2027-01-01")]
        assert days == ["10-13 19:00", "11-10 19:00", "12-08 19:00"]
        two_way_on(client)
        book = find(client, spaces["Family"]["id"], "Book club", "10-13 19:00")
        res = client.patch(
            f"/api/v1/events/{book['event_id']}",
            params={"scope": "all", "occurrence_start": book["occurrence_start"]},
            json={"title": "Book club!"},
            headers=rev(book["revision"]),
        )
        assert res.status_code == 200, res.text
        sync(client)
        pattern = harness.fake.calendars["family-cal"].events["book"]["recurrence"]["pattern"]
        assert (pattern["type"], pattern["index"], pattern["daysOfWeek"]) == (
            "relativeMonthly",
            "second",
            ["tuesday"],
        )
        assert harness.remote("book").title == "Book club!"

    def test_rules_outlook_cannot_store_are_refused_up_front(self, app, harness):
        client, spaces = app
        harness.weekly("standup", "Standup", "2026-10-20T09:00", "2026-10-20T09:30")
        import_family(client, spaces, harness, write=True)
        two_way_on(client)
        first = find(client, spaces["Family"]["id"], "Standup", "10-20 09:00")
        res = client.patch(
            f"/api/v1/events/{first['event_id']}",
            params={"scope": "all", "occurrence_start": first["occurrence_start"]},
            json={"rrule": "FREQ=MONTHLY;BYMONTHDAY=20,27"},
            headers=rev(first["revision"]),
        )
        assert res.json()["error"]["code"] == "UNSUPPORTED_RRULE"
