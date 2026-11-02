import pytest

from refill.spec import SpecError, build_pipeline, parse_change
from helpers import EXAMPLES, example_pipeline, example_raw, mutated


def test_example_pipeline_topo_order():
    p = example_pipeline()
    idx = p.index
    for t in p.order:
        for d in p.tables[t].deps:
            if d.table != t:
                assert idx[d.table] < idx[t]
    assert len(p.all_nodes(derived_only=True)) == 92 * 5 + 3


def _expect_error(fn, match):
    raw = mutated(example_raw(), fn)
    with pytest.raises(SpecError, match=match):
        build_pipeline(raw, EXAMPLES)


def test_undeclared_sql_reference_is_rejected():
    # region_sales_7d's SQL reads daily_region_sales; drop the declaration
    def f(r):
        r["tables"]["region_sales_7d"]["deps"] = [{"table": "raw_orders", "mapping": "same"}]
    _expect_error(f, "undeclared")


def test_declared_but_unused_dep_is_rejected():
    def f(r):
        r["tables"]["region_sales_7d"]["deps"].append({"table": "raw_refunds", "mapping": "same"})
    _expect_error(f, "never referenced")


def test_table_names_in_strings_and_comments_are_ignored():
    def f(r):
        r["tables"]["region_sales_7d"]["sql"] = (
            "-- raw_orders is not read here\n"
            "SELECT 'raw_refunds', 1, 1, 1 FROM daily_region_sales WHERE part = :p /* dim_customer */"
        )
    build_pipeline(mutated(example_raw(), f), EXAMPLES)  # no error


def test_unknown_dep_table():
    _expect_error(lambda r: r["tables"]["dod_change"]["deps"].append(
        {"table": "nope", "mapping": "same"}), "unknown table")


def test_bad_mapping_grain():
    _expect_error(lambda r: r["tables"]["monthly_region_sales"]["deps"].__setitem__(
        0, {"table": "daily_region_sales", "mapping": "same"}), "equal grains")


def test_self_dependency_must_be_lag():
    _expect_error(lambda r: r["tables"]["cumulative_net"]["deps"].__setitem__(
        1, {"table": "cumulative_net", "mapping": "window", "n": 2}), "self-dependency")


def test_cycle_detected():
    def f(r):
        r["tables"]["daily_region_sales"]["deps"].append({"table": "dod_change", "mapping": "same"})
        r["tables"]["daily_region_sales"]["sql"] = (
            "SELECT c.region, 1, x.amount_cents, 0, 0 FROM raw_orders x "
            "JOIN dim_customer c USING (customer_id) JOIN raw_refunds USING (order_id) "
            "JOIN dod_change d ON d.region = c.region")
    _expect_error(f, "cycle")


def test_source_cannot_have_sql():
    _expect_error(lambda r: r["tables"]["raw_orders"].__setitem__("sql", "SELECT 1"), "source")


def test_parse_change_forms():
    p = example_pipeline()
    assert parse_change(p, "raw_orders:2026-10-05") == [("raw_orders", "2026-10-05")]
    assert len(parse_change(p, "raw_orders:2026-10-01..2026-10-07")) == 7
    assert parse_change(p, "dim_customer:*") == [("dim_customer", "*")]
    assert parse_change(p, "monthly_region_sales:2026-09") == [("monthly_region_sales", "2026-09")]
    assert len(parse_change(p, "raw_orders:*")) == 92
    for bad in ("raw_orders", "nope:2026-10-01", "raw_orders:2026-11-05",
                "raw_orders:2026-10", "raw_orders:2026-10-07..2026-10-01"):
        with pytest.raises(SpecError):
            parse_change(p, bad)
