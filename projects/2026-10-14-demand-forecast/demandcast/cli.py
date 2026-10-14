import argparse
import json
from dataclasses import replace

import pandas as pd

from .config import ExperimentConfig
from .data import generate_demand
from .evaluation import run_backtest
from .reporting import save_report
from .serving import load_model, predict_frame, save_model, train_final


def _config(a) -> ExperimentConfig:
    cfg = ExperimentConfig.from_json(a.config) if getattr(a, "config", None) else ExperimentConfig()
    over = {k: getattr(a, k) for k in ("days", "seed") if getattr(a, k, None) is not None}
    if getattr(a, "no_tune", False):
        over["tune"] = False
    return replace(cfg, **over)


def _read(path: str) -> pd.DataFrame:
    return pd.read_csv(path, parse_dates=["timestamp"])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="demandcast")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", help="JSON file with ExperimentConfig fields")
        p.add_argument("--days", type=int)
        p.add_argument("--seed", type=int)
        p.add_argument("--no-tune", action="store_true")

    r = sub.add_parser("run", help="rolling-origin backtest of all models")
    common(r)
    r.add_argument("--out", default="outputs")
    t = sub.add_parser("train", help="fit final model on all data and save it")
    common(t)
    t.add_argument("--data", help="CSV (default: synthetic)")
    t.add_argument("--model-out", required=True)
    p = sub.add_parser("predict", help="score a CSV with a saved model")
    p.add_argument("--model", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    g = sub.add_parser("generate", help="write synthetic data CSV")
    g.add_argument("--days", type=int, default=7)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--out", required=True)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "generate":
        generate_demand(a.days, a.seed).to_csv(a.out, index=False)
        print(f"wrote {a.out}")
    elif a.cmd == "run":
        report = run_backtest(_config(a))
        jp, mp = save_report(report, a.out)
        print(mp.read_text())
        print(f"wrote {jp} and {mp}")
    elif a.cmd == "train":
        cfg = _config(a)
        df = _read(a.data) if a.data else generate_demand(cfg.days, cfg.seed, temp_noise_std=cfg.temp_noise_std)
        art = train_final(df, cfg)
        save_model(art, a.model_out)
        print(json.dumps({"trained_through": art["trained_through"], "hgb_params": art["hgb_params"]}))
    elif a.cmd == "predict":
        out = predict_frame(load_model(a.model), _read(a.input))
        out.to_csv(a.out, index=False)
        print(f"wrote {len(out)} predictions to {a.out}")
    return 0
