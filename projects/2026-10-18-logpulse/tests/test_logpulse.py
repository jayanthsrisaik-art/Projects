import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from logpulse.api import create_app
from logpulse.cli import run_eval
from logpulse.detect import build_matrix, detect, prf
from logpulse.generator import generate, plan_incidents
from logpulse.parsing import format_line, parse_line
from logpulse.store import connect, ingest, window_rows
from logpulse.templates import mine_template

SAMPLE = Path(__file__).parent.parent / "sample"


def test_template_masks_variables():
    a = mine_template("request GET /users/123 completed in 45ms status=200")
    b = mine_template("request GET /users/9 completed in 7ms status=200")
    assert a == b == "request GET /users/<*> completed in <*> status=<*>"
    assert mine_template("db connection pool exhausted active=95 waiting=3") != a


def test_parse_roundtrip_and_rejects():
    line = format_line(1767225603, "api", "INFO", "request GET /x/5 completed in 12ms status=200")
    r = parse_line(line)
    assert (r.service, r.level, r.latency_ms, r.ts) == ("api", "INFO", 12.0, 1767225603)
    assert parse_line("nonsense") is None
    assert parse_line(line.replace("INFO", "LOUD")) is None


def test_sample_expected_parsed():
    exp = json.loads((SAMPLE / "expected_parsed.json").read_text())
    got = []
    for line in (SAMPLE / "logs_sample.txt").read_text().splitlines():
        r = parse_line(line)
        got.append(None if r is None else {"service": r.service, "level": r.level,
                                           "template": mine_template(r.msg), "latency_ms": r.latency_ms})
    assert got == exp


def test_generator_deterministic_and_incidents_disjoint():
    assert generate(600, 3)[0] == generate(600, 3)[0]
    inc = plan_incidents(4320, 7)
    for a, b in zip(inc, inc[1:]):
        assert a.start_min + a.length <= b.start_min


def test_ingest_counts_and_first_seen():
    con = connect()
    res = ingest(con, ["bad", format_line(100, "api", "INFO", "x 1"), format_line(50, "api", "INFO", "x 2")])
    assert res == {"ingested": 2, "rejected": 1}
    assert con.execute("SELECT first_seen FROM templates").fetchone()[0] == 50


def test_novelty_feature_uses_only_training_vocabulary():
    con = connect()
    ingest(con, [format_line(60, "api", "INFO", "early event"),
                 format_line(6000, "api", "INFO", "late event"),
                 format_line(6010, "api", "INFO", "early event")])
    # Split at t=3600: "late event" is unseen in training -> new; "early event" is known.
    rows = {r["win"]: r["n_new"] for r in window_rows(con, known_before=3600)}
    assert rows[1] == 0 and rows[100] == 1
    # Split after everything was first seen: nothing is new.
    assert sum(r["n_new"] for r in window_rows(con, known_before=9000)) == 0


def test_count_ratio_is_causal():
    lines, _ = generate(300, 1)
    con = connect(); ingest(con, lines)
    rows = window_rows(con, known_before=0)
    _, X = build_matrix(rows)
    # Changing a future window's volume must not change earlier features.
    rows2 = [dict(r) for r in rows]; rows2[-1]["n"] *= 5; rows2[-1]["n_err"] = 0
    _, X2 = build_matrix(rows2)
    assert np.allclose(X[:-1], X2[:-1])


def test_prf():
    y = np.array([1, 1, 0, 0], bool); p = np.array([1, 0, 1, 0], bool)
    m = prf(y, p)
    assert (m["tp"], m["fp"], m["fn"]) == (1, 1, 1) and m["f1"] == 0.5


def test_detect_split_is_chronological():
    lines, _ = generate(1000, 2)
    con = connect(); ingest(con, lines)
    d = detect(con, train_fraction=0.6)
    assert d.windows.min() >= d.split_win
    assert 350 < len(d.windows) < 450


def test_end_to_end_beats_baseline_on_ap_and_recall():
    out = run_eval(4320, 7, quantile=0.95)
    assert out["iforest"]["incidents_detected"] > out["zscore"]["incidents_detected"]
    assert out["iforest"]["average_precision"] > out["zscore"]["average_precision"]


def test_matches_recorded_sample_eval():
    exp = json.loads((SAMPLE / "expected_eval.json").read_text())
    got = run_eval(4320, 7)
    assert got["lines"] == exp["lines"]
    for m in ("zscore", "iforest"):
        assert abs(got[m]["average_precision"] - exp[m]["average_precision"]) < 0.05


def test_api_flow():
    c = TestClient(create_app())
    lines, _ = generate(300, 5)
    r = c.post("/ingest", json={"lines": lines + ["junk"]})
    assert r.json()["rejected"] == 1
    assert c.get("/templates?limit=3").json()[0]["count"] > 0
    assert c.get("/anomalies?train_fraction=0.99").status_code == 422
    a = c.get("/anomalies").json()
    assert "split_window" in a and isinstance(a["flagged"], list)


def test_api_rejects_tiny_data():
    c = TestClient(create_app())
    c.post("/ingest", json={"lines": [format_line(0, "api", "INFO", "x 1")]})
    assert c.get("/anomalies").status_code == 409
