"""Time-ordered splitting utilities."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    train_end: int   # exclusive; train = rows [0, train_end)
    test_start: int  # inclusive
    test_end: int    # exclusive


def rolling_origin_splits(n: int, n_folds: int, test_size: int, gap: int = 0, min_train: int = 1) -> list[Fold]:
    """Expanding-window folds; the last fold's test window ends at row n.

    Each fold trains on everything before `test_start - gap`, so the folds' test windows
    are consecutive, non-overlapping and strictly after their training data.
    """
    if n_folds < 1 or test_size < 1 or gap < 0:
        raise ValueError("n_folds and test_size must be >= 1 and gap >= 0")
    first_test = n - n_folds * test_size
    if first_test - gap < min_train:
        raise ValueError(f"series of {n} rows too short for {n_folds} folds of {test_size} (min_train={min_train})")
    folds = []
    for k in range(n_folds):
        start = first_test + k * test_size
        folds.append(Fold(train_end=start - gap, test_start=start, test_end=start + test_size))
    return folds


def chronological_split(n: int, test_frac: float) -> int:
    """Index at which a simple holdout split starts (no shuffling)."""
    if not 0 < test_frac < 1:
        raise ValueError("test_frac must be in (0, 1)")
    return int(n * (1 - test_frac))
