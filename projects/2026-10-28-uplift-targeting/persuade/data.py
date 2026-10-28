"""Synthetic randomized promo campaign with known per-customer treatment effects.

Each customer has pre-campaign features, a randomized treatment flag (got the
discount email or not) and a binary outcome (purchased within 14 days).
Because the data is simulated we also keep the true conversion probabilities
under control (``oracle_p0``) and treatment (``oracle_p1``). These oracle
columns are used **only** for evaluation, never as model inputs.

The effect surface deliberately contains the four classic uplift segments:
sure things (high baseline, ~0 effect), persuadables (discount-seeking or
lapsed customers, large positive effect), lost causes (low baseline, ~0
effect) and sleeping dogs (heavy loyal buyers who react *negatively* to a
discount push).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

REGIONS = ("north", "south", "east", "west")
NUMERIC_FEATURES = (
    "tenure_months",
    "days_since_last_order",
    "orders_90d",
    "avg_basket",
    "discount_share",
    "email_open_rate",
    "app_user",
)
CATEGORICAL_FEATURES = ("region",)
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

TREATMENT = "treatment"
OUTCOME = "converted"
# Recorded after the campaign: only treated buyers can redeem the coupon.
# Using it as a feature would leak the outcome and the treatment.
POST_TREATMENT = ("coupon_redeemed",)
ORACLE = ("oracle_p0", "oracle_p1", "oracle_uplift")


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def generate(n: int = 30_000, seed: int = 0, treat_rate: float = 0.5) -> pd.DataFrame:
    """Simulate one randomized campaign of ``n`` customers."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0.0 < treat_rate < 1.0:
        raise ValueError("treat_rate must be in (0, 1)")
    rng = np.random.default_rng(seed)

    tenure = np.clip(rng.gamma(2.0, 14.0, n), 1, 120).round().astype(int)
    recency = np.clip(rng.exponential(45.0, n), 1, 365).round().astype(int)
    orders = rng.poisson(np.clip(6.0 * np.exp(-recency / 40.0) + 0.3, 0.1, None))
    basket = np.round(rng.lognormal(3.6, 0.45, n), 2)
    discount_share = np.round(rng.beta(2.0, 5.0, n), 3)
    open_rate = np.round(rng.beta(2.0, 3.0, n), 3)
    app_user = (rng.random(n) < 0.35 + 0.004 * np.minimum(tenure, 60)).astype(int)
    region = rng.choice(REGIONS, size=n, p=(0.3, 0.25, 0.25, 0.2))

    # Baseline (untreated) purchase propensity.
    base_logit = (
        -2.4
        + 0.28 * np.minimum(orders, 8)
        - 0.55 * np.log1p(recency / 30.0)
        + 0.35 * app_user
        + 0.004 * (basket - 40.0)
        + 0.15 * (region == "south")
    )

    lapsed = ((recency >= 30) & (recency <= 150)).astype(float)
    just_bought = (recency < 14).astype(float)  # sure things: would buy anyway
    loyal_heavy = ((orders >= 5) & (tenure >= 24)).astype(float)
    effect_logit = (
        0.2
        + 3.2 * (discount_share - 0.28)
        + 1.6 * (open_rate - 0.4)
        + 0.9 * lapsed
        - 0.9 * just_bought
        - 1.5 * loyal_heavy
        - 0.25 * (region == "west")
    )

    p0 = _sigmoid(base_logit)
    p1 = _sigmoid(base_logit + effect_logit)

    treatment = (rng.random(n) < treat_rate).astype(int)
    p = np.where(treatment == 1, p1, p0)
    converted = (rng.random(n) < p).astype(int)
    coupon = ((treatment == 1) & (converted == 1) & (rng.random(n) < 0.8)).astype(int)

    return pd.DataFrame(
        {
            "customer_id": [f"C{seed:02d}-{i:06d}" for i in range(n)],
            "tenure_months": tenure,
            "days_since_last_order": recency,
            "orders_90d": orders,
            "avg_basket": basket,
            "discount_share": discount_share,
            "email_open_rate": open_rate,
            "app_user": app_user,
            "region": region,
            TREATMENT: treatment,
            OUTCOME: converted,
            "coupon_redeemed": coupon,
            "oracle_p0": p0,
            "oracle_p1": p1,
            "oracle_uplift": p1 - p0,
        }
    )


def split(df: pd.DataFrame, test_size: float = 0.3, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reproducible train/test split stratified on (treatment, outcome).

    Customers are independent draws from one randomized experiment, so a
    random customer-level split is appropriate; stratifying keeps the four
    treatment x outcome cells in the same proportions on both sides.
    """
    strata = df[TREATMENT].astype(str) + "_" + df[OUTCOME].astype(str)
    train, test = train_test_split(df, test_size=test_size, random_state=seed, stratify=strata)
    return train.reset_index(drop=True), test.reset_index(drop=True)


def feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Model inputs: pre-treatment features only, region one-hot with fixed levels."""
    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")
    X = df[list(NUMERIC_FEATURES)].astype(float).copy()
    region = df["region"].astype(str)
    unknown = sorted(set(region) - set(REGIONS))
    if unknown:
        raise ValueError(f"unknown region values: {unknown}")
    for r in REGIONS:
        X[f"region_{r}"] = (region == r).astype(float)
    return X
