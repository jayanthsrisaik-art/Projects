from conftest import EXAMPLES
from pitjoin.repo import load_entities, load_repo
from pitjoin.training import leakage_report


def _report():
    return leakage_report(load_repo(EXAMPLES / "feature_repo.json"), load_entities(EXAMPLES / "transactions.csv"))


def test_asof_event_only_leaks_exactly_the_not_yet_ingested_rows():
    t = _report()["strategies"]["asof_event_only"]["total"]
    # T1 (06:00 batch), T4 (correction lands later), T7 (late batch) - all customer_daily
    assert t["not_yet_known"] == 3
    assert t["future_event"] == 0 and t["stale"] == 0
    assert t["rows_affected"] == 3 and t["value_mismatch"] == 3


def test_naive_latest_join_corrupts_every_row_but_one():
    r = _report()["strategies"]["naive_latest"]
    # only T5 happens to be after every relevant update
    assert r["total"]["rows_affected"] == 8
    assert r["total"]["rows_affected_pct"] == round(800 / 9, 2)
    assert r["per_view"]["customer_daily"]["stale"] == 1          # T6
    assert r["per_view"]["customer_daily"]["not_yet_known"] == 1  # T4
    assert r["per_view"]["merchant_risk"]["future_event"] >= 1


def test_point_in_time_join_against_itself_is_clean():
    rep = leakage_report(load_repo(EXAMPLES / "feature_repo.json"), load_entities(EXAMPLES / "transactions.csv"),
                         strategies=("sweep:knowledge", "sql"))
    for r in rep["strategies"].values():
        assert r["total"]["rows_affected"] == 0
