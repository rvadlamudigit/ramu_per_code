"""
Program 2 — Customer cohort retention matrix.

For each user, the cohort is the month of their first purchase. Then for
each cohort we compute, for every subsequent month, what fraction of
those users came back. The result is a triangular retention matrix
that reads like the classic "% of cohort active in month N".

Demonstrates:
  * groupby().transform('min') for per-user features
  * Period arithmetic for "months since signup"
  * pivot_table with normalization to compute a percentage matrix
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RNG = np.random.default_rng(7)


# ---------------------------------------------------------------------------
# Synthetic transactions
# ---------------------------------------------------------------------------

def make_transactions(n_users: int = 1500, months: int = 12) -> pd.DataFrame:
    """Each user signs up in some month; then has a random retention curve."""
    rows = []
    start = pd.Timestamp("2025-01-01")
    for uid in range(n_users):
        signup_month = RNG.integers(0, months - 2)  # leave at least 2 months runway
        # Simulated retention probability decays over time.
        for offset in range(months - signup_month):
            base_prob = 0.65 * (0.78 ** offset)
            if offset == 0 or RNG.random() < base_prob:
                # 1-3 transactions in that month
                for _ in range(RNG.integers(1, 4)):
                    day = RNG.integers(1, 28)
                    ts = (start + pd.DateOffset(months=int(signup_month + offset))).replace(day=int(day))
                    rows.append((uid, ts))
    return pd.DataFrame(rows, columns=["user_id", "ts"])


# ---------------------------------------------------------------------------
# Cohort retention
# ---------------------------------------------------------------------------

def cohort_retention(tx: pd.DataFrame) -> pd.DataFrame:
    """Return a (cohort_month x months_since_signup) retention % matrix."""
    df = tx.copy()
    df["month"] = df["ts"].dt.to_period("M")

    # Each user's cohort = first activity month.
    df["cohort"] = df.groupby("user_id")["month"].transform("min")
    df["months_since"] = (df["month"] - df["cohort"]).apply(lambda x: x.n)

    # Active users per (cohort, months_since)
    active = (
        df.groupby(["cohort", "months_since"])["user_id"]
          .nunique()
          .unstack("months_since")
          .fillna(0)
          .astype(int)
    )
    cohort_size = active.iloc[:, 0]
    retention = active.divide(cohort_size, axis=0).round(3) * 100
    retention.attrs["cohort_size"] = cohort_size
    return retention


def render_retention(matrix: pd.DataFrame, max_periods: int = 8) -> str:
    cohort_size = matrix.attrs["cohort_size"]
    df = matrix.iloc[:, :max_periods].copy()
    df.insert(0, "cohort_size", cohort_size)
    return df.to_string(float_format=lambda v: f"{v:5.1f}" if isinstance(v, float) else str(v))


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tx = make_transactions(n_users=1500, months=12)
    print(f"Generated {len(tx):,} transactions across {tx.user_id.nunique():,} users\n")

    matrix = cohort_retention(tx)

    print("=== Retention matrix (% of cohort active in month N) ===\n")
    print(render_retention(matrix, max_periods=8))

    print("\n=== Average curve across all cohorts ===")
    avg = matrix.mean(axis=0).round(1)
    for period, val in avg.head(8).items():
        bar = "#" * int(val / 4)
        print(f"  M{period:>2}: {val:5.1f}%  {bar}")
