import json

import pytest

from conftest import EXAMPLES
from pitjoin.repo import MICROS, RepoError, format_ts, load_entities, load_repo, parse_ts, parse_value


def test_parse_ts_normalises_offsets_to_utc():
    assert parse_ts("2026-03-02T12:00:00+02:00") == parse_ts("2026-03-02T10:00:00Z")
    assert parse_ts("2026-03-02T10:00:00") == parse_ts("2026-03-02T10:00:00Z")  # naive = UTC


def test_parse_ts_is_exact_to_the_microsecond():
    a = parse_ts("2026-03-02T10:00:00.000001Z")
    assert a - parse_ts("2026-03-02T10:00:00Z") == 1
    assert parse_ts("1970-01-01T00:00:01Z") == MICROS


@pytest.mark.parametrize("text", ["2026-03-02T10:00:00Z", "2026-03-02T10:00:00.250000Z", "1969-12-31T23:59:59Z"])
def test_format_ts_round_trips(text):
    assert format_ts(parse_ts(text)) == text


def test_parse_value_types_and_nulls():
    assert parse_value("", "int") is None
    assert parse_value("3", "int") == 3
    assert parse_value("yes", "bool") is True
    assert parse_value("0", "bool") is False
    with pytest.raises(ValueError):
        parse_value("maybe", "bool")


def test_example_repo_loads():
    views = load_repo(EXAMPLES / "feature_repo.json")
    assert list(views) == ["customer_daily", "merchant_risk", "customer_profile"]
    cd = views["customer_daily"]
    assert cd.ttl_us == 3 * 86400 * MICROS
    assert cd.rows[3].values == (6, 210.0)
    prof = views["customer_profile"]
    # no created timestamp -> created == event timestamp
    assert all(r.created == r.ts for r in prof.rows)
    ents = load_entities(EXAMPLES / "transactions.csv")
    assert len(ents.rows) == 9 and ents.keys("customer_id")[7] == "C3"


def _repo(tmp_path, view_overrides=None, csv_text=None):
    view = {"name": "v", "entity": "k", "source": "v.csv", "created_timestamp_field": "c",
            "features": [{"name": "x", "dtype": "int"}]}
    view.update(view_overrides or {})
    (tmp_path / "v.csv").write_text(csv_text or "k,event_timestamp,c,x\na,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z,1\n")
    (tmp_path / "repo.json").write_text(json.dumps({"feature_views": [view]}))
    return tmp_path / "repo.json"


@pytest.mark.parametrize("overrides,csv_text,match", [
    ({"features": [{"name": "x", "dtype": "decimal"}]}, None, "unknown dtypes"),
    ({"ttl_seconds": 0}, None, "ttl_seconds"),
    ({"name": "a__b"}, None, "may not contain"),
    ({"features": [{"name": "x"}, {"name": "x"}]}, None, "duplicate feature"),
    (None, "k,event_timestamp,c\na,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z\n", "lacks columns"),
    (None, "k,event_timestamp,c,x\n,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z,1\n", "line 2: empty join key"),
    (None, "k,event_timestamp,c,x\na,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z,1\na,yesterday,2026-01-01T00:00:00Z,1\n",
     "line 3"),
    (None, "k,event_timestamp,c,x\na,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z,1.5\n", "line 2"),
])
def test_invalid_repos_are_rejected_with_location(tmp_path, overrides, csv_text, match):
    with pytest.raises(RepoError, match=match):
        load_repo(_repo(tmp_path, overrides, csv_text))


def test_duplicate_view_names_rejected(tmp_path):
    path = _repo(tmp_path)
    spec = json.loads(path.read_text())
    spec["feature_views"].append(spec["feature_views"][0])
    path.write_text(json.dumps(spec))
    with pytest.raises(RepoError, match="duplicate feature view"):
        load_repo(path)


def test_entities_need_timestamp_column(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("k,when\na,2026-01-01\n")
    with pytest.raises(RepoError, match="timestamp column"):
        load_entities(p)
    assert load_entities(p, "when").ts == [parse_ts("2026-01-01T00:00:00Z")]
