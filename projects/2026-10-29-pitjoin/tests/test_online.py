import random

import pytest

from conftest import EXAMPLES
from helpers import random_view
from pitjoin import online
from pitjoin.engines import sweep_join
from pitjoin.repo import MICROS, FeatureRow, load_repo, parse_ts


@pytest.fixture
def views():
    return load_repo(EXAMPLES / "feature_repo.json")


def _state(conn, view):
    return conn.execute(f'SELECT k, idx FROM "online__{view}" ORDER BY k').fetchall()


def test_get_serves_latest_known_row_and_applies_ttl(views):
    conn = online.connect(":memory:")
    online.materialize(conn, views, parse_ts("2026-03-04T12:00:00Z"))
    cd = views["customer_daily"]
    got = online.get_online(conn, cd, ["C1", "C2", "C3"])
    assert got[0]["features"] == {"txn_count_7d": 4, "spend_7d": 150.5}  # correction not landed yet
    assert got[1]["features"] == {"txn_count_7d": 2, "spend_7d": 30.0}
    assert got[2]["idx"] is None
    later = online.get_online(conn, cd, ["C1"], now=parse_ts("2026-03-07T00:00:00Z"))
    assert later[0]["idx"] is None  # expired by TTL at serving time


def test_incremental_equals_one_shot_and_offline(views):
    end = parse_ts("2026-03-06T00:00:00Z")
    one = online.connect(":memory:")
    online.materialize(one, views, end)
    inc = online.connect(":memory:")
    for stamp in ("2026-02-25T00:00:00Z", "2026-03-02T06:00:00Z", "2026-03-04T06:00:00Z", "2026-03-06T00:00:00Z"):
        online.materialize(inc, views, parse_ts(stamp))
    for name in views:
        assert _state(one, name) == _state(inc, name)
    assert all(r["status"] == "ok" for r in online.skew_check(inc, views).values())


def test_watermark_cannot_move_backwards_but_full_rebuild_can(views):
    conn = online.connect(":memory:")
    online.materialize(conn, views, parse_ts("2026-03-05T12:00:00Z"))
    with pytest.raises(online.MaterializeError):
        online.materialize(conn, views, parse_ts("2026-03-04T12:00:00Z"))
    online.materialize(conn, views, parse_ts("2026-03-04T12:00:00Z"), full=True)
    got = online.get_online(conn, views["customer_daily"], ["C1"])
    assert got[0]["features"]["txn_count_7d"] == 4
    assert online.watermark(conn, "customer_daily") == parse_ts("2026-03-04T12:00:00Z")


def test_get_before_materialize_is_an_error(views):
    conn = online.connect(":memory:")
    with pytest.raises(online.MaterializeError):
        online.get_online(conn, views["merchant_risk"], ["M1"])
    assert online.skew_check(conn, views)["merchant_risk"]["status"] == "not_materialized"


def test_skew_check_catches_rows_backdated_behind_the_watermark(views):
    conn = online.connect(":memory:")
    online.materialize(conn, views, parse_ts("2026-03-04T12:00:00Z"))
    mr = views["merchant_risk"]
    # an upstream job appends a row whose created timestamp claims it landed before the watermark
    ts = parse_ts("2026-03-04T11:00:00Z")
    mr.rows.append(FeatureRow(len(mr.rows), "M1", ts, ts, (0.9, "high")))
    online.materialize(conn, views, parse_ts("2026-03-05T00:00:00Z"))  # incremental run misses it
    res = online.skew_check(conn, views)["merchant_risk"]
    assert res["status"] == "skew"
    assert res["mismatches"] == [{"key": "M1", "offline_idx": 4, "online_idx": 1}]
    online.materialize(conn, views, parse_ts("2026-03-05T00:00:00Z"), full=True)
    assert online.skew_check(conn, views)["merchant_risk"]["status"] == "ok"


@pytest.mark.parametrize("seed", range(25))
def test_random_incremental_schedules_match_offline_join(seed):
    rng = random.Random(seed)
    view = random_view(rng, 80, ["a", "b", "c", "d"], rng.choice([None, 3, 8]))
    views = {"v": view}
    conn = online.connect(":memory:")
    cuts = sorted(rng.sample(range(-5, 45), rng.randint(1, 6)))
    for c in cuts:
        online.materialize(conn, views, c * MICROS)
        keys = ["a", "b", "c", "d", "zz"]
        served = [g["idx"] for g in online.get_online(conn, view, keys)]
        assert served == sweep_join(view, keys, [c * MICROS] * len(keys))
