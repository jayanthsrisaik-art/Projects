import json
import sqlite3
from pathlib import Path

import pytest

from settlement import analytics, generate, ingest, warehouse
from settlement.money import fmt, to_cents

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def data(tmp_path):
    generate.generate(tmp_path / "in")
    return tmp_path / "in"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def test_money():
    assert to_cents("12.34") == 1234 and fmt(5) == "0.05"
    for bad in ("12x.00", "1.234", "-1.00", ""):
        with pytest.raises(ValueError):
            to_cents(bad)


def test_pipeline_matches_committed_expected_report(data, conn):
    s = warehouse.run_pipeline(conn, data)
    exp = json.loads((SAMPLES / "expected_report.json").read_text())
    assert s["audit"] == exp["audit"] and s["facts_loaded"] == exp["fact_rows"]
    assert analytics.delinquency_by_product(conn, "2026-09-03") == exp["delinquency"]


def test_committed_sample_input_is_valid():
    assert all(ingest.verify_manifest(SAMPLES / "input").values())


def test_bad_row_redirected_not_fatal(data, conn):
    ingest.ingest(conn, data)
    rej = q(conn, "SELECT * FROM stg_rejects")
    assert len(rej) == 1 and rej[0]["source_file"] == "journal_20260902.txt" and "12x.00" in rej[0]["raw_line"]


def test_tampered_file_quarantined(data, conn):
    (data / "journal_20260901.txt").write_text("tampered")
    s = ingest.ingest(conn, data)
    assert s["quarantined"] == ["journal_20260901.txt"]
    assert q(conn, "SELECT COUNT(*) n FROM stg_journal WHERE batch_id LIKE '20260901%'")[0]["n"] == 0


def test_missing_file_counts_as_failed_checksum(data, conn):
    (data / "loans_20260831.txt").unlink()
    assert "loans_20260831.txt" in ingest.ingest(conn, data)["quarantined"]


def test_audit_flags_penny_difference_and_gates_facts(data, conn):
    s = warehouse.run_pipeline(conn, data)
    assert s["audit"]["20260903-ACH"] == "UNBALANCED"
    row = q(conn, "SELECT * FROM audit_batch WHERE batch_id='20260903-ACH'")[0]
    assert abs(row["diff_cents"]) == 1
    # held-back batches contribute nothing to the fact table
    assert q(conn, "SELECT COUNT(*) n FROM fact_member_transactions WHERE batch_id IN ('20260903-ACH','20260902-CORE')")[0]["n"] == 0
    # every loaded batch balances to the penny
    d = q(conn, """SELECT SUM(CASE WHEN entry_type='D' THEN amount_cents ELSE -amount_cents END) x
                   FROM fact_member_transactions GROUP BY batch_id""")
    assert all(r["x"] == 0 for r in d)
    assert len(q(conn, "SELECT * FROM exception_log")) == 2
    assert "2 settlement batch" in q(conn, "SELECT message FROM notification_log")[0]["message"]


def test_control_mismatch_detected(data, conn):
    ingest.ingest(conn, data)
    conn.execute("UPDATE stg_controls SET control_debit_cents = control_debit_cents + 100 WHERE batch_id='20260831-ACH'")
    warehouse.build_dimensions(conn)
    statuses = {a["batch_id"]: a["status"] for a in warehouse.audit_batches(conn)}
    assert statuses["20260831-ACH"] == "CONTROL_MISMATCH"


def test_scd2_history_for_moved_member(data, conn):
    warehouse.run_pipeline(conn, data)
    v = q(conn, "SELECT * FROM dim_member WHERE member_id='M001' ORDER BY valid_from")
    assert len(v) == 2
    assert v[0]["valid_to"] == v[1]["valid_from"] == "2026-09-02"   # contiguous, half-open
    assert [r["is_current"] for r in v] == [0, 1] and v[1]["valid_to"] == "9999-12-31"
    assert v[0]["region"] != v[1]["region"] == "West"
    # exactly one current row per business key
    assert q(conn, "SELECT COUNT(*) n FROM (SELECT member_id FROM dim_member WHERE is_current=1 GROUP BY 1 HAVING COUNT(*)>1)")[0]["n"] == 0


