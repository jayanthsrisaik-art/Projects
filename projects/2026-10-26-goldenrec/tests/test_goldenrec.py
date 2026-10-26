import csv
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from goldenrec.cli import main
from goldenrec.evaluate import blocking_metrics, pairwise_metrics
from goldenrec.matcher import MatchConfig, baseline_clusters, resolve, score_pair
from goldenrec.normalize import (canonical_first, norm_dob, norm_email, norm_phone, norm_street, norm_zip,
                                 normalize_record)
from goldenrec.similarity import jaro_winkler, soundex, token_jaccard
from goldenrec.store import connect, export_golden, lookup, read_csv, run_pipeline
from goldenrec.survivorship import build_golden
from goldenrec.synth import generate

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def rec(source="crm", sid="1", first="Robert", last="Smith", email="", phone="", dob="",
        street="", city="Springfield", zip="62701", updated_at="2025-01-01"):
    return {"source": source, "source_id": sid, "first_name": first, "last_name": last, "email": email,
            "phone": phone, "dob": dob, "street": street, "city": city, "zip": zip, "updated_at": updated_at}


# --- similarity ---------------------------------------------------------------------------------

def test_jaro_winkler_reference_values():
    assert jaro_winkler("martha", "marhta") == pytest.approx(0.9611, abs=1e-4)
    assert jaro_winkler("dixon", "dicksonx") == pytest.approx(0.8133, abs=1e-4)
    assert jaro_winkler("same", "same") == 1.0
    assert jaro_winkler("", "abc") == 0.0


@pytest.mark.parametrize("word,code", [("Robert", "R163"), ("Rupert", "R163"), ("Ashcraft", "A261"),
                                       ("Tymczak", "T522"), ("Pfister", "P236"), ("Lee", "L000")])
def test_soundex(word, code):
    assert soundex(word) == code


def test_token_jaccard():
    assert token_jaccard("12 oak st", "12 oak st apt 3") == pytest.approx(0.6)
    assert token_jaccard(None, "x") == 0.0


# --- normalisation ------------------------------------------------------------------------------

def test_email_normalisation_handles_gmail_and_tags():
    assert norm_email("  John.Smith+promo@GoogleMail.com ") == "johnsmith@gmail.com"
    assert norm_email("j.smith+x@outlook.com") == "j.smith@outlook.com"  # dots matter outside Gmail
    assert norm_email("not-an-email") is None
    assert norm_email("") is None


@pytest.mark.parametrize("raw", ["(555) 123-4567", "555.123.4567", "+1 555 123 4567", "1-555-123-4567"])
def test_phone_formats_converge(raw):
    assert norm_phone(raw) == "5551234567"


def test_phone_rejects_short_numbers():
    assert norm_phone("123-4567") is None


def test_dob_formats_and_invalid_dates():
    assert norm_dob("1984-03-01") == norm_dob("03/01/1984") == norm_dob("19840301") == "1984-03-01"
    assert norm_dob("02/30/2020") is None
    assert norm_dob("yesterday") is None


def test_street_and_zip_and_nicknames():
    assert norm_street("12 Oak Street, Apartment 4") == norm_street("12 oak st. apt 4") == "12 oak st apt 4"
    assert norm_street("12 Oak St #4") == "12 oak st apt 4"
    assert norm_zip("62701-1234") == "62701"
    assert canonical_first("Bobby") == "robert" and canonical_first("José") == "jose"


def test_record_requires_source_id():
    with pytest.raises(ValueError):
        normalize_record(rec(sid=""))


# --- scoring and clustering ---------------------------------------------------------------------

def test_household_members_do_not_match():
    """Same surname, address and landline but different person: name mismatch outweighs the shared fields."""
    a = normalize_record(rec(sid="1", first="Maria", phone="5551234567", street="12 Oak St"))
    b = normalize_record(rec(sid="2", first="Thomas", phone="(555) 123-4567", street="12 Oak Street"))
    assert score_pair(a, b, MatchConfig()).score < MatchConfig().match_threshold


def test_nickname_typo_and_format_variants_match():
    a = normalize_record(rec(sid="1", first="Bob", last="Smiht", email="ROB.SMITH@gmail.com", dob="03/01/1984"))
    b = normalize_record(rec(source="billing", sid="9", first="Robert", last="Smith",
                             email="robsmith+shop@gmail.com", dob="1984-03-01"))
    p = score_pair(a, b, MatchConfig())
    assert p.score >= MatchConfig().match_threshold, p.reasons


def test_swapped_name_fields_still_match():
    a = normalize_record(rec(sid="1", first="Schneider", last="Andrew", dob="1984-03-01"))
    b = normalize_record(rec(sid="2", first="Andrew", last="Schneider", dob="19840301"))
    assert score_pair(a, b, MatchConfig()).score >= 6


