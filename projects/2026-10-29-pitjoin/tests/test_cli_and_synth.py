import json

from conftest import EXAMPLES
from pitjoin import synth
from pitjoin.cli import main
from pitjoin.repo import load_entities, load_repo
from pitjoin.training import build_training_set

REPO = str(EXAMPLES / "feature_repo.json")


def test_join_command_writes_expected_file(tmp_path):
    out = tmp_path / "train.csv"
    assert main(["join", REPO, str(EXAMPLES / "transactions.csv"), "--out", str(out), "--engine", "sql"]) == 0
    assert out.read_text() == (EXAMPLES / "expected_training_set.csv").read_text()


def test_leakage_command_prints_json(capsys):
    assert main(["leakage", REPO, str(EXAMPLES / "transactions.csv")]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["strategies"]["asof_event_only"]["total"]["rows_affected"] == 3


def test_online_commands_round_trip(tmp_path, capsys):
    db = str(tmp_path / "online.db")
    assert main(["materialize", REPO, "--db", db, "--end", "2026-03-05T12:00:00Z"]) == 0
    capsys.readouterr()
    assert main(["get", REPO, "--db", db, "--view", "customer_daily", "C1", "C9"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got[0]["features"] == {"txn_count_7d": 6, "spend_7d": 210.0}
    assert got[1]["idx"] is None
    assert main(["skew-check", REPO, "--db", db]) == 0
    assert main(["materialize", REPO, "--db", db, "--end", "2026-03-01T00:00:00Z"]) == 2
    assert main(["get", REPO, "--db", db, "--view", "nope", "C1"]) == 2


def test_bad_repo_exits_2(tmp_path, capsys):
    (tmp_path / "r.json").write_text('{"feature_views": []}')
    assert main(["leakage", str(tmp_path / "r.json"), str(EXAMPLES / "transactions.csv")]) == 2
    assert "no feature views" in capsys.readouterr().err


def test_synth_is_deterministic_and_loadable(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    ca = synth.generate(a, seed=3, customers=20, days=10, transactions=200)
    synth.generate(b, seed=3, customers=20, days=10, transactions=200)
    for f in ("customer_daily.csv", "merchant_risk.csv", "customer_profile.csv", "transactions.csv"):
        assert (a / f).read_bytes() == (b / f).read_bytes()
    views = load_repo(a / "feature_repo.json")
    ents = load_entities(a / "transactions.csv")
    assert len(ents.rows) == ca["transactions"] == 200
    _, sweep_rows = build_training_set(views, ents, "sweep")
    _, sql_rows = build_training_set(views, ents, "sql")
    assert sweep_rows == sql_rows


def test_demo_runs_and_passes(capsys):
    assert main(["demo", "--oracle-rows", "50"]) == 0
    assert "ALL CHECKS PASSED" in capsys.readouterr().out