def test_within_band_score_change_does_not_create_version(conn):
    conn.executescript(ingest.STG)
    conn.executescript(warehouse.DDL)
    for day, score in (("2026-09-01", 700), ("2026-09-02", 710)):
        conn.execute("INSERT INTO stg_members VALUES (?,?,?,?,?,?,?)", (day, "M9", "B1", "North", score, "1 A St", 1))
    warehouse.build_dimensions(conn)
    assert len(q(conn, "SELECT * FROM dim_member")) == 1


def test_rerun_is_idempotent_and_replay_safe(data, conn):
    warehouse.run_pipeline(conn, data)
    snap = {t: q(conn, f"SELECT COUNT(*) n FROM {t}")[0]["n"]
            for t in ("dim_member", "dim_loan_account", "fact_member_transactions", "stg_journal")}
    s2 = warehouse.run_pipeline(conn, data)           # same files again
    assert s2["loaded"] == []
    warehouse.build_dimensions(conn)                  # replay of old snapshots must not reopen history
    for t, n in snap.items():
        assert q(conn, f"SELECT COUNT(*) n FROM {t}")[0]["n"] == n
    assert q(conn, "SELECT is_current FROM dim_member WHERE member_id='M001' AND valid_from='2026-08-31'")[0][0] == 0


def test_fact_uses_dimension_version_valid_on_txn_date(conn):
    conn.executescript(ingest.STG)
    conn.executescript(warehouse.DDL)
    for day, br in (("2026-09-01", "B1"), ("2026-09-03", "B2")):
        conn.execute("INSERT INTO stg_members VALUES (?,?,?,?,?,?,?)", (day, "M1", br, "R", 700, "a", 1))
    rows = [("B-1", "T1-D", "GL-CASH", None, "D", 500, "2026-09-02"), ("B-1", "T1-C", "SH-M1", "M1", "C", 500, "2026-09-02"),
            ("B-2", "T2-D", "GL-CASH", None, "D", 700, "2026-09-04"), ("B-2", "T2-C", "SH-M1", "M1", "C", 700, "2026-09-04")]
    conn.executemany("INSERT INTO stg_journal VALUES (?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO stg_controls VALUES (?,?,?)", [("B-1", 500, 500), ("B-2", 700, 700)])
    warehouse.build_dimensions(conn)
    warehouse.audit_batches(conn)
    warehouse.load_facts(conn)
    got = {r["txn_id"]: r["branch_id"] for r in q(
        conn, "SELECT f.txn_id, m.branch_id FROM fact_member_transactions f JOIN dim_member m USING (member_key)")}
    assert got == {"T1-C": "B1", "T2-C": "B2"}


def test_delinquency_ratio_hand_computed(conn):
    conn.executescript(warehouse.DDL)
    ins = "INSERT INTO dim_loan_account (loan_id,member_id,product,rate_tier,delinquency_bucket,balance_cents,row_hash,valid_from,valid_to,is_current) VALUES (?,?,?,?,?,?,?,?,?,1)"
    conn.executemany(ins, [("L1", "M", "Auto", "A", "current", 7000, "h", "2026-01-01", "9999-12-31"),
                           ("L2", "M", "Auto", "A", "30", 2000, "h", "2026-01-01", "9999-12-31"),
                           ("L3", "M", "Auto", "A", "90+", 1000, "h", "2026-01-01", "9999-12-31"),
                           ("L4", "M", "Auto", "A", "90+", 500, "h", "2025-01-01", "2026-01-01")])  # expired row ignored
    r = analytics.delinquency_by_product(conn, "2026-02-01")[0]
    assert (r["d30_plus"], r["d60_plus"], r["d90_plus"]) == (0.3, 0.1, 0.1)


def test_region_scoped_portfolio(data, conn):
    warehouse.run_pipeline(conn, data)
    everyone = analytics.branch_region_portfolio(conn, None)
    north = analytics.branch_region_portfolio(conn, "North")
    assert [r["region"] for r in north] == ["North"]
    assert north[0] == next(r for r in everyone if r["region"] == "North")
    assert sum(r["loans"] for r in everyone) == 14


def test_deposit_velocity_mom(data, conn):
    warehouse.run_pipeline(conn, data)
    v = analytics.deposit_velocity(conn)
    assert [x["month"] for x in v] == ["2026-08", "2026-09"] and v[0]["mom_change"] is None
    assert v[1]["mom_change"] == round((v[1]["deposits_cents"] - v[0]["deposits_cents"]) / v[0]["deposits_cents"], 4)