def test_dob_conflict_is_a_hard_veto():
    a = normalize_record(rec(sid="1", email="x@y.com", phone="5551234567", dob="1980-01-01"))
    b = normalize_record(rec(sid="2", email="x@y.com", phone="5551234567", dob="2010-06-06"))
    p = score_pair(a, b, MatchConfig())
    assert p.conflict == "dob" and p.score == float("-inf")


def _chain_records():
    # parent and child share an email; a DOB-less record matches both (A more strongly)
    return [normalize_record(r) for r in [
        rec(sid="A", first="Robert", email="fam@x.com", phone="5550001111", dob="1960-01-01"),
        rec(sid="B", first="Robert", email="fam@x.com", phone="5550001111"),
        rec(sid="C", first="Roberta", email="fam@x.com", phone="5559998888", dob="1995-05-05"),
    ]]


def test_constrained_clustering_blocks_transitive_overmerge():
    records = _chain_records()
    result = resolve(records)
    assert result.blocked_merges == 1
    clusters = {frozenset(k[1] for k in c) for c in result.clusters}
    assert frozenset({"A", "C"}) not in clusters and not any({"A", "C"} <= c for c in clusters)
    # the strongest link (A~B, identical names) wins; C stays alone
    assert frozenset({"A", "B"}) in clusters and frozenset({"C"}) in clusters

    unconstrained = resolve(records, replace(MatchConfig(), enforce_dob_constraint=False))
    assert [len(c) for c in unconstrained.clusters] == [3]


def test_blocking_drops_oversized_blocks():
    records = [normalize_record(rec(sid=str(i), first=f"P{i}", last="Zed", email="shared@corp.com"))
               for i in range(10)]
    result = resolve(records, replace(MatchConfig(), max_block_size=5))
    assert result.blocking.oversized_blocks >= 1


def test_duplicate_source_keys_rejected():
    r = normalize_record(rec())
    with pytest.raises(ValueError):
        resolve([r, r])


# --- survivorship ---------------------------------------------------------------------------------

def test_survivorship_rules_and_lineage():
    cluster = [normalize_record(r) for r in [
        rec(source="support", sid="s1", first="bob", email="old@x.com", street="1 Elm St", updated_at="2024-01-01"),
        rec(source="crm", sid="c1", first="Robert", email="", dob="1970-02-02", updated_at="2023-05-05"),
        rec(source="billing", sid="b1", first="Rob", email="new@x.com", street="9 Pine Ave", zip="53703",
            updated_at="2025-07-07"),
    ]]
    g = build_golden(cluster)
    assert g.values["first_name"] == "Robert" and g.lineage["first_name"] == "crm:c1"  # formal form of consensus
    assert g.values["email"] == "new@x.com" and g.lineage["email"] == "billing:b1"  # most recent
    assert g.values["street"] == "9 Pine Ave" and g.values["zip"] == "53703"
    assert g.values["dob"] == "1970-02-02"
    assert g.values["phone"] is None and g.lineage["phone"] is None


# --- store: stable IDs ----------------------------------------------------------------------------

def _ids(db):
    conn = sqlite3.connect(db)
    out = dict(((s, i), g) for s, i, g in conn.execute("SELECT source, source_id, golden_id FROM crosswalk"))
    conn.close()
    return out


def test_rerun_is_idempotent(tmp_path):
    db = tmp_path / "g.db"
    rows = read_csv(SAMPLES / "customers.csv")
    first = run_pipeline(db, rows)
    ids1 = _ids(db)
    second = run_pipeline(db, rows)
    assert _ids(db) == ids1
    assert first.events["created"] == first.clusters
    assert dict(second.events) == {}
    assert second.clusters == first.clusters


def test_bridging_record_merges_ids_and_old_id_still_resolves(tmp_path):
    db = tmp_path / "g.db"
    a = rec(sid="A", first="Robert", email="rob@x.com", updated_at="2024-01-01")
    b = rec(source="billing", sid="B", first="Robert", phone="5552223333", updated_at="2024-01-01")
    run_pipeline(db, [a, b])
    ids = _ids(db)
    assert ids[("crm", "A")] != ids[("billing", "B")]
    bridge = rec(source="support", sid="C", first="Bob", email="rob@x.com", phone="555-222-3333",
                 updated_at="2024-02-01")
    summary = run_pipeline(db, [bridge])
    new = _ids(db)
    assert len(set(new.values())) == 1
    survivor = new[("crm", "A")]
    assert survivor == min(ids.values())  # tie on votes -> lowest id kept
    retired = max(ids.values())
    assert summary.events["merged"] == 1
    conn = connect(db)
    assert lookup(conn, retired) == survivor
    assert lookup(conn, survivor) == survivor
    assert lookup(conn, "G999999") is None


