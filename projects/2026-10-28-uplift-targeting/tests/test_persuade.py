import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from persuade import data, metrics, models, pipeline

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


@pytest.fixture(scope="module")
def campaign():
    df = data.generate(n=20_000, seed=3)
    train, test = data.split(df, seed=3)
    return df, train, test


# ---------------------------------------------------------------- data
def test_generate_is_deterministic_and_seed_dependent():
    a, b, c = data.generate(500, seed=1), data.generate(500, seed=1), data.generate(500, seed=2)
    pd.testing.assert_frame_equal(a, b)
    assert not a[list(data.NUMERIC_FEATURES)].equals(c[list(data.NUMERIC_FEATURES)])


def test_treatment_is_randomized_and_balanced(campaign):
    df, _, _ = campaign
    assert abs(df[data.TREATMENT].mean() - 0.5) < 0.02
    # Randomization: pre-treatment features have the same distribution in both arms.
    by_arm = df.groupby(data.TREATMENT)[["orders_90d", "discount_share", "oracle_p0"]].mean()
    rel_gap = (by_arm.loc[1] - by_arm.loc[0]).abs() / by_arm.mean()
    assert (rel_gap < 0.05).all()


def test_oracle_columns_consistent_and_include_all_segments(campaign):
    df, _, _ = campaign
    assert np.allclose(df["oracle_uplift"], df["oracle_p1"] - df["oracle_p0"])
    assert df[["oracle_p0", "oracle_p1"]].stack().between(0, 1).all()
    assert (df["oracle_uplift"] < -0.1).mean() > 0.05  # sleeping dogs exist
    assert (df["oracle_uplift"] > 0.1).mean() > 0.1  # persuadables exist
    # Observed ATE from the randomized experiment tracks the true ATE.
    t, y = df[data.TREATMENT], df[data.OUTCOME]
    assert abs((y[t == 1].mean() - y[t == 0].mean()) - df["oracle_uplift"].mean()) < 0.015


def test_split_is_reproducible_disjoint_and_stratified(campaign):
    df, train, test = campaign
    train2, test2 = data.split(df, seed=3)
    pd.testing.assert_frame_equal(test, test2)
    assert set(train.customer_id).isdisjoint(test.customer_id)
    assert len(train) + len(test) == len(df)
    assert abs(len(test) / len(df) - 0.3) < 1e-3
    for frame in (train, test):
        cells = frame.groupby([data.TREATMENT, data.OUTCOME]).size() / len(frame)
        overall = df.groupby([data.TREATMENT, data.OUTCOME]).size() / len(df)
        assert np.allclose(cells, overall, atol=0.002)


# ---------------------------------------------------------------- leakage
def test_feature_matrix_uses_only_pre_treatment_columns(campaign):
    df, _, _ = campaign
    X = data.feature_matrix(df)
    expected = list(data.NUMERIC_FEATURES) + [f"region_{r}" for r in data.REGIONS]
    assert list(X.columns) == expected
    forbidden = {data.TREATMENT, data.OUTCOME, "customer_id", *data.POST_TREATMENT, *data.ORACLE}
    assert forbidden.isdisjoint(X.columns)


def test_post_treatment_column_really_would_leak(campaign):
    df, _, _ = campaign
    redeemed = df["coupon_redeemed"] == 1
    assert redeemed.any()
    assert (df.loc[redeemed, data.TREATMENT] == 1).all()
    assert (df.loc[redeemed, data.OUTCOME] == 1).all()


def test_feature_matrix_rejects_unknown_region():
    df = data.generate(5, seed=0)
    df.loc[0, "region"] = "mars"
    with pytest.raises(ValueError, match="unknown region"):
        data.feature_matrix(df)


# ---------------------------------------------------------------- metrics
def test_qini_curve_hand_computed():
    #          customers sorted by score:  A    B    C    D
    score = np.array([0.9, 0.7, 0.5, 0.1])
    t = np.array([1, 0, 1, 0])
    y = np.array([1, 0, 1, 1])
    q = metrics.qini_curve(score, y, t)
    # k=1: Yt=1, Nc=0 -> 1 ; k=2: 1 - 0*1/1 = 1 ; k=3: 2 - 0*2/1 = 2 ; k=4: 2 - 1*2/2 = 1
    assert np.allclose(q, [0, 1, 1, 2, 1])
    area = np.mean(q - np.linspace(0, 1, 5)) / 4
    assert metrics.qini_area(score, y, t) == pytest.approx(area)


def test_qini_area_random_near_zero_and_oracle_best(campaign):
    _, _, test = campaign
    y, t, u = test[data.OUTCOME], test[data.TREATMENT], test["oracle_uplift"].to_numpy()
    rnd = np.random.default_rng(0).random(len(test))
    assert abs(metrics.qini_area(rnd, y, t)) < 0.004
    assert metrics.qini_area(u, y, t) > metrics.qini_area(-u, y, t) + 0.02


