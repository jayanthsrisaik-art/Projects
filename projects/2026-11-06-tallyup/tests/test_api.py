import threading

import pytest
from fastapi.testclient import TestClient

from tallyup.api import create_app


@pytest.fixture
def make_client(tmp_path):
    def make(**kw):
        return TestClient(create_app(str(tmp_path / f"t{len(list(tmp_path.iterdir()))}.db"), **kw))
    return make


@pytest.fixture
def client(make_client):
    return make_client()


def setup_group(client, names=("Ana", "Ben", "Chi"), currency="USD"):
    g = client.post("/api/groups", json={"name": "Trip", "currency": currency}).json()
    ids = [client.post(f"/api/groups/{g['id']}/members", json={"name": n}).json()["id"] for n in names]
    return g["id"], ids


def equal(ids):
    return {"kind": "equal", "parts": [{"member_id": m} for m in ids]}


def expense(client, gid, payer, amount, split, key=None, desc="x"):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/groups/{gid}/expenses",
                       json={"description": desc, "payer_id": payer, "amount": amount, "split": split},
                       headers=headers)


def balances(client, gid):
    body = client.get(f"/api/groups/{gid}/balances").json()
    assert body["sum_minor"] == 0
    return {b["member_id"]: b["balance_minor"] for b in body["balances"]}


def test_full_flow_settles_to_zero(client):
    gid, (a, b, c) = setup_group(client)
    assert expense(client, gid, a, "100.00", equal([a, b, c])).status_code == 201
    assert expense(client, gid, b, "45.50", {"kind": "shares", "parts": [
        {"member_id": a, "value": 1}, {"member_id": b, "value": 2}]}).status_code == 201
    assert expense(client, gid, c, "20.00", {"kind": "exact", "parts": [
        {"member_id": a, "value": "5.00"}, {"member_id": c, "value": "15.00"}]}).status_code == 201
    bal = balances(client, gid)
    # Ana: paid 10000, owes 3334 + 1516 + 500. Shares 1:2 of 4550 is 1516.67/3033.33;
    # Ana already got the odd cent on the first expense, so drift-aware rounding gives it to Ben.
    assert bal[a] == 10000 - 3334 - 1516 - 500
    assert bal[b] == 4550 - 3333 - 3034
    plan = client.get(f"/api/groups/{gid}/settle").json()
    assert plan["transfer_count"] <= plan["greedy_transfer_count"]
    for t in plan["transfers"]:
        r = client.post(f"/api/groups/{gid}/payments", json={
            "from_member": t["from_member"], "to_member": t["to_member"], "amount": t["amount"]})
        assert r.status_code == 201
    assert set(balances(client, gid).values()) == {0}
    assert client.get(f"/api/groups/{gid}/settle").json()["transfers"] == []


def test_idempotent_create_replays_and_detects_mismatch(client):
    gid, (a, b, _) = setup_group(client)
    r1 = expense(client, gid, a, "10.00", equal([a, b]), key="k1")
    r2 = expense(client, gid, a, "10.00", equal([a, b]), key="k1")
    assert r1.status_code == r2.status_code == 201
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get(f"/api/groups/{gid}/expenses").json()) == 1
    r3 = expense(client, gid, a, "11.00", equal([a, b]), key="k1")
    assert r3.status_code == 422 and "different request body" in r3.json()["detail"]
    # A failed validation does not burn the key.
    bad = expense(client, gid, a, "0.001", equal([a, b]), key="k2")
    assert bad.status_code == 422
    assert expense(client, gid, a, "1.00", equal([a, b]), key="k2").status_code == 201


