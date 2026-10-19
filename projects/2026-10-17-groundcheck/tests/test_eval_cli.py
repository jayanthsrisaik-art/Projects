import json
from pathlib import Path

from groundcheck.cli import main
from groundcheck.evaluate import (
    load_cases, metrics, run, split_case, split_cases, tune,
)
from groundcheck.lexical import LexicalConfig

ROOT = Path(__file__).resolve().parent.parent


def test_metrics_known_values():
    m = metrics(["supported", "contradicted", "unsupported", "supported"],
                ["supported", "supported", "unsupported", "supported"])
    assert m["accuracy"] == 0.75
    assert m["per_class"]["supported"]["precision"] == 0.667
    assert m["per_class"]["contradicted"]["recall"] == 0.0
    assert m["confusion"]["contradicted"]["supported"] == 1


def test_split_is_deterministic_disjoint_and_complete():
    _, cases = load_cases()
    dev, test = split_cases(cases)
    assert len(dev) + len(test) == len(cases)
    assert not {c["id"] for c in dev} & {c["id"] for c in test}
    assert split_case("alder-01") == split_case("alder-01")
    assert len(dev) >= 20 and len(test) >= 20


def test_eval_data_is_well_formed():
    sources, cases = load_cases()
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["source"] in sources and c["label"] in
               {"supported", "unsupported", "contradicted"} for c in cases)


def test_tuning_uses_dev_only_and_matches_shipped_defaults():
    sources, cases = load_cases()
    dev, _ = split_cases(cases)
    assert tune(dev, sources) == LexicalConfig()


def test_lexical_beats_baselines_on_test():
    r = run()["test"]
    assert r["lexical"]["macro_f1"] > r["overlap-baseline"]["macro_f1"] > r["majority-class"]["macro_f1"]


def test_cli_matches_checked_in_example_report(capsys):
    rc = main(["check", "--json", "--answer", str(ROOT / "examples/answer.txt"),
               "--sources", str(ROOT / "examples/sources/mount_alder.txt")])
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    assert got == json.loads((ROOT / "examples/expected_report.json").read_text())
    assert got["counts"] == {"supported": 3, "unsupported": 1, "contradicted": 2}


def test_cli_fail_on_contradiction_exit_code(capsys):
    rc = main(["check", "--fail-on-contradiction", "--answer", str(ROOT / "examples/answer.txt"),
               "--sources", str(ROOT / "examples/sources/mount_alder.txt")])
    assert rc == 1


def test_cli_anthropic_without_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main(["check", "--backend", "anthropic", "--answer", str(ROOT / "examples/answer.txt"),
               "--sources", str(ROOT / "examples/sources/mount_alder.txt")])
    assert rc == 2 and "ANTHROPIC_API_KEY" in capsys.readouterr().err
