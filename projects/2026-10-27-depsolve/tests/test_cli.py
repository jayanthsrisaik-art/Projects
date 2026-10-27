import json

from depsolve.cli import main

from helpers import SAMPLES

REG = str(SAMPLES / "registry.json")


def test_resolve_matches_expected_lock(tmp_path, capsys):
    out = tmp_path / "lock.json"
    assert main(["resolve", "--registry", REG, "--manifest", str(SAMPLES / "app.json"), "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == json.loads((SAMPLES / "expected/app.lock.json").read_text())
    assert "resolved 7 packages" in capsys.readouterr().err


def test_conflict_matches_expected_explanation(capsys):
    assert main(["resolve", "--registry", REG, "--manifest", str(SAMPLES / "app-conflict.json")]) == 1
    assert capsys.readouterr().err == (SAMPLES / "expected/app-conflict.txt").read_text()


def test_lock_preference_and_targeted_upgrade(tmp_path, capsys):
    out = tmp_path / "lock.json"
    args = ["resolve", "--registry", REG, "--manifest", str(SAMPLES / "app.json"),
            "--lock", str(SAMPLES / "old.lock.json"), "--out", str(out)]
    assert main(args) == 0
    pkgs = json.loads(out.read_text())["packages"]
    assert pkgs["crypto"]["version"] == "3.1.5"  # yanked but locked: kept
    assert pkgs["log"]["version"] == "1.2.0"
    capsys.readouterr()
    assert main(args + ["--upgrade", "log"]) == 0
    assert json.loads(out.read_text()) == json.loads((SAMPLES / "expected/app.locked-upgrade.lock.json").read_text())
    assert "↑ log 1.2.0 -> 1.3.2" in capsys.readouterr().err


def test_check_and_why(tmp_path, capsys):
    lock = str(SAMPLES / "expected/app.lock.json")
    assert main(["check", "--registry", REG, "--manifest", str(SAMPLES / "app.json"), "--lock", lock]) == 0
    assert main(["why", "--lock", lock, "crypto"]) == 0
    assert capsys.readouterr().out.strip().endswith("shop-api -> orm@3.1.0 -> tls@2.4.0 -> crypto@3.2.0")
    assert main(["why", "--lock", lock, "nope"]) == 1
    # A lock that violates the manifest is rejected.
    bad = json.loads((SAMPLES / "expected/app.lock.json").read_text())
    bad["packages"]["tls"]["version"] = "3.0.1"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    assert main(["check", "--registry", REG, "--manifest", str(SAMPLES / "app.json"), "--lock", str(p)]) == 1
    assert "orm@3.1.0 requires tls ^2.1.0 but tls@3.0.1 was chosen" in capsys.readouterr().out


def test_match_and_bad_input(tmp_path, capsys):
    assert main(["match", "^1.2.0", "1.3.0", "2.0.0"]) == 0
    assert capsys.readouterr().out == "1.3.0\tyes\n2.0.0\tno\n"
    bad = tmp_path / "r.json"
    bad.write_text('{"packages": {"a": {"not-a-version": {}}}}')
    assert main(["resolve", "--registry", str(bad), "--manifest", str(SAMPLES / "app.json")]) == 2
