"""
Program 4 — Multi-method outlier detection ensemble.

We run three detectors over a numeric column and combine their flags
into a single "outlier score" (0..3). This is more robust than any
single rule on real data.

Detectors:
  * IQR rule      — outside [Q1 - k*IQR, Q3 + k*IQR]
  * Z-score       — |x - mean| / std > z_threshold
  * Modified MAD  — uses median + median absolute deviation, robust to
                    contamination (recommended in Iglewicz & Hoaglin).

Demonstrates:
  * Vectorized boolean logic with numpy/pandas
  * Combining multiple weak detectors
  * Per-group outlier detection (e.g. per region) via groupby+apply
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RNG = np.random.default_rng(3)


# ---------------------------------------------------------------------------
# Synthetic data with planted anomalies
# ---------------------------------------------------------------------------

def make_dataset(n: int = 1000) -> pd.DataFrame:
    region = RNG.choice(["A", "B", "C"], size=n, p=[0.5, 0.3, 0.2])
    # Each region has its own mean/std.
    means = {"A": 100, "B": 50, "C": 200}
    stds = {"A": 10, "B": 5, "C": 25}
    value = np.array([RNG.normal(means[r], stds[r]) for r in region])

    # Inject ~3% extreme outliers + ~2% subtle ones.
    n_extreme = int(0.03 * n)
    n_subtle = int(0.02 * n)
    extreme_idx = RNG.choice(n, n_extreme, replace=False)
    subtle_idx = RNG.choice(np.setdiff1d(np.arange(n), extreme_idx), n_subtle, replace=False)
    value[extreme_idx] += RNG.choice([-1, 1], n_extreme) * RNG.uniform(80, 200, n_extreme)
    value[subtle_idx] += RNG.choice([-1, 1], n_subtle) * RNG.uniform(15, 30, n_subtle)

    df = pd.DataFrame({"region": region, "value": value.round(2)})
    df["is_planted_extreme"] = False
    df.loc[extreme_idx, "is_planted_extreme"] = True
    return df


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def iqr_flags(s: pd.Series, k: float = 1.5) -> pd.Series:
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    return (s < q1 - k * iqr) | (s > q3 + k * iqr)


def zscore_flags(s: pd.Series, threshold: float = 3.0) -> pd.Series:
    mean, std = s.mean(), s.std(ddof=0)
    if std == 0:
        return pd.Series(False, index=s.index)
    return ((s - mean).abs() / std) > threshold


def mad_flags(s: pd.Series, threshold: float = 3.5) -> pd.Series:
    """Modified z-score using median + MAD."""
    med = s.median()
    mad = (s - med).abs().median()
    if mad == 0:
        return pd.Series(False, index=s.index)
    modz = 0.6745 * (s - med) / mad
    return modz.abs() > threshold


# ---------------------------------------------------------------------------
# Ensemble
# ---------------------------------------------------------------------------

def detect_outliers(df: pd.DataFrame, value_col: str = "value",
                    group_col: str | None = None) -> pd.DataFrame:
    """Run the three detectors (optionally per-group) and return flags."""
    out = df.copy()

    def run(s: pd.Series) -> pd.DataFrame:
        return pd.DataFrame({
            "iqr_outlier": iqr_flags(s),
            "z_outlier":   zscore_flags(s),
            "mad_outlier": mad_flags(s),
        })

    if group_col is None:
        flags = run(df[value_col])
    else:
        flags = (
            df.groupby(group_col, group_keys=False)[value_col]
              .apply(run)
              .reindex(df.index)
        )

    out = pd.concat([out, flags], axis=1)
    out["outlier_score"] = out[["iqr_outlier", "z_outlier", "mad_outlier"]].sum(axis=1)
    out["is_outlier"] = out["outlier_score"] >= 2  # majority vote
    return out


def evaluate_against_truth(scored: pd.DataFrame) -> dict:
    """Quick precision/recall vs. the planted extreme outliers."""
    if "is_planted_extreme" not in scored.columns:
        return {}
    tp = ((scored.is_outlier) & (scored.is_planted_extreme)).sum()
    fp = ((scored.is_outlier) & (~scored.is_planted_extreme)).sum()
    fn = ((~scored.is_outlier) & (scored.is_planted_extreme)).sum()
    return {
        "true_positive": int(tp),
        "false_positive": int(fp),
        "false_negative": int(fn),
        "precision": round(tp / (tp + fp), 3) if tp + fp else 0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 0,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    df = make_dataset(2000)
    print(f"Dataset: {len(df):,} rows, {df.is_planted_extreme.sum()} planted extreme outliers\n")

    print("=== Global detectors ===")
    global_res = detect_outliers(df, "value", group_col=None)
    print(global_res[["region", "value", "iqr_outlier", "z_outlier", "mad_outlier",
                      "outlier_score", "is_outlier"]].head(10).to_string(index=False))
    print(f"  flagged: {global_res.is_outlier.sum()}")
    print(f"  vs truth: {evaluate_against_truth(global_res)}\n")

    print("=== Per-region detectors (much better when regions have different scales) ===")
    grouped_res = detect_outliers(df, "value", group_col="region")
    print(f"  flagged: {grouped_res.is_outlier.sum()}")
    print(f"  vs truth: {evaluate_against_truth(grouped_res)}")

    print("\n=== Per-region summary ===")
    summary = (
        grouped_res.groupby("region")
        .agg(rows=("value", "size"),
             flagged=("is_outlier", "sum"),
             pct_flagged=("is_outlier", "mean"))
    )
    summary["pct_flagged"] = (summary["pct_flagged"] * 100).round(2)
    print(summary)
