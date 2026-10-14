import pytest

from demandcast.config import ExperimentConfig

SMALL = dict(days=150, n_folds=2, test_days=14, val_days=14, tune=False)


@pytest.fixture(scope="session")
def small_cfg():
    return ExperimentConfig(**SMALL)
