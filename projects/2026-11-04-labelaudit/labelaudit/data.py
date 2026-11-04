"""Datasets, controlled label-noise injection, splits and CSV I/O.

The default dataset is scikit-learn's bundled handwritten digits (1,797 8x8
images, UCI; shipped with scikit-learn, no download). Noise is injected only
into the training split, so the ground truth of every flip is known and the
test split keeps clean labels for an honest downstream evaluation.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.datasets import load_digits, make_classification
from sklearn.model_selection import train_test_split

# Visually confusable digit pairs: a realistic *asymmetric* annotator error model.
DIGIT_CONFUSIONS = {0: 6, 1: 7, 2: 3, 3: 8, 4: 9, 5: 6, 6: 5, 7: 1, 8: 3, 9: 4}


@dataclass
class Split:
    x_train: np.ndarray
    y_train_true: np.ndarray
    y_train_noisy: np.ndarray
    x_test: np.ndarray
    y_test: np.ndarray            # clean
    train_index: np.ndarray       # row ids in the original dataset
    test_index: np.ndarray

    @property
    def is_flipped(self) -> np.ndarray:
        return self.y_train_true != self.y_train_noisy

    @property
    def true_noise_rate(self) -> float:
        return float(self.is_flipped.mean())


def load_dataset(name: str = "digits", seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    if name == "digits":
        d = load_digits()
        return d.data.astype(float), d.target.astype(int)
    if name == "synthetic":
        x, y = make_classification(n_samples=2000, n_features=20, n_informative=10,
                                   n_redundant=4, n_classes=5, n_clusters_per_class=2,
                                   class_sep=1.2, random_state=seed)
        return x, y.astype(int)
    raise ValueError(f"unknown dataset {name!r} (digits | synthetic)")


def inject_noise(y: np.ndarray, rate: float, kind: str, n_classes: int,
                 rng: np.random.Generator, confusions: dict[int, int] | None = None) -> np.ndarray:
    """Flip exactly round(rate * n_c) labels within each class c.

    kind="asymmetric": class c flips to confusions[c] (default: c+1 mod K or the digit pairs)
    kind="uniform":    class c flips to a uniformly random *other* class
    """
    if not 0.0 <= rate < 1.0:
        raise ValueError("rate must be in [0, 1)")
    y = np.asarray(y, dtype=int)
    noisy = y.copy()
    if confusions is None:
        confusions = {c: (c + 1) % n_classes for c in range(n_classes)}
    for c in range(n_classes):
        idx = np.flatnonzero(y == c)
        m = int(round(rate * idx.size))
        if m == 0:
            continue
        pick = rng.choice(idx, size=m, replace=False)
        if kind == "asymmetric":
            noisy[pick] = confusions[c]
        elif kind == "uniform":
            shift = rng.integers(1, n_classes, size=m)
            noisy[pick] = (c + shift) % n_classes
        else:
            raise ValueError(f"unknown noise kind {kind!r} (asymmetric | uniform)")
    return noisy


def make_split(dataset: str = "digits", noise_rate: float = 0.2, noise_kind: str = "asymmetric",
               test_size: float = 0.3, seed: int = 0) -> Split:
    """Stratified train/test split first; noise goes into the training labels only."""
    x, y = load_dataset(dataset, seed)
    idx = np.arange(len(y))
    tr, te = train_test_split(idx, test_size=test_size, stratify=y, random_state=seed)
    k = int(y.max()) + 1
    rng = np.random.default_rng(seed + 10_000)
    conf = DIGIT_CONFUSIONS if dataset == "digits" else None
    noisy = inject_noise(y[tr], noise_rate, noise_kind, k, rng, conf)
    return Split(x[tr], y[tr], noisy, x[te], y[te], tr, te)


# ----------------------------------------------------------------- CSV I/O
def read_table(path: str | Path, label_col: str) -> tuple[np.ndarray, list[str], list[str], list[str]]:
    """Read a numeric-feature CSV. Returns (X, raw_labels, feature_names, ids).

    An optional ``id`` column is carried through untouched; every other non-label
    column must be numeric.
    """
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or label_col not in reader.fieldnames:
            raise ValueError(f"label column {label_col!r} not found in {path}")
        feats = [c for c in reader.fieldnames if c not in (label_col, "id")]
        rows, labels, ids = [], [], []
        for line_no, r in enumerate(reader, start=2):
            try:
                rows.append([float(r[c]) for c in feats])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: non-numeric feature ({exc})") from None
            labels.append(r[label_col])
            ids.append(r.get("id") or str(line_no - 2))
    if not rows:
        raise ValueError(f"{path} has no data rows")
    return np.asarray(rows), labels, feats, ids


def encode_labels(raw: list[str]) -> tuple[np.ndarray, list[str]]:
    """Map label strings to 0..K-1 (numeric-aware sort so '10' follows '9')."""
    def key(v: str):
        try:
            return (0, float(v), v)
        except ValueError:
            return (1, 0.0, v)
    classes = sorted(set(raw), key=key)
    lookup = {c: i for i, c in enumerate(classes)}
    return np.array([lookup[v] for v in raw], dtype=int), classes


def write_sample(split: Split, out_dir: str | Path) -> tuple[Path, Path]:
    """Write the noisy training set (what a user would have) and the ground truth."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data_path, truth_path = out / "digits_noisy.csv", out / "truth.csv"
    nf = split.x_train.shape[1]
    with open(data_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", *[f"px{i}" for i in range(nf)], "label"])
        for rid, row, lab in zip(split.train_index, split.x_train, split.y_train_noisy):
            w.writerow([int(rid), *[int(v) for v in row], int(lab)])
    with open(truth_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "given_label", "true_label", "is_flipped"])
        for rid, g, t in zip(split.train_index, split.y_train_noisy, split.y_train_true):
            w.writerow([int(rid), int(g), int(t), int(g != t)])
    return data_path, truth_path
