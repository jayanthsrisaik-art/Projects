import json

import pandas as pd
import pytest

from demandcast.config import ExperimentConfig
from demandcast.data import generate_demand


def test_generator_deterministic_and_shaped():
    a, b = generate_demand(10, seed=1), generate_demand(10, seed=1)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 240 and (a["demand"] >= 0).all()
    assert not a.equals(generate_demand(10, seed=2))


def test_forecast_differs_from_truth_by_noise_level():
    d = generate_demand(60, seed=0, temp_noise_std=2.0)
    assert 1.5 < (d["temp_forecast"] - d["temperature"]).std() < 2.5
    perfect = generate_demand(5, seed=0, temp_noise_std=0.0)
    assert (perfect["temp_forecast"] - perfect["temperature"]).abs().max() < 0.01


def test_generator_rejects_bad_days():
    with pytest.raises(ValueError):
        generate_demand(0)


def test_config_roundtrip_and_validation(tmp_path):
    cfg = ExperimentConfig(days=200, n_folds=2)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg.to_dict()))
    assert ExperimentConfig.from_json(p) == cfg
    with pytest.raises(ValueError, match="unknown"):
        ExperimentConfig.from_dict({"nope": 1})
    with pytest.raises(ValueError):
        ExperimentConfig(quantiles=(0.9, 0.1))
    with pytest.raises(ValueError, match="too small"):
        ExperimentConfig(days=30)
