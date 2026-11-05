import json
import os

from conftest import SAMPLES
from winstream.bench import run_bench
from winstream.cli import main
from winstream.generator import generate
from winstream.model import WindowSpec


def test_tiny_sample_matches_expected_files(tmp_path, capsys):
    tiny = os.path.join(SAMPLES, "tiny.jsonl")
    for spec, name in (("tumbling:60", "tumbling60"), ("session:40", "session40")):
        db = str(tmp_path / f"{name}.db")
        assert main(["run", "--events", tiny, "--db", db, "--window", spec, "--delay", "10",
                     "--lateness", "60"]) == 0
        capsys.readouterr()
        main(["show", "--db", db])
        assert capsys.readouterr().out == open(os.path.join(SAMPLES, "expected", f"tiny_{name}_results.tsv")).read()
        main(["show", "--db", db, "--changelog"])
        assert capsys.readouterr().out == open(os.path.join(SAMPLES, "expected", f"tiny_{name}_changelog.tsv")).read()


def test_verify_command_on_bundled_sample(capsys):
    ev = os.path.join(SAMPLES, "events.jsonl")
    for w in ("tumbling:60", "hopping:300:60", "session:120"):
        rc = main(["verify", "--events", ev, "--window", w, "--delay", "15", "--lateness", "120",
                   "--crashes", "1"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["ok"] and out["crash_runs"][0]["identical_to_clean_run"], out


def test_run_reports_crash_and_resumes(tmp_path, capsys):
    ev = os.path.join(SAMPLES, "events.jsonl")
    db = str(tmp_path / "c.db")
    args = ["run", "--events", ev, "--db", db, "--window", "tumbling:60", "--checkpoint-every", "100"]
    assert main(args + ["--crash-after", "250"]) == 3
    capsys.readouterr()
    assert main(args) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["resumed_from_offset"] == 200 and rep["flushed"]


def test_generate_writes_jsonl(tmp_path, capsys):
    out = tmp_path / "g.jsonl"
    assert main(["generate", "--out", str(out), "--keys", "2", "--duration", "900"]) == 0
    assert len(out.read_text().splitlines()) > 20


def test_bench_event_time_with_lateness_beats_processing_time():
    events = generate(n_keys=4, duration=3600, seed=2)
    rows = run_bench(events, [WindowSpec("tumbling", size=60), WindowSpec("session", gap=60)],
                     grid=[(0, 0), (15, 0), (15, 600)])
    by = {(r["window"], r["mode"], r["delay"], r["lateness"]): r for r in rows}
    pt = by[("tumbling:60", "processing-time", 0, 0)]
    full = by[("tumbling:60", "event-time", 15, 600)]
    assert full["final_exact"] == 1.0 and full["dropped"] == 0
    assert pt["final_exact"] < by[("tumbling:60", "event-time", 15, 0)]["final_exact"] < 1.0
    assert by[("session:60", "event-time", 15, 600)]["final_exact"] == 1.0
    # waiting longer costs latency
    assert by[("tumbling:60", "event-time", 15, 0)]["p50_latency"] > by[("tumbling:60", "event-time", 0, 0)]["p50_latency"]