def test_concurrent_retries_with_same_key_create_one_expense(client):
    gid, (a, b, _) = setup_group(client)
    results = []

    def worker():
        results.append(expense(client, gid, a, "33.33", equal([a, b]), key="same").json()["id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert len(client.get(f"/api/groups/{gid}/expenses").json()) == 1


def test_optimistic_concurrency(client):
    gid, (a, b, c) = setup_group(client)
    e = expense(client, gid, a, "30.00", equal([a, b, c])).json()
    url = f"/api/expenses/{e['id']}"
    body = {"description": "taxi", "payer_id": b, "amount": "31.00", "split": equal([a, b])}
    assert client.put(url, json=body).status_code == 428
    assert client.put(url, json=body, headers={"If-Match": "nope"}).status_code == 400
    ok = client.put(url, json=body, headers={"If-Match": '"1"'})
    assert ok.status_code == 200 and ok.headers["ETag"] == '"2"'
    assert [s["amount_minor"] for s in ok.json()["shares"]] == [1550, 1550]
    stale = client.put(url, json=body, headers={"If-Match": '"1"'})
    assert stale.status_code == 412 and stale.headers["ETag"] == '"2"'
    assert client.delete(url, headers={"If-Match": '"1"'}).status_code == 412
    assert client.delete(url, headers={"If-Match": '"2"'}).status_code == 204
    assert client.get(url).status_code == 404
    assert set(balances(client, gid).values()) == {0}


def test_concurrent_edits_exactly_one_wins(client):
    gid, (a, b, c) = setup_group(client)
    e = expense(client, gid, a, "30.00", equal([a, b, c])).json()
    statuses = []

    def worker(i):
        body = {"description": f"edit {i}", "payer_id": a, "amount": f"{40 + i}.00", "split": equal([a, b])}
        statuses.append(client.put(f"/api/expenses/{e['id']}", json=body, headers={"If-Match": '"1"'}).status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200] + [412] * 5
    assert client.get(f"/api/expenses/{e['id']}").json()["version"] == 2


def test_input_validation(client):
    gid, (a, b, _) = setup_group(client)
    r = client.post(f"/api/groups/{gid}/expenses",
                    json={"description": "x", "payer_id": a, "amount": 12.3, "split": equal([a, b])})
    assert r.status_code == 422  # JSON numbers refused: no float money
    assert expense(client, gid, 999, "1.00", equal([a])).status_code == 422
    assert expense(client, gid, a, "1.00", equal([a, 999])).status_code == 422
    assert client.post(f"/api/groups/{gid}/payments", json={"from_member": a, "to_member": a, "amount": "1.00"}).status_code == 422
    assert client.post("/api/groups", json={"name": "x", "currency": "XYZ"}).status_code == 422
    assert client.post(f"/api/groups/{gid}/members", json={"name": "Ana"}).status_code == 422
    assert client.get("/api/groups/999").status_code == 404
    assert expense(client, 999, a, "1.00", equal([a])).status_code == 404


def test_zero_decimal_currency(client):
    gid, (a, b, c) = setup_group(client, currency="JPY")
    assert expense(client, gid, a, "1000.5", equal([a, b, c])).status_code == 422
    e = expense(client, gid, a, "1000", equal([a, b, c])).json()
    assert [s["amount"] for s in e["shares"]] == ["334", "333", "333"]


def test_drift_aware_rounding_vs_fixed_order(make_client):
    results = {}
    for mode in (True, False):
        client = make_client(drift_aware=mode)
        gid, ids = setup_group(client)
        for _ in range(30):
            assert expense(client, gid, ids[0], "1.00", equal(ids)).status_code == 201
        drift = client.get(f"/api/groups/{gid}/balances").json()["balances"]
        results[mode] = max(abs(d["rounding_drift_minor"]) for d in drift)
    assert results[True] < 1          # nobody is ever a cent ahead
    assert results[False] == pytest.approx(20)   # Ana pays +2/3 cent on all 30 expenses


def test_preview_matches_create(client):
    gid, (a, b, c) = setup_group(client)
    body = {"description": "x", "payer_id": a, "amount": "10.00", "split": equal([a, b, c])}
    expense(client, gid, a, "1.00", equal([a, b, c]))
    p = client.post(f"/api/groups/{gid}/preview", json=body).json()
    e = client.post(f"/api/groups/{gid}/expenses", json=body).json()
    assert p["shares"] == e["shares"]


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and 'id="app"' in r.text
