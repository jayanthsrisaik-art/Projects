import threading

from fastapi.testclient import TestClient

from flagdeck.app import create_app

BASIC = {"key": "dark-mode", "description": "Dark theme", "variations": [False, True],
         "environments": {"prod": {"on": False, "fallthrough": {"variation": 1}}}}


def create(client, body=BASIC, actor="ana"):
    r = client.post("/api/flags", json=body, headers={"X-Actor": actor, "X-Comment": "initial"})
    assert r.status_code == 201, r.text
    return r


def test_create_get_list(client):
    r = create(client)
    assert r.headers["etag"] == '"1"'
    assert r.json()["flag"]["environments"]["dev"]["on"] is False  # defaulted env
    assert client.get("/api/flags/dark-mode").json()["version"] == 1
    assert [row["flag"]["key"] for row in client.get("/api/flags").json()] == ["dark-mode"]
    assert client.post("/api/flags", json=BASIC).status_code == 409
    assert client.get("/api/flags/nope").status_code == 404


def test_invalid_flags_return_422_with_messages(client):
    bad = {**BASIC, "environments": {"prod": {"fallthrough": {"rollout": {"weights": [1, 2]}}}}}
    r = client.post("/api/flags", json=bad)
    assert r.status_code == 422
    assert any("sum to 3" in m for m in r.json()["detail"])
    r = client.post("/api/flags", json={**BASIC, "key": "Has Spaces"})
    assert r.status_code == 422 and any("key" in m for m in r.json()["detail"])


def test_update_requires_if_match_and_rejects_stale_versions(client):
    create(client)
    doc = client.get("/api/flags/dark-mode").json()["flag"]
    doc["description"] = "v2"
    assert client.put("/api/flags/dark-mode", json=doc).status_code == 428
    assert client.put("/api/flags/dark-mode", json=doc, headers={"If-Match": "abc"}).status_code == 400
    ok = client.put("/api/flags/dark-mode", json=doc, headers={"If-Match": '"1"', "X-Actor": "bo"})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.headers["etag"] == '"2"'
    doc["description"] = "v3 from a stale tab"
    stale = client.put("/api/flags/dark-mode", json=doc, headers={"If-Match": '"1"'})
    assert stale.status_code == 412
    assert stale.json()["current_version"] == 2
    assert client.get("/api/flags/dark-mode").json()["flag"]["description"] == "v2"


def test_noop_save_does_not_bump_version_or_audit(client):
    create(client)
    doc = client.get("/api/flags/dark-mode").json()["flag"]
    r = client.put("/api/flags/dark-mode", json=doc, headers={"If-Match": '"1"'})
    assert r.json()["version"] == 1 and r.json()["unchanged"] is True
    assert len(client.get("/api/flags/dark-mode/audit").json()) == 1


def test_key_cannot_change_on_update(client):
    create(client)
    r = client.put("/api/flags/dark-mode", json={**BASIC, "key": "light-mode"}, headers={"If-Match": "1"})
    assert r.status_code == 422


def test_toggle_and_audit_trail(client):
    create(client)
    r = client.post("/api/flags/dark-mode/toggle", json={"env": "prod", "on": True, "comment": "launch"},
                    headers={"If-Match": '"1"', "X-Actor": "cy"})
    assert r.status_code == 200 and r.json()["flag"]["environments"]["prod"]["on"] is True
    assert client.post("/api/flags/dark-mode/toggle", json={"env": "qa", "on": True},
                       headers={"If-Match": '"2"'}).status_code == 404
    audit = client.get("/api/flags/dark-mode/audit").json()
    assert [(a["version"], a["action"], a["actor"]) for a in audit] == [(2, "toggle", "cy"), (1, "create", "ana")]
    assert audit[0]["comment"] == "launch"
    assert audit[0]["diff"] == [{"path": "environments.prod.on", "op": "changed", "before": False, "after": True}]


def test_concurrent_writers_exactly_one_wins_per_version(client):
    create(client)
    app = client.app
    results = []
    barrier = threading.Barrier(8)

    def writer(i):
        c = TestClient(app)
        doc = c.get("/api/flags/dark-mode").json()["flag"]
        doc["description"] = f"writer {i}"
        barrier.wait()
        results.append(c.put("/api/flags/dark-mode", json=doc, headers={"If-Match": '"1"'}).status_code)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] + [412] * 7
    assert client.get("/api/flags/dark-mode").json()["version"] == 2
    assert len(client.get("/api/flags/dark-mode/audit").json()) == 2


def test_two_processes_share_db_safely(tmp_path):
    db = str(tmp_path / "shared.db")
    a, b = TestClient(create_app(db)), TestClient(create_app(db))
    create(a)
    doc = a.get("/api/flags/dark-mode").json()["flag"]
    assert a.put("/api/flags/dark-mode", json={**doc, "description": "A"}, headers={"If-Match": "1"}).status_code == 200
    assert b.put("/api/flags/dark-mode", json={**doc, "description": "B"}, headers={"If-Match": "1"}).status_code == 412


