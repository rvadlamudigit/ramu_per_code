"""
Program 3 — Time-series gap filling + rolling analytics.

Synthetic sensor stream: hourly readings with random gaps and a few
spikes. We:
  1. Resample to a strict hourly grid.
  2. Fill gaps with multiple strategies (ffill, linear interp, seasonal
     mean) and compare residuals.
  3. Compute rolling mean + std and z-score for anomaly flagging.
  4. Detect "long gaps" (>= K consecutive missing hours) for reporting.

Demonstrates:
  * pd.date_range + reindex
  * interpolate with multiple methods
  * groupby on a dt-derived key for seasonal fill
  * rolling().agg with multiple stats
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RNG = np.random.default_rng(11)


# ---------------------------------------------------------------------------
# Synthetic stream with gaps
# ---------------------------------------------------------------------------

def make_sensor_stream(days: int = 14, gap_rate: float = 0.18) -> pd.DataFrame:
    full_idx = pd.date_range("2026-01-01", periods=days * 24, freq="h")

    # Daily seasonality + noise + a couple of spikes.
    hours = np.arange(len(full_idx))
    seasonal = 10 + 4 * np.sin(2 * np.pi * (hours % 24) / 24)
    trend = hours * 0.005
    noise = RNG.normal(0, 0.5, len(full_idx))
    values = seasonal + trend + noise

    spike_idx = RNG.choice(len(full_idx), size=5, replace=False)
    values[spike_idx] += RNG.uniform(8, 14, size=5)

    df = pd.DataFrame({"ts": full_idx, "value": values.round(3)})

    # Drop a fraction of rows to create gaps.
    drop_mask = RNG.random(len(df)) < gap_rate
    return df.loc[~drop_mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Gap filling
# ---------------------------------------------------------------------------

def regularize(stream: pd.DataFrame, freq: str = "h") -> pd.DataFrame:
    """Reindex to a strict regular grid; missing slots become NaN."""
    full = pd.date_range(stream["ts"].min(), stream["ts"].max(), freq=freq)
    out = stream.set_index("ts").reindex(full).rename_axis("ts").reset_index()
    out["is_missing"] = out["value"].isna()
    return out


def fill_strategies(reg: pd.DataFrame) -> pd.DataFrame:
    """Apply several strategies to the same NaN-bearing series."""
    df = reg.copy()
    s = df["value"]

    df["ffill"] = s.ffill()
    df["linear"] = s.interpolate(method="linear")
    # Seasonal: fill NaNs with that hour's mean across the dataset.
    by_hour = df.groupby(df["ts"].dt.hour)["value"].transform("mean")
    df["seasonal"] = s.fillna(by_hour)

    # Compose: linear, then back-fill any leading NaNs.
    df["composite"] = df["linear"].bfill().ffill()
    return df


def long_gaps(reg: pd.DataFrame, min_run: int = 3) -> pd.DataFrame:
    """Find runs of consecutive missing rows of length >= min_run."""
    miss = reg["is_missing"].to_numpy()
    if not miss.any():
        return pd.DataFrame(columns=["start", "end", "length"])
    # Run-length encoding via boundary detection.
    diff = np.diff(miss.astype(int), prepend=0, append=0)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0] - 1
    runs = []
    for s, e in zip(starts, ends):
        if e - s + 1 >= min_run:
            runs.append((reg["ts"].iloc[s], reg["ts"].iloc[e], int(e - s + 1)))
    return pd.DataFrame(runs, columns=["start", "end", "length"])


# ---------------------------------------------------------------------------
# Rolling analytics
# ---------------------------------------------------------------------------

def rolling_anomalies(filled: pd.DataFrame, value_col: str = "composite",
                      window: int = 12, z_threshold: float = 2.5) -> pd.DataFrame:
    df = filled.copy()
    s = df[value_col]
    df["roll_mean"] = s.rolling(window, min_periods=window // 2).mean().round(3)
    df["roll_std"] = s.rolling(window, min_periods=window // 2).std().round(3)
    df["z"] = ((s - df["roll_mean"]) / df["roll_std"]).round(2)
    df["anomaly"] = df["z"].abs() >= z_threshold
    return df[["ts", value_col, "roll_mean", "roll_std", "z", "anomaly"]]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pd.set_option("display.width", 140)

    stream = make_sensor_stream(days=14, gap_rate=0.18)
    print(f"Raw stream: {len(stream):,} readings (after random drops)\n")

    reg = regularize(stream)
    print(f"After reindex: {len(reg):,} hourly slots, {reg['is_missing'].sum()} missing\n")

    filled = fill_strategies(reg)

    print("=== Sample of fill strategies (first 10 rows) ===")
    print(filled.head(10).to_string(index=False))
    print()

    gaps = long_gaps(reg, min_run=3)
    print(f"=== Long gaps (>= 3 consecutive missing hours): {len(gaps)} ===")
    print(gaps.head().to_string(index=False))
    print()

    anomalies = rolling_anomalies(filled)
    flagged = anomalies[anomalies["anomaly"]].head(10)
    print(f"=== Anomalies flagged via rolling z-score (showing {len(flagged)}/{anomalies.anomaly.sum()}) ===")
    print(flagged.to_string(index=False))
