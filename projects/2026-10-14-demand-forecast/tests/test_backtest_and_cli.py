import json

import pandas as pd
import pytest

from demandcast.cli import main
from demandcast.config import ExperimentConfig
from demandcast.evaluation import run_backtest
from demandcast.reporting import render_markdown
from demandcast.serving import load_model, predict_frame, save_model, train_final
from demandcast.data import generate_demand


@pytest.fixture(scope="module")
def report(small_cfg):
    return run_backtest(small_cfg)


def test_backtest_structure(report):
    assert len(report["folds"]) == 2
    f0, f1 = report["folds"]
    assert f0["train_end"] < f0["test_start"] <= f0["test_end"] < f1["test_start"]
    assert f1["train_rows"] > f0["train_rows"]  # expanding window
    assert set(report["summary"]) >= {"ridge", "hist_gradient_boosting", "ensemble_ridge_hgb"}


def test_backtest_reproducible(small_cfg, report):
    assert run_backtest(small_cfg) == report


def test_models_beat_best_baseline(report):
    s = report["summary"]
    assert s["hist_gradient_boosting"]["mae"]["mean"] < s["seasonal_naive_168h"]["mae"]["mean"]
    assert s["hist_gradient_boosting"]["mae"]["mean"] < s["seasonal_naive_24h"]["mae"]["mean"]


def test_interval_metrics_sane(report):
    iv = report["interval_summary"]
    assert 0.5 < iv["coverage"]["mean"] <= 1.0
    assert iv["mean_width"]["mean"] > 0


def test_markdown_report_mentions_models(report):
    md = render_markdown(report)
    assert "| ridge |" in md and "WAPE by segment" in md


def test_tuning_runs_inside_backtest():
    cfg = ExperimentConfig(days=120, n_folds=1, test_days=14, val_days=14, tune=True)
    r = run_backtest(cfg)
    assert r["folds"][0]["hgb_params"]["learning_rate"] in (0.03, 0.1)


def test_serving_roundtrip(tmp_path, small_cfg):
    df = generate_demand(60, seed=1)
    art = train_final(df, small_cfg)
    path = tmp_path / "m.joblib"
    save_model(art, path)
    out = predict_frame(load_model(path), df)
    assert (out["lower"] <= out["upper"]).all()
    assert len(out) == len(df) - 504
    pd.testing.assert_frame_equal(out, predict_frame(art, df))


def test_cli_end_to_end(tmp_path, capsys):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"days": 100, "n_folds": 1, "test_days": 14, "val_days": 14, "tune": False}))
    out = tmp_path / "out"
    assert main(["run", "--config", str(cfg), "--out", str(out)]) == 0
    assert json.loads((out / "metrics.json").read_text())["config"]["days"] == 100
    assert (out / "report.md").exists()

    csv, model, pred = tmp_path / "d.csv", tmp_path / "m.joblib", tmp_path / "p.csv"
    main(["generate", "--days", "40", "--out", str(csv)])
    main(["train", "--config", str(cfg), "--data", str(csv), "--model-out", str(model)])
    main(["predict", "--model", str(model), "--input", str(csv), "--out", str(pred)])
    assert list(pd.read_csv(pred).columns) == ["timestamp", "prediction", "lower", "upper"]
