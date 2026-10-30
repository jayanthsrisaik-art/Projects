"""Scripted walkthrough of the HTTP API against a throwaway database.

Output is deterministic and checked in as samples/demo_output.txt (a test diffs it).
Run: ``python scripts/demo.py``.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from flagdeck.app import create_app  # noqa: E402
from flagdeck.seed import seed  # noqa: E402


def run() -> str:
    lines: list[str] = []
    out = lines.append
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "demo.db")
        out(seed(db, str(ROOT / "samples/flags.json")))
        with TestClient(create_app(db)) as c:
            out("\n# evaluate (prod)")
            for ctx in json.loads((ROOT / "samples/contexts.json").read_text())[:6]:
                res = c.post("/api/evaluate", json={"env": "prod", "context": ctx}).json()
                cells = []
                for key, r in res.items():
                    why = r["reason"].get("rule_id") or r["reason"].get("prerequisite") or ""
                    val = r["value"]["color"] if isinstance(r["value"], dict) else r["value"]
                    cells.append(f"{key}={json.dumps(val)}[{r['reason']['kind']}{':' + why if why else ''}]")
                out(f"{ctx['key']:<10} " + "  ".join(cells))

            out("\n# preview: new-checkout prod fallthrough 20% -> 50%")
            row = c.get("/api/flags/new-checkout").json()
            draft = row["flag"]
            draft["environments"]["prod"]["fallthrough"] = {"rollout": {"bucket_by": "key", "weights": [50000, 50000]}}
            p = c.post("/api/flags/new-checkout/preview", json={"env": "prod", "draft": draft, "sample_size": 5000}).json()
            out(f"changed {p['changed']}/{p['total']}  before={p['before']}  after={p['after']}")
            flips = {(x["before"]["value"], x["after"]["value"]) for x in p["examples"]}
            out(f"flip directions among examples: {sorted(flips)}")

            out("\n# optimistic concurrency")
            v = row["version"]
            r1 = c.put("/api/flags/new-checkout", json=draft, headers={"If-Match": f'"{v}"', "X-Actor": "ana"})
            out(f"ana saves with If-Match {v}: HTTP {r1.status_code}, now v{r1.json()['version']}")
            stale = {**draft, "description": "bo's edit"}
            r2 = c.put("/api/flags/new-checkout", json=stale, headers={"If-Match": f'"{v}"', "X-Actor": "bo"})
            out(f"bo saves with If-Match {v}: HTTP {r2.status_code} {r2.json()}")

            out("\n# validation")
            bad = {**draft, "prerequisites": [{"key": "one-click-reorder", "variation": 1}]}
            r3 = c.put("/api/flags/new-checkout", json=bad, headers={"If-Match": '"2"'})
            out(f"cycle: HTTP {r3.status_code} {r3.json()['detail']}")
            r4 = c.delete("/api/flags/new-checkout", headers={"If-Match": '"2"'})
            out(f"delete prerequisite: HTTP {r4.status_code} {r4.json()}")

            out("\n# audit (newest first)")
            for a in c.get("/api/flags/new-checkout/audit").json():
                out(f"v{a['version']} {a['action']:<6} by {a['actor']:<5} {len(a['diff'])} change(s)"
                    + (f": {a['diff'][0]['path']} {json.dumps(a['diff'][0]['before'])} -> {json.dumps(a['diff'][0]['after'])}"
                       if a["action"] == "update" else ""))

            tag = c.get("/api/sdk/prod").headers["etag"]
            out(f"\n# sdk: GET /api/sdk/prod with If-None-Match -> HTTP {c.get('/api/sdk/prod', headers={'If-None-Match': tag}).status_code}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    text = run()
    print(text, end="")
    if "--write" in sys.argv:
        (ROOT / "samples/demo_output.txt").write_text(text, encoding="utf-8")
