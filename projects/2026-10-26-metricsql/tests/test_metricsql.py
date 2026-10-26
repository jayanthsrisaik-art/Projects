import copy
import csv
import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from metricsql import JoinPathError, ModelError, QueryError, SemanticModel, compile_query, run_query
from metricsql.cli import main, parse_where
from metricsql.compiler import grain_sql
from metricsql.model import tokenize_derived
from metricsql.naive import compile_naive
from metricsql.reference import compute
from metricsql.synth import generate

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
MODEL_RAW = json.loads((SAMPLES / "model.json").read_text())


@pytest.fixture(scope="module")
def model():
    return SemanticModel.from_dict(MODEL_RAW)


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(":memory:")
    generate(c, seed=7)
    return c


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "shop.db"
    assert main(["demo-db", "--out", str(path)]) == 0
    return path


def as_dict(columns, rows, ndim):
    return {tuple(r[:ndim]): dict(zip(columns[ndim:], r[ndim:])) for r in rows}


def assert_matches_oracle(conn, model, metrics, group_by, channel_in=None):
    query = {"metrics": metrics, "group_by": group_by}
    if channel_in:
        query["where"] = [{"dimension": "orders__channel", "op": "in", "value": channel_in}]
    columns, rows = run_query(conn, model, query)
    got = as_dict(columns, rows, len(group_by))
    want = compute(conn, metrics, group_by, channel_in)
    assert len(rows) == len(got), "duplicate group rows"
    zero = {m: (None if m == "aov" else 0) for m in metrics}
    for key in set(got) | set(want):
        for m in metrics:
            a, b = got.get(key, zero)[m], want.get(key, zero)[m]
            if b is None:
                assert a is None, (key, m)
            else:
                assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (key, m)


# --- correctness against the independent oracle --------------------------------------------------

@pytest.mark.parametrize("group_by", [[], ["customers__region"], ["metric_time__month"], ["orders__channel"],
                                      ["customers__region", "metric_time__month"]])
def test_multi_dataset_metrics_match_oracle(conn, model, group_by):
    assert_matches_oracle(conn, model, ["revenue", "orders", "shipping_revenue", "refunds", "refund_count",
                                        "buyers", "aov", "net_revenue", "completed_revenue", "units"], group_by)


def test_query_filter_applies_to_every_dataset(conn, model):
    assert_matches_oracle(conn, model, ["revenue", "orders", "refunds", "net_revenue"],
                          ["customers__region"], channel_in=["web", "store"])


def test_null_region_is_its_own_group(conn, model):
    columns, rows = run_query(conn, model, {"metrics": ["orders"], "group_by": ["customers__region"]})
    regions = [r[0] for r in rows]
    assert None in regions and len(regions) == len(set(regions)) == 5
    assert sum(r[1] for r in rows) == 3000


def test_refunds_use_their_own_time_dimension(conn, model):
    # Refunds can land after the order month, including in 2026; orders never do.
    _, rows = run_query(conn, model, {"metrics": ["orders", "refunds"], "group_by": ["metric_time__year"]})
    by_year = {r[0]: r[1:] for r in rows}
    assert by_year["2026-01-01"][0] == 0 and by_year["2026-01-01"][1] > 0
    total_refunds = conn.execute("SELECT SUM(amount) FROM refunds").fetchone()[0]
    assert sum(v[1] for v in by_year.values()) == pytest.approx(total_refunds)


def test_no_group_by_returns_grand_totals(conn, model):
    _, rows = run_query(conn, model, {"metrics": ["revenue", "orders", "buyers", "new_customers"]})
    revenue, orders, buyers, customers = rows[0]
    assert revenue == pytest.approx(conn.execute("SELECT SUM(quantity*unit_price) FROM order_items").fetchone()[0])
    assert (orders, customers) == (3000, 200)
    assert buyers == conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]


def test_dimension_reachable_only_from_items(conn, model):
    _, rows = run_query(conn, model, {"metrics": ["revenue", "units"], "group_by": ["products__category"]})
    want = dict(((c, (r, u)) for c, r, u in conn.execute(
        "SELECT p.category, SUM(i.quantity*i.unit_price), SUM(i.quantity) FROM order_items i "
        "JOIN products p USING (product_id) GROUP BY 1")))
    assert {r[0]: (pytest.approx(r[1]), r[2]) for r in rows} == want


