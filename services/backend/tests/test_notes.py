from tests.conftest import AUTH, running


def test_create_then_list(client):
    res = client.post("/api/v1/notes", json={"body": "hello"}, headers=AUTH)
    assert res.status_code == 201
    created = res.json()["data"]
    assert created["body"] == "hello"

    listed = client.get("/api/v1/notes", headers=AUTH).json()["data"]
    assert [n["id"] for n in listed] == [created["id"]]


def test_validation_uses_error_envelope(client):
    res = client.post("/api/v1/notes", json={"body": ""}, headers=AUTH)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_FAILED"
    assert res.json()["error"]["details"]["fields"][0]["loc"] == ["body", "body"]


def test_unknown_route_uses_error_envelope(client):
    res = client.get("/api/v1/nope", headers=AUTH)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_note_survives_restart(settings):
    """The Phase 0 exit gate, minus the installer."""
    with running(settings) as first:
        note_id = first.post("/api/v1/notes", json={"body": "persist me"}, headers=AUTH).json()["data"]["id"]
    with running(settings) as second:
        bodies = {n["id"]: n["body"] for n in second.get("/api/v1/notes", headers=AUTH).json()["data"]}
    assert bodies == {note_id: "persist me"}


def test_wal_mode_is_on(client, settings):
    import sqlite3

    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
