"""Every route rejects callers that are not the Agenda frontend (spec §06, §16)."""

from tests.conftest import AUTH, PORT, TOKEN


def test_health_is_public(client):
    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").status_code == 200


def test_missing_secret_is_rejected(client):
    res = client.get("/api/v1/notes")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
    assert res.json()["request_id"].startswith("req_")


def test_wrong_secret_is_rejected(client):
    res = client.get("/api/v1/notes", headers={"Authorization": "Bearer " + "b" * 64})
    assert res.status_code == 401


def test_secret_in_wrong_scheme_is_rejected(client):
    res = client.get("/api/v1/notes", headers={"Authorization": TOKEN})
    assert res.status_code == 401


def test_bad_host_is_rejected_before_auth(client):
    res = client.get("/api/v1/notes", headers={**AUTH, "Host": "evil.example"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "BAD_HOST"


def test_wrong_port_in_host_is_rejected(client):
    res = client.get("/health/live", headers={"Host": f"127.0.0.1:{PORT + 1}"})
    assert res.status_code == 400


def test_preflight_from_tauri_origin_is_allowed(client):
    res = client.options(
        "/api/v1/notes",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_preflight_from_other_origin_is_refused(client):
    res = client.options(
        "/api/v1/notes",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_dev_origin_only_allowed_in_dev(client):
    res = client.options(
        "/api/v1/notes",
        headers={"Origin": "http://localhost:1420", "Access-Control-Request-Method": "GET"},
    )
    assert res.status_code == 400


def test_docs_disabled_outside_dev(client):
    assert client.get("/docs", headers=AUTH).status_code == 404