# --- fan-out protection -------------------------------------------------------------------------

def test_fanout_dimension_is_rejected(model):
    with pytest.raises(JoinPathError, match="fan-out"):
        compile_query(model, {"metrics": ["orders"], "group_by": ["products__category"]})


def test_fanout_through_derived_metric_is_rejected(model):
    # units_per_order needs orders.order_count, which cannot be split by product category
    with pytest.raises(JoinPathError):
        compile_query(model, {"metrics": ["units_per_order"], "group_by": ["products__category"]})


def test_filter_on_fanout_dimension_is_rejected(model):
    with pytest.raises(JoinPathError):
        compile_query(model, {"metrics": ["orders"],
                              "where": [{"dimension": "products__category", "op": "=", "value": "toys"}]})


def test_ambiguous_join_path_is_rejected():
    raw = copy.deepcopy(MODEL_RAW)
    # Diamond: order_items -> orders -> customers and order_items -> shipments -> customers.
    raw["datasets"]["shipments"] = {"primary_key": "shipment_id",
                                    "joins": [{"to": "customers", "foreign_key": "customer_id"}]}
    raw["datasets"]["order_items"]["joins"].append({"to": "shipments", "foreign_key": "shipment_id"})
    m = SemanticModel.from_dict(raw)
    with pytest.raises(JoinPathError, match="ambiguous"):
        compile_query(m, {"metrics": ["revenue"], "group_by": ["customers__region"]})


def test_naive_baseline_overcounts_on_the_same_query(conn, model):
    q = {"metrics": ["revenue", "orders", "shipping_revenue", "refunds"]}
    safe = conn.execute(*_sql(compile_query(model, q))).fetchone()
    naive = conn.execute(*_sql(compile_naive(model, q))).fetchone()
    assert safe[1] == 3000
    assert naive[1] > 3 * safe[1]  # each order counted once per item x refund row
    assert naive[0] > safe[0] and naive[2] > safe[2] and naive[3] > safe[3]


def _sql(compiled):
    return compiled.sql, compiled.params


# --- metric semantics ---------------------------------------------------------------------------

def test_ratio_with_zero_denominator_is_null_and_derived_uses_real_division():
    raw = {
        "datasets": {"t": {"primary_key": "id", "dimensions": {"g": {"column": "g"}},
                           "measures": {"a": {"agg": "sum", "column": "a"}, "b": {"agg": "sum", "column": "b"},
                                        "mx": {"agg": "max", "column": "a"}}}},
        "metrics": {"a": {"measure": "t.a"}, "b": {"measure": "t.b"}, "mx": {"measure": "t.mx"},
                    "r": {"type": "ratio", "numerator": "a", "denominator": "b"},
                    "d": {"type": "derived", "expr": "(a + 1) / b * -2"}},
    }
    m = SemanticModel.from_dict(raw)
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE t (id INTEGER, g TEXT, a INTEGER, b INTEGER);"
                    "INSERT INTO t VALUES (1,'x',5,2),(2,'y',3,0),(3,'z',NULL,NULL);")
    _, rows = run_query(c, m, {"metrics": ["r", "d", "mx"], "group_by": ["t__g"]})
    assert rows[0] == ("x", 2.5, -6.0, 5)
    assert rows[1] == ("y", None, None, 3)  # divide by zero -> NULL, not an error or 0
    assert rows[2] == ("z", None, None, None)  # sums of nothing are 0, max of nothing is NULL


def test_metric_level_filter_only_affects_that_metric(conn, model):
    _, rows = run_query(conn, model, {"metrics": ["revenue", "completed_revenue"]})
    total, completed = rows[0]
    want = conn.execute("SELECT SUM(i.quantity*i.unit_price) FROM order_items i JOIN orders o USING (order_id) "
                        "WHERE o.status = 'completed'").fetchone()[0]
    assert completed == pytest.approx(want) and total > completed


def truncate(day, grain):
    return sqlite3.connect(":memory:").execute(f"SELECT {grain_sql('d', grain)} FROM (SELECT ? AS d)", [day]).fetchone()[0]