def test_split_keeps_id_for_larger_part(tmp_path):
    db = tmp_path / "g.db"
    rows = [rec(sid="A", email="rob@x.com", dob="1970-01-01"),
            rec(source="billing", sid="B", email="rob@x.com", dob="1970-01-01"),
            rec(source="support", sid="C", email="rob@x.com")]
    run_pipeline(db, rows)
    (gid,) = set(_ids(db).values())
    # support corrects C: different person (other DOB, own email)
    summary = run_pipeline(db, [rec(source="support", sid="C", first="Rachel", email="rachel@x.com",
                                    dob="1999-09-09", updated_at="2025-06-01")])
    ids = _ids(db)
    assert ids[("crm", "A")] == ids[("billing", "B")] == gid
    assert ids[("support", "C")] != gid
    conn = connect(db)
    detail = conn.execute("SELECT detail FROM id_events WHERE run_id = 2 AND event = 'created'").fetchone()[0]
    assert json.loads(detail) == {"split_from": [gid]}
    assert summary.events == {"created": 1, "updated": 1}


def test_stale_update_does_not_overwrite_newer_row(tmp_path):
    db = tmp_path / "g.db"
    run_pipeline(db, [rec(sid="A", email="new@x.com", updated_at="2025-05-05")])
    run_pipeline(db, [rec(sid="A", email="old@x.com", updated_at="2024-01-01")])
    conn = connect(db)
    assert conn.execute("SELECT email FROM source_records").fetchone()[0] == "new@x.com"


def test_bad_batch_rolls_back_everything(tmp_path):
    db = tmp_path / "g.db"
    run_pipeline(db, [rec(sid="A")])
    with pytest.raises(ValueError):
        run_pipeline(db, [rec(sid="B"), rec(sid="")])
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


# --- end to end -----------------------------------------------------------------------------------

def test_synthetic_generator_is_deterministic_and_truth_is_separate():
    r1, t1 = generate(50, seed=3)
    r2, t2 = generate(50, seed=3)
    assert r1 == r2 and t1 == t2
    assert all("entity_id" not in r for r in r1)
    assert len({(r["source"], r["source_id"]) for r in r1}) == len(r1)


def test_matcher_beats_baseline_on_synthetic_data():
    rows, truth = generate(300, seed=11)
    records = [normalize_record(r) for r in rows]
    result = resolve(records)
    ours = pairwise_metrics(result.clusters, truth)
    base = pairwise_metrics(baseline_clusters(records), truth)
    assert ours["precision"] >= 0.98
    assert ours["f1"] >= base["f1"] + 0.15
    assert blocking_metrics(result.candidate_set, truth)["pair_completeness"] >= 0.97


def test_pairwise_metrics_hand_example():
    truth = [{"source": "s", "source_id": i, "entity_id": e} for i, e in [("1", "x"), ("2", "x"), ("3", "y")]]
    m = pairwise_metrics([[("s", "1"), ("s", "2"), ("s", "3")]], truth)
    assert (m["precision"], m["recall"]) == (round(1 / 3, 4), 1.0)


def test_sample_golden_output_matches_expected(tmp_path):
    db = tmp_path / "g.db"
    run_pipeline(db, read_csv(SAMPLES / "customers.csv"))
    out = tmp_path / "golden.csv"
    export_golden(connect(db), out)
    assert out.read_text() == (SAMPLES / "expected_golden.csv").read_text()


def test_sample_incremental_batch_matches_expected(tmp_path):
    db = tmp_path / "g.db"
    run_pipeline(db, read_csv(SAMPLES / "customers.csv"))
    summary = run_pipeline(db, read_csv(SAMPLES / "batch2.csv"))
    expected = json.loads((SAMPLES / "expected_batch2_summary.json").read_text())
    assert summary.as_dict() == expected


def test_cli_end_to_end(tmp_path, capsys):
    data, truth, db, out = (str(tmp_path / n) for n in ("c.csv", "t.csv", "g.db", "golden.csv"))
    assert main(["generate", "--entities", "40", "--seed", "5", "--out", data, "--truth", truth]) == 0
    assert main(["run", "--db", db, data]) == 0
    assert main(["export", "--db", db, "--out", out]) == 0
    with open(out) as fh:
        golden = list(csv.DictReader(fh))
    assert golden and all(g["golden_id"].startswith("G") for g in golden)
    assert main(["lookup", golden[0]["golden_id"], "--db", db]) == 0
    assert main(["review", "--db", db]) == 0
    capsys.readouterr()
    assert main(["evaluate", data, truth]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["goldenrec"]["f1"] > report["baseline_exact_email_or_phone"]["f1"]


def test_consensus_outvotes_a_typo_from_the_priority_source():
    cluster = [normalize_record(r) for r in [
        rec(source="crm", sid="c1", first="Nick", last="Clrak"),
        rec(source="billing", sid="b1", first="Nicholas", last="Clark"),
        rec(source="support", sid="s1", first="NICHOLAS", last="Clark"),
    ]]
    g = build_golden(cluster)
    assert g.values["last_name"] == "Clark" and g.lineage["last_name"] == "billing:b1"
    assert g.values["first_name"] == "Nicholas"


def test_golden_email_keeps_dots_but_drops_tags():
    cluster = [normalize_record(rec(email="Michael.Clark88+news@Gmail.com"))]
    assert build_golden(cluster).values["email"] == "michael.clark88@gmail.com"
