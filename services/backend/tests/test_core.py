"""Setup, profile, spaces, and people."""

import sqlite3

from tests.conftest import AUTH, complete_setup, rev, running


class TestSetup:
    def test_domain_routes_require_setup(self, client):
        assert client.get("/api/v1/setup/status", headers=AUTH).json()["data"] == {"completed": False}
        res = client.get("/api/v1/spaces", headers=AUTH)
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "SETUP_REQUIRED"

    def test_creates_owner_and_default_spaces(self, client, spaces):
        assert set(spaces) == {"Personal", "Work", "Family"}
        assert {s["role"] for s in spaces.values()} == {"owner"}
        me = client.get("/api/v1/users/me", headers=AUTH).json()["data"]
        assert me["display_name"] == "Pete"
        assert me["timezone"] == "America/New_York"

    def test_runs_only_once(self, client, spaces):
        res = client.post("/api/v1/setup", json={"display_name": "Again", "timezone": "UTC"}, headers=AUTH)
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "SETUP_ALREADY_DONE"

    def test_rejects_unknown_timezone(self, client):
        res = client.post(
            "/api/v1/setup", json={"display_name": "Pete", "timezone": "Mars/Olympus"}, headers=AUTH
        )
        assert res.status_code == 422

    def test_setup_survives_restart(self, settings):
        with running(settings) as first:
            complete_setup(first)
        with running(settings) as second:
            names = {s["name"] for s in second.get("/api/v1/spaces", headers=AUTH).json()["data"]}
        assert names == {"Personal", "Work", "Family"}


class TestProfile:
    def test_update(self, client, spaces):
        res = client.patch("/api/v1/users/me", json={"timezone": "Europe/London"}, headers=AUTH)
        assert res.json()["data"]["timezone"] == "Europe/London"


class TestSpaces:
    def test_create_and_get(self, client, spaces):
        created = client.post("/api/v1/spaces", json={"name": "Garden", "color": "#10b981"}, headers=AUTH)
        assert created.status_code == 201
        space = created.json()["data"]
        assert (space["type"], space["role"], space["revision"]) == ("custom", "owner", 1)
        assert client.get(f"/api/v1/spaces/{space['id']}", headers=AUTH).json()["data"]["name"] == "Garden"

    def test_patch_requires_if_match(self, client, spaces):
        space = spaces["Work"]
        res = client.patch(f"/api/v1/spaces/{space['id']}", json={"name": "Job"}, headers=AUTH)
        assert res.status_code == 428

    def test_patch_bumps_revision_and_detects_conflicts(self, client, spaces):
        space = spaces["Work"]
        ok = client.patch(f"/api/v1/spaces/{space['id']}", json={"name": "Job"}, headers=rev(1))
        assert ok.json()["data"]["revision"] == 2
        stale = client.patch(f"/api/v1/spaces/{space['id']}", json={"name": "Office"}, headers=rev(1))
        assert stale.status_code == 409
        assert stale.json()["error"] == {
            "code": "REVISION_CONFLICT",
            "message": "The resource changed after this version was loaded.",
            "details": {"resource_id": space["id"], "current_revision": 2},
        }

    def test_archive_hides_space_and_blocks_writes(self, client, spaces):
        space = spaces["Work"]
        assert client.delete(f"/api/v1/spaces/{space['id']}", headers=rev(1)).status_code == 204
        listed = {s["name"] for s in client.get("/api/v1/spaces", headers=AUTH).json()["data"]}
        assert "Work" not in listed
        everything = client.get("/api/v1/spaces?include_archived=true", headers=AUTH).json()["data"]
        assert "Work" in {s["name"] for s in everything}
        res = client.post(f"/api/v1/spaces/{space['id']}/people", json={"display_name": "Sam"}, headers=AUTH)
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "SPACE_ARCHIVED"

    def test_unarchive(self, client, spaces):
        space_id = spaces["Work"]["id"]
        client.delete(f"/api/v1/spaces/{space_id}", headers=rev(1))
        res = client.patch(f"/api/v1/spaces/{space_id}", json={"archived": False}, headers=rev(2))
        assert res.json()["data"]["archived_at"] is None

    def test_hard_delete_removes_contents(self, client, spaces, settings):
        space_id = spaces["Family"]["id"]
        client.post(f"/api/v1/spaces/{space_id}/people", json={"display_name": "Kid"}, headers=AUTH)
        assert client.delete(f"/api/v1/spaces/{space_id}?hard=true", headers=rev(1)).status_code == 204
        assert client.get(f"/api/v1/spaces/{space_id}", headers=AUTH).status_code == 404
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM people").fetchone()[0] == 0
            actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
        assert actions == ["space.deleted"]

    def test_space_without_membership_is_invisible(self, client, spaces, settings):
        with sqlite3.connect(settings.db_path) as conn:
            owner = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute(
                "INSERT INTO spaces (id, owner_user_id, name, type, color, created_at, updated_at)"
                " VALUES ('00000000-0000-0000-0000-000000000001', ?, 'Hidden', 'custom', '#000', 'x', 'x')",
                (owner,),
            )
        res = client.get("/api/v1/spaces/00000000-0000-0000-0000-000000000001", headers=AUTH)
        assert res.status_code == 404

    def test_writes_go_to_the_outbox(self, client, spaces, settings):
        client.post("/api/v1/spaces", json={"name": "Garden"}, headers=AUTH)
        with sqlite3.connect(settings.db_path) as conn:
            types = [r[0] for r in conn.execute("SELECT event_type FROM outbox")]
        assert types == ["space.created"]


class TestPeople:
    def test_crud(self, client, spaces):
        space_id = spaces["Family"]["id"]
        person = client.post(
            f"/api/v1/spaces/{space_id}/people", json={"display_name": "Alex"}, headers=AUTH
        ).json()["data"]
        renamed = client.patch(f"/api/v1/people/{person['id']}", json={"display_name": "Lex"}, headers=rev(1))
        assert renamed.json()["data"]["display_name"] == "Lex"
        listed = client.get(f"/api/v1/spaces/{space_id}/people", headers=AUTH).json()["data"]
        assert [p["display_name"] for p in listed] == ["Lex"]
        assert client.delete(f"/api/v1/people/{person['id']}", headers=rev(1)).status_code == 409
        assert client.delete(f"/api/v1/people/{person['id']}", headers=rev(2)).status_code == 204
        assert client.get(f"/api/v1/spaces/{space_id}/people", headers=AUTH).json()["data"] == []

    def test_unknown_person_is_404(self, client, spaces):
        res = client.patch(
            "/api/v1/people/00000000-0000-0000-0000-000000000009", json={"display_name": "X"}, headers=rev(1)
        )
        assert res.status_code == 404
