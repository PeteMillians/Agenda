import pytest

from tests.conftest import AUTH, rev


@pytest.fixture
def grocery(client, spaces):
    res = client.post(
        f"/api/v1/spaces/{spaces['Family']['id']}/lists", json={"name": "Groceries"}, headers=AUTH
    )
    assert res.status_code == 201
    return res.json()["data"]


def add(client, list_id, text, **extra):
    res = client.post(f"/api/v1/lists/{list_id}/items", json={"text": text, **extra}, headers=AUTH)
    assert res.status_code == 201, res.text
    return res.json()["data"]


def items(client, list_id):
    return client.get(f"/api/v1/lists/{list_id}", headers=AUTH).json()["data"]["items"]


def test_items_append_in_order(client, grocery):
    for text in ("milk", "eggs", "bread"):
        add(client, grocery["id"], text)
    assert [i["text"] for i in items(client, grocery["id"])] == ["milk", "eggs", "bread"]


def test_reorder_by_position(client, grocery):
    milk, eggs = add(client, grocery["id"], "milk"), add(client, grocery["id"], "eggs")
    client.patch(
        f"/api/v1/list-items/{eggs['id']}", json={"position": milk["position"] - 0.5}, headers=rev(1)
    )
    assert [i["text"] for i in items(client, grocery["id"])] == ["eggs", "milk"]


def test_counts_and_clear_checked(client, spaces, grocery):
    milk = add(client, grocery["id"], "milk")
    add(client, grocery["id"], "eggs")
    client.patch(f"/api/v1/list-items/{milk['id']}", json={"checked": True}, headers=rev(1))
    [summary] = client.get(f"/api/v1/spaces/{spaces['Family']['id']}/lists", headers=AUTH).json()["data"]
    assert (summary["item_count"], summary["checked_count"]) == (2, 1)

    cleared = client.post(f"/api/v1/lists/{grocery['id']}/clear-checked", headers=AUTH)
    assert cleared.json()["data"] == {"removed": 1}
    assert [i["text"] for i in items(client, grocery["id"])] == ["eggs"]


def test_item_revisions_are_independent(client, grocery):
    milk, eggs = add(client, grocery["id"], "milk"), add(client, grocery["id"], "eggs")
    assert (
        client.patch(f"/api/v1/list-items/{milk['id']}", json={"checked": True}, headers=rev(1)).status_code
        == 200
    )
    # ticking milk does not make an edit to eggs stale
    assert (
        client.patch(f"/api/v1/list-items/{eggs['id']}", json={"text": "6 eggs"}, headers=rev(1)).status_code
        == 200
    )


def test_delete_item_checks_revision(client, grocery):
    milk = add(client, grocery["id"], "milk")
    assert client.delete(f"/api/v1/list-items/{milk['id']}", headers=rev(9)).status_code == 409
    assert client.delete(f"/api/v1/list-items/{milk['id']}", headers=rev(1)).status_code == 204
    assert items(client, grocery["id"]) == []


def test_rename_and_archive(client, spaces, grocery):
    renamed = client.patch(f"/api/v1/lists/{grocery['id']}", json={"name": "Shopping"}, headers=rev(1))
    assert renamed.json()["data"]["name"] == "Shopping"
    assert client.delete(f"/api/v1/lists/{grocery['id']}", headers=rev(2)).status_code == 204
    assert client.get(f"/api/v1/spaces/{spaces['Family']['id']}/lists", headers=AUTH).json()["data"] == []


def test_items_of_unknown_list_are_404(client, spaces):
    res = client.post(
        "/api/v1/lists/00000000-0000-0000-0000-000000000000/items", json={"text": "x"}, headers=AUTH
    )
    assert res.status_code == 404
