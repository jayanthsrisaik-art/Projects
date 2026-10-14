"""Write JSON and Markdown reports."""
import json
from pathlib import Path


def render_markdown(report: dict) -> str:
    c = report["config"]
    lines = [
        "# Backtest report",
        "",
        f"{len(report['folds'])} rolling-origin folds, {c['test_days']}-day test windows, "
        f"{c['horizon']}h horizon, seed {c['seed']}.",
        "",
        "## Point forecasts (mean ± std over folds)",
        "",
        "| Model | MAE | RMSE | WAPE |",
        "|---|---|---|---|",
    ]
    for name, s in report["summary"].items():
        cells = " | ".join(f"{s[m]['mean']:.3f} ± {s[m]['std']:.3f}" for m in ("mae", "rmse", "wape"))
        lines.append(f"| {name} | {cells} |")
    lines += ["", f"Best by MAE: **{report['best_model_by_mae']}**", "", "## Prediction interval"]
    lo, hi = c["quantiles"]
    lines += [f"Nominal coverage {hi - lo:.0%}.", "", "| Metric | mean ± std |", "|---|---|"]
    for k, v in report["interval_summary"].items():
        lines.append(f"| {k} | {v['mean']:.3f} ± {v['std']:.3f} |")
    lines += ["", "## WAPE by segment (pooled test rows)", ""]
    names = list(report["summary"])
    lines += ["| Segment | Rows | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for seg, v in report["segment_wape"].items():
        lines.append(f"| {seg} | {v['rows']} | " + " | ".join(f"{v[n]:.4f}" for n in names) + " |")
    return "\n".join(lines) + "\n"


def save_report(report: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "metrics.json", out / "report.md"
    jp.write_text(json.dumps(report, indent=2) + "\n")
    mp.write_text(render_markdown(report))
    return jp, mp