@pytest.mark.parametrize("grain,expected", [("day", "2025-08-14"), ("week", "2025-08-11"), ("month", "2025-08-01"),
                                            ("quarter", "2025-07-01"), ("year", "2025-01-01")])
def test_time_grains(grain, expected):
    assert truncate("2025-08-14", grain) == expected


def test_week_grain_keeps_monday_and_handles_sunday():
    assert truncate("2025-08-11", "week") == "2025-08-11"  # Monday stays
    assert truncate("2025-08-17", "week") == "2025-08-11"  # Sunday belongs to the previous Monday
    assert truncate("2025-12-31", "quarter") == "2025-10-01"


def test_order_by_and_limit(conn, model):
    _, rows = run_query(conn, model, {"metrics": ["revenue"], "group_by": ["customers__region"],
                                      "order_by": ["-revenue"], "limit": 2})
    assert len(rows) == 2 and rows[0][1] >= rows[1][1]


def test_compiled_sql_is_deterministic(model):
    q = {"metrics": ["net_revenue", "aov"], "group_by": ["metric_time__month", "customers__region"]}
    assert compile_query(model, q).sql == compile_query(model, q).sql


# --- safety and validation ----------------------------------------------------------------------

def test_filter_values_are_bound_parameters(conn, model):
    evil = "x'; DROP TABLE orders; --"
    compiled = compile_query(model, {"metrics": ["orders"],
                                     "where": [{"dimension": "orders__channel", "op": "=", "value": evil}]})
    assert evil not in compiled.sql and evil in compiled.params
    assert conn.execute(compiled.sql, compiled.params).fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3000


@pytest.mark.parametrize("query,error", [
    ({"metrics": []}, "at least one metric"),
    ({"metrics": ["nope"]}, "unknown metric"),
    ({"metrics": ["orders"], "group_by": ['orders__status" --']}, "unknown dimension"),
    ({"metrics": ["orders"], "group_by": ["orders__status__month"]}, "not valid"),
    ({"metrics": ["orders"], "group_by": ["metric_time__fortnight"]}, "metric_time"),
    ({"metrics": ["orders"], "order_by": ["revenue"]}, "order_by"),
    ({"metrics": ["orders"], "limit": -1}, "limit"),
    ({"metrics": ["orders"], "where": [{"dimension": "orders__status", "op": "like", "value": "%"}]}, "op"),
    ({"metrics": ["orders"], "where": [{"dimension": "orders__status", "op": "in", "value": []}]}, "non-empty"),
    ({"metrics": ["revenue"], "group_by": ["metric_time__month"],
      "where": [{"dimension": "products__category", "op": "=", "value": ["a"]}]}, "strings or numbers"),
])
def test_bad_queries_are_rejected(model, query, error):
    with pytest.raises(QueryError, match=error):
        compile_query(model, query)


def test_metric_time_needs_a_time_dimension():
    raw = copy.deepcopy(MODEL_RAW)
    raw["metrics"]["product_count"] = {"measure": "products.n"}
    raw["datasets"]["products"]["measures"] = {"n": {"agg": "count", "column": "product_id"}}
    m = SemanticModel.from_dict(raw)
    with pytest.raises(QueryError, match="no time_dimension"):
        compile_query(m, {"metrics": ["product_count"], "group_by": ["metric_time__month"]})


def _broken(mutate):
    raw = copy.deepcopy(MODEL_RAW)
    mutate(raw)
    return raw


@pytest.mark.parametrize("mutate,error", [
    (lambda r: r["metrics"].update(a={"type": "derived", "expr": "b"}, b={"type": "derived", "expr": "a + 1"}),
     "cycle"),
    (lambda r: r["metrics"].update(x={"type": "derived", "expr": "revenue; DROP TABLE orders"}), "derived"),
    (lambda r: r["metrics"].update(x={"type": "derived", "expr": "revenue revenue"}), "derived"),
    (lambda r: r["metrics"].update(x={"type": "ratio", "numerator": "revenue", "denominator": "missing"}),
     "unknown metric"),
    (lambda r: r["metrics"].update(x={"measure": "orders.nope"}), "unknown measure"),
    (lambda r: r["datasets"]["orders"]["joins"].append({"to": "ghost", "foreign_key": "g"}), "unknown dataset"),
    (lambda r: r["datasets"]["orders"]["measures"].update(x={"agg": "median", "column": "a"}), "agg"),
    (lambda r: r["datasets"]["orders"]["dimensions"].update(bad__name={"column": "a"}), "invalid"),
    (lambda r: r["datasets"]["orders"].update(time_dimension="orders__status"), "time dimension"),
    (lambda r: r["datasets"]["customers"].update(time_dimension="orders__order_date"), "reachable"),
])
def test_invalid_models_are_rejected(mutate, error):
    with pytest.raises(ModelError, match=error):
        SemanticModel.from_dict(_broken(mutate))