def test_prerequisite_cycle_rejected_via_api(seeded):
    doc = seeded.get("/api/flags/new-checkout").json()
    flag = doc["flag"]
    flag["prerequisites"] = [{"key": "one-click-reorder", "variation": 1}]
    r = seeded.put("/api/flags/new-checkout", json=flag, headers={"If-Match": str(doc["version"])})
    assert r.status_code == 422
    assert r.json()["detail"] == ["prerequisite cycle: new-checkout -> one-click-reorder -> new-checkout"]


def test_delete_blocked_while_flag_is_a_prerequisite(seeded):
    v = seeded.get("/api/flags/new-checkout").json()["version"]
    r = seeded.delete("/api/flags/new-checkout", headers={"If-Match": str(v)})
    assert r.status_code == 409 and r.json()["dependents"] == ["one-click-reorder"]
    child_v = seeded.get("/api/flags/one-click-reorder").json()["version"]
    assert seeded.delete("/api/flags/one-click-reorder", headers={"If-Match": "99"}).status_code == 412
    assert seeded.delete("/api/flags/one-click-reorder", headers={"If-Match": str(child_v)}).status_code == 204
    assert seeded.delete("/api/flags/new-checkout", headers={"If-Match": str(v)}).status_code == 204
    audit = seeded.get("/api/flags/new-checkout/audit").json()
    assert audit[0]["action"] == "delete"


def test_evaluate_endpoint(seeded):
    r = seeded.post("/api/evaluate", json={"env": "prod", "context": {"key": "dan", "email": "dan@acme.com", "country": "FR"}})
    body = r.json()
    assert body["new-checkout"] == {"value": True, "variation": 1,
                                    "reason": {"kind": "RULE_MATCH", "rule_index": 0, "rule_id": "staff"}}
    assert seeded.post("/api/evaluate", json={"env": "prod", "context": {"email": "x"}}).status_code == 422
    assert seeded.post("/api/evaluate", json={"env": "staging", "context": {"key": "x"}}).status_code == 404


def test_sdk_payload_etag_and_304(seeded):
    r = seeded.get("/api/sdk/prod")
    assert r.status_code == 200
    tag = r.headers["etag"]
    payload = r.json()
    assert set(payload["flags"]["new-checkout"]["environments"]) == {"prod"}  # other envs not leaked
    assert seeded.get("/api/sdk/prod", headers={"If-None-Match": tag}).status_code == 304
    v = seeded.get("/api/flags/checkout-banner").json()["version"]
    seeded.post("/api/flags/checkout-banner/toggle", json={"env": "prod", "on": False}, headers={"If-Match": str(v)})
    r2 = seeded.get("/api/sdk/prod", headers={"If-None-Match": tag})
    assert r2.status_code == 200 and r2.headers["etag"] != tag
    # A dev-only change does not invalidate the prod payload.
    tag2 = r2.headers["etag"]
    seeded.post("/api/flags/checkout-banner/toggle", json={"env": "dev", "on": True}, headers={"If-Match": str(v + 1)})
    assert seeded.get("/api/sdk/prod", headers={"If-None-Match": tag2}).status_code == 304


def test_preview_reports_blast_radius(seeded):
    row = seeded.get("/api/flags/new-checkout").json()
    draft = row["flag"]
    draft["environments"]["prod"]["fallthrough"] = {"rollout": {"bucket_by": "key", "weights": [50_000, 50_000]}}
    r = seeded.post("/api/flags/new-checkout/preview", json={"env": "prod", "draft": draft, "sample_size": 4000}).json()
    assert r["total"] == 4000
    # Moving 20% -> 50% only ever turns users on, and only fallthrough users are affected.
    assert r["changed"] > 0
    assert all(x["before"]["value"] is False and x["after"]["value"] is True for x in r["examples"])
    assert all(x["after"]["reason"]["kind"] == "FALLTHROUGH" for x in r["examples"])
    assert r["after"]["true"] - r["before"]["true"] == r["changed"]
    # Nothing is persisted by a preview.
    assert seeded.get("/api/flags/new-checkout").json()["version"] == row["version"]


def test_preview_with_explicit_contexts_and_invalid_draft(seeded):
    draft = seeded.get("/api/flags/new-checkout").json()["flag"]
    draft["environments"]["prod"]["on"] = False
    ctxs = [{"key": "qa-alice"}, {"key": "vip-carol"}]
    r = seeded.post("/api/flags/new-checkout/preview", json={"env": "prod", "draft": draft, "contexts": ctxs}).json()
    assert (r["total"], r["changed"]) == (2, 1)  # alice loses her target; carol was already false
    draft["environments"]["prod"]["off_variation"] = 7
    bad = seeded.post("/api/flags/new-checkout/preview", json={"env": "prod", "draft": draft})
    assert bad.status_code == 422


def test_ui_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "flagdeck" in r.text and "/dist/main.js" in r.text
    assert client.get("/styles.css").status_code == 200
