"""
Program 1 — Sales analytics: groupby + pivot + ranking + YoY.

Generates synthetic sales transactions, then produces:
  1. Monthly revenue per (region, product) pivot.
  2. Top-N products per region by revenue.
  3. Year-over-year growth per product.
  4. A "rolling 3-month" smoothed revenue trend per region.

Demonstrates:
  * pd.MultiIndex pivots
  * groupby + transform vs. agg
  * rank() with method='dense' for top-N
  * pct_change for YoY
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RNG = np.random.default_rng(42)


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

def make_sales(n_rows: int = 5000) -> pd.DataFrame:
    products = ["Notebook", "Pen", "Mug", "Backpack", "Headphones", "Charger"]
    regions = ["North", "South", "East", "West"]
    dates = pd.date_range("2024-01-01", "2025-12-31", freq="D")

    df = pd.DataFrame({
        "date":     RNG.choice(dates, size=n_rows),
        "region":   RNG.choice(regions, size=n_rows),
        "product":  RNG.choice(products, size=n_rows),
        "qty":      RNG.integers(1, 10, size=n_rows),
        "unit_price": RNG.uniform(5, 120, size=n_rows).round(2),
    })
    df["revenue"] = (df["qty"] * df["unit_price"]).round(2)
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.to_period("M")
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def monthly_pivot(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.pivot_table(
            index=["region", "product"],
            columns="month",
            values="revenue",
            aggfunc="sum",
            fill_value=0,
        )
        .round(2)
    )


def top_n_per_region(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Top N products per region by total revenue."""
    grouped = (
        df.groupby(["region", "product"], as_index=False)["revenue"].sum()
    )
    grouped["rank"] = grouped.groupby("region")["revenue"].rank(method="dense", ascending=False)
    top = grouped[grouped["rank"] <= n].sort_values(["region", "rank"])
    return top.reset_index(drop=True)


def yoy_growth(df: pd.DataFrame) -> pd.DataFrame:
    """Year-over-year growth per product."""
    yearly = df.groupby(["product", "year"])["revenue"].sum().unstack("year")
    yearly["yoy_pct"] = ((yearly[2025] - yearly[2024]) / yearly[2024] * 100).round(2)
    return yearly.sort_values("yoy_pct", ascending=False)


def rolling_region_trend(df: pd.DataFrame, window: int = 3) -> pd.DataFrame:
    """Rolling N-month revenue per region."""
    monthly = (
        df.groupby(["region", "month"])["revenue"].sum().reset_index()
    )
    monthly["month_dt"] = monthly["month"].dt.to_timestamp()
    monthly = monthly.sort_values(["region", "month_dt"])
    monthly[f"rev_rolling_{window}m"] = (
        monthly.groupby("region")["revenue"]
               .transform(lambda s: s.rolling(window, min_periods=1).mean())
               .round(2)
    )
    return monthly[["region", "month", "revenue", f"rev_rolling_{window}m"]]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pd.set_option("display.width", 140)
    pd.set_option("display.max_columns", 30)

    df = make_sales(5000)
    print(f"Generated {len(df):,} sales rows from {df.date.min().date()} to {df.date.max().date()}\n")

    print("=== 1. Monthly revenue pivot (first 6 months shown) ===")
    pivot = monthly_pivot(df)
    print(pivot.iloc[:8, :6])
    print()

    print("=== 2. Top 3 products per region ===")
    print(top_n_per_region(df, n=3))
    print()

    print("=== 3. Year-over-year growth per product ===")
    print(yoy_growth(df))
    print()

    print("=== 4. Rolling 3-month revenue trend (head) ===")
    trend = rolling_region_trend(df, window=3)
    print(trend.head(12).to_string(index=False))
