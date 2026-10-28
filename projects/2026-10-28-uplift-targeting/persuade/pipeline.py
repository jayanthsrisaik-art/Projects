"""generate -> split -> fit(train) -> score(test) -> evaluate, over one or more seeds."""

from __future__ import annotations

import numpy as np
import pandas as pd

from persuade import data, metrics, models

MODEL_ORDER = ("random", "response_model", "s_learner", "t_learner", "transformed_outcome")


def evaluate_scores(score, test: pd.DataFrame, budget: float) -> dict:
    y = test[data.OUTCOME].to_numpy()
    t = test[data.TREATMENT].to_numpy()
    u = test["oracle_uplift"].to_numpy()
    out = {
        "qini_area": metrics.qini_area(score, y, t),
        f"observed_uplift_top{int(round(budget * 100))}": metrics.observed_uplift_at(score, y, t, budget),
        "spearman_true_uplift": metrics.spearman(score, u),
    }
    out.update(metrics.oracle_policy(score, u, budget))
    return out


def run_seed(n: int, seed: int, budget: float, model_names=MODEL_ORDER) -> dict:
    df = data.generate(n=n, seed=seed)
    train, test = data.split(df, seed=seed)
    X_train, X_test = data.feature_matrix(train), data.feature_matrix(test)
    results = {}
    for name in model_names:
        model = models.make(name, seed=seed).fit(
            X_train, train[data.TREATMENT].to_numpy(), train[data.OUTCOME].to_numpy()
        )
        results[name] = evaluate_scores(model.score(X_test), test, budget)
    t = test[data.TREATMENT].to_numpy()
    y = test[data.OUTCOME].to_numpy()
    info = {
        "seed": seed,
        "n_train": len(train),
        "n_test": len(test),
        "test_observed_ate": float(y[t == 1].mean() - y[t == 0].mean()),
        "test_true_ate": float(test["oracle_uplift"].mean()),
        "test_negative_effect_share": float((test["oracle_uplift"] < 0).mean()),
    }
    return {"data": info, "models": results}


def run(n: int = 30_000, seeds=(0, 1, 2, 3, 4), budget: float = 0.2, model_names=MODEL_ORDER) -> dict:
    per_seed = [run_seed(n, s, budget, model_names) for s in seeds]
    summary = {}
    for name in model_names:
        keys = per_seed[0]["models"][name].keys()
        summary[name] = {
            k: {
                "mean": round(float(np.mean([r["models"][name][k] for r in per_seed])), 5),
                "std": round(float(np.std([r["models"][name][k] for r in per_seed], ddof=0)), 5),
            }
            for k in keys
            if k != "contacted"
        }
    return {
        "config": {"n": n, "seeds": list(seeds), "budget": budget, "test_size": 0.3},
        "summary": summary,
        "per_seed": per_seed,
    }


def score_customers(customers: pd.DataFrame, model_name: str = "t_learner", budget: float = 0.2,
                    train_n: int = 30_000, seed: int = 0) -> pd.DataFrame:
    """Train on a simulated campaign and rank new customers by predicted uplift.

    ``treat`` marks the top ``budget`` share of the input, but never a
    customer with a non-positive predicted effect.
    """
    if model_name not in models.UPLIFT_MODELS:
        raise ValueError(f"scoring needs an uplift model: {models.UPLIFT_MODELS}")
    df = data.generate(n=train_n, seed=seed)
    train, _ = data.split(df, seed=seed)
    model = models.make(model_name, seed=seed).fit(
        data.feature_matrix(train), train[data.TREATMENT].to_numpy(), train[data.OUTCOME].to_numpy()
    )
    uplift = model.score(data.feature_matrix(customers))
    k = metrics._top_k(len(customers), budget)
    order = np.argsort(-uplift, kind="stable")
    treat = np.zeros(len(customers), dtype=int)
    treat[order[:k]] = 1
    treat[uplift <= 0] = 0
    out = pd.DataFrame({
        "customer_id": customers["customer_id"].to_numpy(),
        "predicted_uplift": np.round(uplift, 5),
        "treat": treat,
    })
    out["rank"] = np.empty(len(out), dtype=int)
    out.loc[order, "rank"] = np.arange(1, len(out) + 1)
    return out