def test_derived_tokenizer_accepts_numbers_and_unary_minus():
    kinds = [k for k, _ in tokenize_derived("-(revenue - 2.5) * 100 / orders")]
    assert kinds.count("metric") == 2 and kinds.count("number") == 2


# --- CLI and sample files -----------------------------------------------------------------------

def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(args)
    return code, out.getvalue(), err.getvalue()


def read_csv_text(text):
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.parametrize("name", ["revenue_by_region", "monthly_net_revenue", "category_completed"])
def test_sample_queries_reproduce_expected_output(db_path, name):
    code, out, _ = run_cli(["run", "--model", str(SAMPLES / "model.json"), "--db", str(db_path), "--format", "csv",
                            str(SAMPLES / "queries" / f"{name}.json")])
    assert code == 0
    got = read_csv_text(out)
    want = read_csv_text((SAMPLES / f"expected_{name}.csv").read_text())
    assert got[0] == want[0] and len(got) == len(want)
    for g_row, w_row in zip(got[1:], want[1:]):
        for g, w in zip(g_row, w_row):
            try:
                assert float(g) == pytest.approx(float(w), rel=1e-9)
            except ValueError:
                assert g == w


def test_sample_fanout_query_fails_with_expected_message(db_path):
    code, _, err = run_cli(["run", "--model", str(SAMPLES / "model.json"), "--db", str(db_path),
                            str(SAMPLES / "queries" / "fanout_rejected.json")])
    assert code == 2
    assert err.strip() == (SAMPLES / "expected_fanout_rejected.txt").read_text().strip()


def test_sample_sql_matches_expected():
    code, out, _ = run_cli(["run", "--model", str(SAMPLES / "model.json"), "--sql",
                            str(SAMPLES / "queries" / "category_completed.json")])
    assert code == 0 and out == (SAMPLES / "expected_category_completed.sql").read_text()


def test_cli_query_with_where_and_order(db_path):
    code, out, _ = run_cli(["query", "--model", str(SAMPLES / "model.json"), "--db", str(db_path),
                            "--metrics", "orders,aov", "--by", "orders__channel",
                            "--where", "customers__region is not null", "--order=-orders", "--format", "json"])
    rows = json.loads(out)
    assert code == 0 and [r["orders__channel"] for r in rows][0] == "web"
    assert rows[0]["orders"] >= rows[1]["orders"] >= rows[2]["orders"]


def test_cli_demo_db_refuses_to_overwrite(db_path):
    assert run_cli(["demo-db", "--out", str(db_path)])[0] == 1


def test_cli_opens_database_read_only(db_path):
    with sqlite3.connect(db_path) as c:
        before = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    run_cli(["query", "--model", str(SAMPLES / "model.json"), "--db", str(db_path), "--metrics", "orders"])
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before


@pytest.mark.parametrize("text,expected", [
    ("orders__status = completed", {"dimension": "orders__status", "op": "=", "value": "completed"}),
    ("orders__channel in web|app", {"dimension": "orders__channel", "op": "in", "value": ["web", "app"]}),
    ("orders__shipping >= 4.99", {"dimension": "orders__shipping", "op": ">=", "value": 4.99}),
    ("customers__region IS NULL", {"dimension": "customers__region", "op": "is null"}),
])
def test_parse_where(text, expected):
    assert parse_where(text) == expected


def test_synthetic_data_is_deterministic():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    generate(a, seed=3, orders=200)
    generate(b, seed=3, orders=200)
    dump = lambda c: list(c.execute("SELECT * FROM order_items ORDER BY item_id"))  # noqa: E731
    assert dump(a) == dump(b)
