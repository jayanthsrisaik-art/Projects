import random
from pathlib import Path

import pytest

from conftest import EXAMPLES
from helpers import random_spine, random_view
from pitjoin.engines import (asof_bisect_join, naive_latest_join, oracle_join, sql_join, sweep_join)
from pitjoin.repo import MICROS, FeatureRow, FeatureView, load_entities, load_repo
from pitjoin.training import build_training_set, write_csv

ENGINES = [sweep_join, sql_join, oracle_join]

@pytest.fixture(scope="module")
def example():
    return load_repo(EXAMPLES / "feature_repo.json"), load_entities(EXAMPLES / "transactions.csv")

def _by_txn(header, rows, col):
    i = header.index(col)
    return {r[0]: r[i] for r in rows}

@pytest.mark.parametrize("engine", ["sweep", "sql", "oracle"])
def test_example_matches_expected_file(tmp_path, example, engine):
    views, ents = example
    header, rows = build_training_set(views, ents, engine)
    write_csv(tmp_path / "out.csv", header, rows)
    assert (tmp_path / "out.csv").read_text() == (EXAMPLES / "expected_training_set.csv").read_text()

def test_example_semantics_spelled_out(example):
    views, ents = example
    header, rows = build_training_set(views, ents)
    cnt = _by_txn(header, rows, "customer_daily__txn_count_7d")
    assert cnt["T1"] is None   # 03-01 row exists but lands at 06:00, label at 03:00
    assert cnt["T2"] == 3
    assert cnt["T4"] == 4      # correction for 03-03 lands 03-05 09:00: not visible yet
    assert cnt["T5"] == 6      # ... and visible afterwards
    assert cnt["T6"] is None   # newest row (03-03) older than the 3-day TTL at 03-07
    assert cnt["T7"] is None   # C2's 03-01 row arrived late (03-04)
    assert cnt["T8"] is None   # unknown customer
    risk = _by_txn(header, rows, "merchant_risk__risk_score")
    assert risk["T3"] == 0.30  # row at exactly the label time is included; +02:00 row is older
    assert risk["T9"] == 0.12  # M1's 03-04 10:00 update is after 08:00
    seg = _by_txn(header, rows, "customer_profile__segment")
    assert (seg["T3"], seg["T4"], seg["T8"]) == ("retail", "premium", None)

def test_event_mode_lets_late_and_corrected_rows_leak(example):
    views, ents = example
    header, rows = build_training_set(views, ents, "sweep", "event")
    cnt = _by_txn(header, rows, "customer_daily__txn_count_7d")
    assert (cnt["T1"], cnt["T4"], cnt["T7"]) == (3, 6, 1)

def make_view(rows, ttl_seconds=None):
    """rows: (key, ts_seconds, created_seconds); feature value = file position."""
    return FeatureView(name="v", entity="k", source=Path("x"), timestamp_field="ts",
                       features=[("x", "int")], created_timestamp_field="c",
                       ttl_seconds=ttl_seconds,
                       rows=[FeatureRow(i, k, ts * MICROS, c * MICROS, (i,)) for i, (k, ts, c) in enumerate(rows)])

@pytest.mark.parametrize("engine", ENGINES)
def test_tie_break_prefers_later_created_then_later_file_position(engine):
    v = make_view([("a", 10, 10), ("a", 10, 12), ("a", 10, 12), ("a", 9, 20)])
    assert engine(v, ["a", "a", "a"], [10 * MICROS, 12 * MICROS, 30 * MICROS]) == [0, 2, 2]

@pytest.mark.parametrize("engine", ENGINES)
def test_ttl_boundary_is_inclusive(engine):
    v = make_view([("a", 0, 0)], ttl_seconds=5)
    assert engine(v, ["a", "a"], [5 * MICROS, 5 * MICROS + 1]) == [0, None]

@pytest.mark.parametrize("engine", ENGINES)
def test_created_before_event_time_still_waits_for_event_time(engine):
    v = make_view([("a", 10, 3)])
    assert engine(v, ["a", "a"], [9 * MICROS, 10 * MICROS]) == [None, 0]

@pytest.mark.parametrize("engine", ENGINES)
def test_null_and_unknown_keys_get_no_row(engine):
    v = make_view([("a", 0, 0)])
    assert engine(v, [None, "b", "a"], [5 * MICROS] * 3) == [None, None, 0]

@pytest.mark.parametrize("engine", ENGINES)
def test_newer_event_hidden_falls_back_to_older_known_row(engine):
    # row 1 is newer by event time but not ingested yet; row 0 is the answer
    v = make_view([("a", 1, 1), ("a", 5, 50)])
    assert engine(v, ["a"], [10 * MICROS]) == [0]
    assert engine(v, ["a"], [10 * MICROS], "event") == [1]

def test_bad_mode_rejected():
    v = make_view([("a", 0, 0)])
    for engine in ENGINES:
        with pytest.raises(ValueError):
            engine(v, ["a"], [0], "processing")

@pytest.mark.parametrize("seed", range(40))
def test_engines_agree_with_oracle_on_random_data(seed):
    rng = random.Random(seed)
    keys = ["a", "b", "c"]
    view = random_view(rng, rng.randrange(0, 60), keys, rng.choice([None, 1, 4, 10]))
    ks, ts = random_spine(rng, 50, keys)
    for mode in ("knowledge", "event"):
        want = oracle_join(view, ks, ts, mode)
        assert sweep_join(view, ks, ts, mode) == want
        assert sql_join(view, ks, ts, mode) == want
    assert asof_bisect_join(view, ks, ts) == oracle_join(view, ks, ts, "event")

def test_naive_latest_ignores_label_time():
    v = make_view([("a", 0, 0), ("a", 100, 100)])
    assert naive_latest_join(v, ["a"], [1 * MICROS]) == [1]  # a value from the future


@pytest.mark.parametrize("engine", ENGINES)
def test_exact_duplicates_resolve_by_file_position_not_storage_order(engine):
    v = make_view([("a", 5, 5)] * 4)
    v.rows.reverse()  # storage order != file order; the answer must still be idx 3
    assert engine(v, ["a"], [9 * MICROS]) == [3]
