import pytest

from tests.conftest import AUTH, rev


def placement(key="a", **extra):
    return {
        "key": key,
        "widget": "agenda.lists",
        "version": 1,
        "config": {},
        "x": 0,
        "y": 0,
        "w": 4,
        "h": 3,
        **extra,
    }


@pytest.fixture
def family(spaces):
    return spaces["Family"]["id"]


def create(client, space_ids, **body):
    res = client.post(
        "/api/v1/dashboard-profiles", json={"name": "Home", "space_ids": space_ids, **body}, headers=AUTH
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


def test_create_and_list_in_order(client, spaces):
    first = create(client, [spaces["Personal"]["id"]], name="Me")
    second = create(client, [spaces["Family"]["id"], spaces["Work"]["id"]], name="Everything")
    listed = client.get("/api/v1/dashboard-profiles", headers=AUTH).json()["data"]
    assert [d["name"] for d in listed] == ["Me", "Everything"]
    assert second["position"] > first["position"]
    assert second["space_ids"] == [spaces["Family"]["id"], spaces["Work"]["id"]]


def test_layout_round_trips_and_bumps_revision(client, family):
    dash = create(client, [family])
    layout = [
        placement("tasks", widget="agenda.tasks-and-chores", config={"filter": "chores"}, w=8),
        placement("x", x=8),
    ]
    res = client.patch(f"/api/v1/dashboard-profiles/{dash['id']}", json={"layout": layout}, headers=rev(1))
    assert res.status_code == 200, res.text
    saved = res.json()["data"]
    assert saved["revision"] == 2
    assert saved["layout"][0]["config"] == {"filter": "chores"}
    stale = client.patch(f"/api/v1/dashboard-profiles/{dash['id']}", json={"name": "x"}, headers=rev(1))
    assert stale.status_code == 409


@pytest.mark.parametrize(
    "bad",
    [
        [placement(x=10, w=4)],  # past column 12
        [placement("same"), placement("same")],  # duplicate keys
        [placement(widget="Not A Widget!")],
        [placement(config={"blob": "x" * 20_000})],
        [placement(str(n)) for n in range(41)],
    ],
)
def test_layout_shape_is_enforced(client, family, bad):
    dash = create(client, [family])
    res = client.patch(f"/api/v1/dashboard-profiles/{dash['id']}", json={"layout": bad}, headers=rev(1))
    assert res.status_code == 422


def test_spaces_must_be_accessible(client, spaces):
    res = client.post(
        "/api/v1/dashboard-profiles",
        json={"name": "x", "space_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=AUTH,
    )
    assert res.json()["error"]["code"] == "UNKNOWN_SPACE"


def test_deleted_space_drops_out_of_dashboards(client, spaces):
    dash = create(client, [spaces["Family"]["id"], spaces["Work"]["id"]])
    client.delete(f"/api/v1/spaces/{spaces['Work']['id']}?hard=true", headers=rev(1))
    got = client.get(f"/api/v1/dashboard-profiles/{dash['id']}", headers=AUTH).json()["data"]
    assert got["space_ids"] == [spaces["Family"]["id"]]


def test_delete_dashboard_keeps_data(client, family):
    dash = create(client, [family])
    client.post(f"/api/v1/spaces/{family}/tasks", json={"title": "keep me"}, headers=AUTH)
    assert client.delete(f"/api/v1/dashboard-profiles/{dash['id']}", headers=rev(1)).status_code == 204
    assert client.get(f"/api/v1/dashboard-profiles/{dash['id']}", headers=AUTH).status_code == 404
    assert len(client.get(f"/api/v1/spaces/{family}/tasks", headers=AUTH).json()["data"]) == 1