def test_observed_uplift_at_k_exact():
    score = np.array([4, 3, 2, 1, 0])
    t = np.array([1, 1, 0, 0, 1])
    y = np.array([1, 0, 0, 0, 1])
    # top 80% = first 4: treated rate 1/2, control rate 0/2
    assert metrics.observed_uplift_at(score, y, t, 0.8) == pytest.approx(0.5)
    assert np.isnan(metrics.observed_uplift_at(score, y, t, 0.4))  # only treated in top 2


def test_oracle_policy_values():
    u = np.array([0.3, -0.2, 0.1, 0.0, 0.2])
    res = metrics.oracle_policy(u, u, 0.4)
    assert res["contacted"] == 2 and res["capture_of_best"] == pytest.approx(1.0)
    bad = metrics.oracle_policy(-u, u, 0.4)
    assert bad["expected_incremental"] == pytest.approx(-0.2)
    assert bad["sleeping_dog_share"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        metrics.oracle_policy(u, u, 0.0)


# ---------------------------------------------------------------- models
def test_transformed_outcome_is_unbiased_for_ate(campaign):
    df, _, _ = campaign
    t, y = df[data.TREATMENT].to_numpy(), df[data.OUTCOME].to_numpy()
    ystar = models.transformed_outcome(y, t, t.mean())
    assert ystar.mean() == pytest.approx(y[t == 1].mean() - y[t == 0].mean(), abs=1e-9)


def test_models_reject_bad_input(campaign):
    _, train, _ = campaign
    X = data.feature_matrix(train)
    y = train[data.OUTCOME].to_numpy()
    with pytest.raises(ValueError, match="both treated and control"):
        models.TLearner().fit(X, np.ones(len(X), dtype=int), y)
    with pytest.raises(ValueError, match="unknown model"):
        models.make("magic")


def test_uplift_models_beat_baselines_on_held_out_data(campaign):
    _, train, test = campaign
    res = {}
    for name in pipeline.MODEL_ORDER:
        m = models.make(name, seed=3).fit(
            data.feature_matrix(train), train[data.TREATMENT], train[data.OUTCOME]
        )
        res[name] = pipeline.evaluate_scores(m.score(data.feature_matrix(test)), test, 0.2)
    for name in models.UPLIFT_MODELS:
        assert res[name]["qini_area"] > res["response_model"]["qini_area"]
        assert res[name]["capture_of_best"] > res["response_model"]["capture_of_best"] + 0.05
        assert res[name]["sleeping_dog_share"] < res["response_model"]["sleeping_dog_share"]
    assert res["response_model"]["qini_area"] > res["random"]["qini_area"]


# ---------------------------------------------------------------- pipeline / CLI
def test_run_report_structure_small():
    rep = pipeline.run(n=4_000, seeds=(0, 1), budget=0.3, model_names=("random", "t_learner"))
    assert rep["config"]["seeds"] == [0, 1]
    assert set(rep["summary"]) == {"random", "t_learner"}
    assert "observed_uplift_top30" in rep["summary"]["t_learner"]
    assert len(rep["per_seed"]) == 2 and rep["per_seed"][0]["data"]["n_test"] == 1_200


def test_expected_report_matches_documented_ordering():
    rep = json.loads((SAMPLES / "expected_report.json").read_text())
    s = rep["summary"]
    assert s["t_learner"]["capture_of_best"]["mean"] > s["response_model"]["capture_of_best"]["mean"]
    assert abs(s["random"]["qini_area"]["mean"]) < 0.002


def test_score_cli_reproduces_sample_output(tmp_path):
    out = tmp_path / "scores.csv"
    cmd = [sys.executable, "-m", "persuade", "score", "--input", str(SAMPLES / "customers_sample.csv"),
           "--output", str(out)]
    subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True)
    got, want = pd.read_csv(out), pd.read_csv(SAMPLES / "expected_scores.csv")
    assert list(got.columns) == ["customer_id", "predicted_uplift", "treat", "rank"]
    assert (got["customer_id"] == want["customer_id"]).all()
    assert np.allclose(got["predicted_uplift"], want["predicted_uplift"], atol=1e-4)
    assert (got["treat"] == want["treat"]).all()
    assert got["treat"].sum() <= int(np.ceil(0.2 * len(got)))
    assert (got.loc[got["treat"] == 1, "predicted_uplift"] > 0).all()


def test_score_rejects_non_uplift_model():
    with pytest.raises(ValueError, match="uplift model"):
        pipeline.score_customers(data.generate(5, 0), model_name="response_model", train_n=500)
