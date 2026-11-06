"""Replay a scenario file through the HTTP API (in-process) and print a report.

    python -m tallyup.demo samples/scenario.json
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .api import create_app


def _split(spec: dict, ids: dict[str, int]) -> dict:
    if spec["kind"] == "equal":
        return {"kind": "equal", "parts": [{"member_id": ids[n]} for n in spec["members"]]}
    return {"kind": spec["kind"], "parts": [{"member_id": ids[n], "value": v} for n, v in spec["values"].items()]}


def run(scenario: dict, db_path: str) -> list[str]:
    c = TestClient(create_app(db_path))
    out: list[str] = []
    g = c.post("/api/groups", json=scenario["group"]).raise_for_status().json()
    gid = g["id"]
    ids = {n: c.post(f"/api/groups/{gid}/members", json={"name": n}).raise_for_status().json()["id"]
           for n in scenario["members"]}
    names = {v: k for k, v in ids.items()}
    out.append(f"group: {g['name']} ({g['currency']}), members: {', '.join(scenario['members'])}")
    bodies, created = {}, {}
    for i, e in enumerate(scenario["expenses"]):
        body = {"description": e["description"], "payer_id": ids[e["payer"]], "amount": e["amount"],
                "split": _split(e["split"], ids)}
        bodies[e["description"]] = (body, f"exp-{i}")
        r = c.post(f"/api/groups/{gid}/expenses", json=body, headers={"Idempotency-Key": f"exp-{i}"})
        r.raise_for_status()
        x = r.json()
        created[e["description"]] = x
        shares = ", ".join(f"{names[s['member_id']]} {s['amount']}" for s in x["shares"])
        out.append(f"+ {x['description']:<10} {x['amount']:>7} paid by {e['payer']:<4} [{e['split']['kind']}] {shares}")
    for rt in scenario.get("retries", []):
        body, key = bodies[rt["expense"]]
        r = c.post(f"/api/groups/{gid}/expenses", json=body, headers={"Idempotency-Key": key})
        out.append(f"retry {rt['expense']}: status {r.status_code}, replayed={r.headers.get('Idempotent-Replayed') == 'true'}, "
                   f"same id={r.json()['id'] == created[rt['expense']]['id']}")
    for ed in scenario.get("edits", []):
        x = created[ed["expense"]]
        body, _ = bodies[ed["expense"]]
        new = {**body, "amount": ed["amount"]}
        r1 = c.put(f"/api/expenses/{x['id']}", json=new, headers={"If-Match": f'"{x["version"]}"'})
        r2 = c.put(f"/api/expenses/{x['id']}", json=new, headers={"If-Match": f'"{x["version"]}"'})
        shares = ", ".join(f"{names[s['member_id']]} {s['amount']}" for s in r1.json()["shares"])
        out.append(f"edit {ed['expense']} -> {ed['amount']}: status {r1.status_code} (v{r1.json()['version']}) {shares}; "
                   f"stale retry status {r2.status_code}")
    n_exp = len(c.get(f"/api/groups/{gid}/expenses").json())
    out.append(f"expenses stored: {n_exp}")
    bal = c.get(f"/api/groups/{gid}/balances").json()
    out.append("balances:")
    for b in bal["balances"]:
        out.append(f"  {b['name']:<4} {b['balance']:>8}   rounding drift {b['rounding_drift_minor']:+.3f}")
    out.append(f"  sum (minor units): {bal['sum_minor']}")
    for method in ("greedy", "optimal"):
        p = c.get(f"/api/groups/{gid}/settle", params={"method": method}).json()
        out.append(f"settle ({p['method']}): {p['transfer_count']} transfers")
        for t in p["transfers"]:
            out.append(f"  {t['from_name']} -> {t['to_name']} {t['amount']}")
    plan = c.get(f"/api/groups/{gid}/settle").json()
    for t in plan["transfers"]:
        c.post(f"/api/groups/{gid}/payments", json={k: t[k] for k in ("from_member", "to_member", "amount")}).raise_for_status()
    after = c.get(f"/api/groups/{gid}/balances").json()
    out.append("after recording the optimal plan: " +
               ", ".join(f"{b['name']} {b['balance']}" for b in after["balances"]))
    return out


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0] if argv else Path(__file__).resolve().parent.parent / "samples" / "scenario.json")
    with tempfile.TemporaryDirectory() as d:
        print("\n".join(run(json.loads(path.read_text()), str(Path(d) / "demo.db"))))


if __name__ == "__main__":
    main()
