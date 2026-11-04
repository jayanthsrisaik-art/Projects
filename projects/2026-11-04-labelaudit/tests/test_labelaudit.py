import csv
import json
from pathlib import Path

import numpy as np
import pytest

from labelaudit.cli import main
from labelaudit.confident import (
    calibrate_joint, confident_assignment, confident_joint, find_label_issues,
    noise_matrix_from_joint, per_class_thresholds,
)
from labelaudit.data import DIGIT_CONFUSIONS, encode_labels, inject_noise, make_split, read_table
from labelaudit.evaluate import empirical_noise_matrix, run_once, score_flags
from labelaudit.models import out_of_fold_proba

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


# ------------------------------------------------------------ core algorithm
def handmade():
    labels = np.array([0, 0, 0, 1, 1, 1])
    probs = np.array([
        [0.9, 0.1],
        [0.8, 0.2],
        [0.2, 0.8],   # labelled 0, confidently 1 -> issue
        [0.1, 0.9],
        [0.3, 0.7],
        [0.6, 0.4],   # labelled 1, p(0)=0.6 < t0 and p(1)=0.4 < t1 -> unassigned
    ])
    return labels, probs


def test_thresholds_are_mean_self_confidence_per_given_class():
    labels, probs = handmade()
    t = per_class_thresholds(labels, probs)
    assert t == pytest.approx([(0.9 + 0.8 + 0.2) / 3, (0.9 + 0.7 + 0.4) / 3])


def test_confident_joint_counts_by_hand():
    labels, probs = handmade()
    c, assign = confident_joint(labels, probs)
    # t0 = .633, t1 = .667 : row 5 qualifies for neither class
    assert assign.tolist() == [0, 0, 1, 1, 1, -1]
    assert c.tolist() == [[2, 1], [0, 2]]


def test_tie_between_qualifying_classes_goes_to_larger_probability():
    labels = np.array([0, 1, 2, 0])
    probs = np.array([[.5, .5, 0], [0, .5, .5], [.5, 0, .5], [.4, .6, 0]])
    a = confident_assignment(labels, probs, thresholds=np.array([.3, .3, .3]))
    assert a[3] == 1


def test_class_without_rows_gets_infinite_threshold():
    labels = np.array([0, 0, 1])
    probs = np.array([[.5, .2, .3], [.6, .1, .3], [.1, .8, .1]])
    t = per_class_thresholds(labels, probs)
    assert np.isinf(t[2])
    assert (confident_assignment(labels, probs, t) != 2).all()


def test_calibrated_joint_rows_match_label_counts_and_sum_to_one():
    labels, probs = handmade()
    c, _ = confident_joint(labels, probs)
    q = calibrate_joint(c, labels, 2)
    assert q.sum() == pytest.approx(1.0)
    assert (q.sum(axis=1) * len(labels)) == pytest.approx([3, 3])
    nm = noise_matrix_from_joint(q)
    assert nm.sum(axis=0) == pytest.approx([1.0, 1.0])


def test_oracle_probabilities_recover_every_flip_exactly():
    rng = np.random.default_rng(3)
    true = rng.integers(0, 4, size=400)
    noisy = inject_noise(true, 0.25, "uniform", 4, rng)
    probs = np.full((400, 4), 0.02)
    probs[np.arange(400), true] = 0.94
    for method in ("confident_joint", "prune_by_noise_rate", "both"):
        audit = find_label_issues(noisy, probs, method)
        assert (audit.issue_mask == (true != noisy)).all(), method
        assert (audit.suggested[audit.issue_mask] == true[audit.issue_mask]).all()
    assert audit.estimated_noise_rate == pytest.approx((true != noisy).mean())


def test_clean_labels_with_agreeing_model_flag_nothing():
    labels = np.repeat(np.arange(3), 50)
    probs = np.full((150, 3), 0.1)
    probs[np.arange(150), labels] = 0.8
    audit = find_label_issues(labels, probs)
    assert audit.n_issues == 0 and audit.estimated_noise_rate == pytest.approx(0.0)


def test_prune_by_noise_rate_takes_largest_margin_rows_and_ranking_is_most_suspicious_first():
    labels = np.array([0] * 8 + [1] * 8)
    probs = np.zeros((16, 2))
    probs[:, 0] = [.9, .9, .9, .9, .9, .9, .45, .05] + [.1] * 8
    probs[:, 1] = 1 - probs[:, 0]
    audit = find_label_issues(labels, probs, "prune_by_noise_rate")
    # confident joint: only row 7 (p1 = .95 >= t1) lands off-diagonal -> budget 1 -> row 7, not row 6
    assert np.flatnonzero(audit.issue_mask).tolist() == [7]
    assert audit.ranking.tolist() == [7]


def test_input_validation():
    with pytest.raises(ValueError):
        find_label_issues(np.array([0, 1]), np.array([[.5, .6], [.5, .5]]))
    with pytest.raises(ValueError):
        find_label_issues(np.array([0, 2]), np.array([[.5, .5], [.5, .5]]))
    with pytest.raises(ValueError):
        find_label_issues(np.array([0, 1]), np.array([[.5, .5], [.5, .5]]), "magic")


# ------------------------------------------------------------ data + splits
def test_noise_injection_rate_partner_and_determinism():
    y = np.repeat(np.arange(10), 100)
    a = inject_noise(y, 0.2, "asymmetric", 10, np.random.default_rng(1), DIGIT_CONFUSIONS)
    b = inject_noise(y, 0.2, "asymmetric", 10, np.random.default_rng(1), DIGIT_CONFUSIONS)
    assert (a == b).all()
    flipped = a != y
    assert flipped.sum() == 200
    assert all(a[i] == DIGIT_CONFUSIONS[y[i]] for i in np.flatnonzero(flipped))
    u = inject_noise(y, 0.3, "uniform", 10, np.random.default_rng(2))
    assert ((u != y).sum() == 300) and not np.any(u[u != y] == y[u != y])


def test_split_is_disjoint_and_test_labels_stay_clean():
    s = make_split("digits", 0.3, "uniform", seed=4)
    assert not set(s.train_index) & set(s.test_index)
    assert len(s.train_index) + len(s.test_index) == 1797
    assert s.true_noise_rate == pytest.approx(0.3, abs=0.01)
    from sklearn.datasets import load_digits
    assert (load_digits().target[s.test_index] == s.y_test).all()


def test_out_of_fold_rows_are_never_scored_by_their_own_model():
    # 1-row-per-pattern data where memorisation is the only way to be right:
    # random labels on random features -> OOF accuracy ~ chance, in-sample RF ~ 100%.
    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, 5))
    y = rng.integers(0, 3, size=300)
    probs, fold = out_of_fold_proba(x, y, "rf", folds=5, seed=0)
    assert set(fold.tolist()) == set(range(5))
    assert (probs.argmax(1) == y).mean() < 0.5
    assert probs.sum(axis=1) == pytest.approx(np.ones(300))


def test_csv_reader_and_label_encoding(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("id,a,b,label\nr1,1,2,cat\nr2,3,4,dog\n")
    x, raw, feats, ids = read_table(p, "label")
    assert x.tolist() == [[1, 2], [3, 4]] and feats == ["a", "b"] and ids == ["r1", "r2"]
    enc, classes = encode_labels(["10", "9", "2", "9"])
    assert classes == ["2", "9", "10"] and enc.tolist() == [2, 1, 0, 1]
    bad = tmp_path / "bad.csv"
    bad.write_text("a,label\nx,1\n")
    with pytest.raises(ValueError, match="non-numeric"):
        read_table(bad, "label")


# ------------------------------------------------------------ evaluation
def test_score_flags_and_empirical_noise_matrix():
    d = score_flags(np.array([1, 1, 0, 0], bool), np.array([1, 0, 1, 0], bool))
    assert (d.precision, d.recall, d.f1, d.flagged) == (0.5, 0.5, 0.5, 2)
    nm = empirical_noise_matrix(np.array([0, 1, 1, 1]), np.array([0, 0, 1, 1]), 2)
    assert nm.tolist() == [[0.5, 0.0], [0.5, 1.0]]


@pytest.fixture(scope="module")
def digits_run():
    return run_once("digits", "asymmetric", 0.2, seed=1, model="rf")


def test_confident_learning_beats_baselines_on_digits(digits_run):
    det = digits_run["detection"]
    cl = det["cl_prune_by_noise_rate"]
    assert cl["precision"] > 0.85 and cl["recall"] > 0.8
    assert cl["f1"] > det["self_conf_lt_0.5"]["f1"]
    assert cl["f1"] > det["random_same_k"]["f1"] + 0.5
    assert cl["precision"] > det["argmax_disagree"]["precision"]
    assert abs(digits_run["est_noise_rate"] - digits_run["true_noise_rate"]) < 0.05


def test_in_sample_probabilities_hide_the_noise(digits_run):
    det = digits_run["detection"]
    assert det["cl_pbnr_IN_SAMPLE_leaky"]["recall"] < 0.2 < det["cl_prune_by_noise_rate"]["recall"]


def test_pruning_improves_clean_test_accuracy(digits_run):
    acc = digits_run["test_accuracy"]
    assert acc["pruned_cl_prune_by_noise_rate"] > acc["noisy_labels"]
    assert acc["clean_labels_oracle"] >= acc["pruned_cl_prune_by_noise_rate"] - 0.01


# ------------------------------------------------------------ CLI on samples
def test_cli_audit_reproduces_committed_sample_outputs(tmp_path, capsys):
    out, rep = tmp_path / "issues.csv", tmp_path / "report.json"
    assert main(["audit", "--csv", str(SAMPLES / "digits_noisy.csv"), "--out", str(out),
                 "--report", str(rep)]) == 0
    assert out.read_text() == (SAMPLES / "expected_issues.csv").read_text()
    assert json.loads(rep.read_text()) == json.loads((SAMPLES / "expected_audit_report.json").read_text())
    capsys.readouterr()
    assert main(["score", "--issues", str(out), "--truth", str(SAMPLES / "truth.csv")]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((SAMPLES / "expected_score.json").read_text())


def test_cli_make_sample_matches_committed_sample(tmp_path):
    assert main(["make-sample", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "digits_noisy.csv").read_text() == (SAMPLES / "digits_noisy.csv").read_text()
    with open(tmp_path / "truth.csv") as fh:
        assert sum(r["is_flipped"] == "1" for r in csv.DictReader(fh)) == 250


def test_cli_errors_are_reported_not_raised(tmp_path, capsys):
    p = tmp_path / "x.csv"
    p.write_text("a,label\n1,0\n")
    assert main(["audit", "--csv", str(p), "--label", "nope"]) == 2
    assert "not found" in capsys.readouterr().err
