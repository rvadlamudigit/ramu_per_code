"""
Program 5 — Fuzzy join between two datasets with imperfect keys.

You have two CSVs that should join on company name, but the keys are
dirty (case, punctuation, "Inc" vs "Incorporated", typos). A plain
merge would lose 30% of the matches. We:

  1. Normalize names (case, punctuation, common stopwords).
  2. Block by first letter (or first n-gram) to avoid an O(N*M) compare.
  3. Score remaining candidates with a similarity function (token-set
     ratio reimplemented in pure Python — no external deps).
  4. Accept matches above a threshold; report unmatched rows.

Demonstrates:
  * Pure-Python token-set similarity
  * Blocking strategy for fuzzy joins (essential at any real scale)
  * Reporting matched + unmatched cleanly via outer-join semantics
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Synthetic dirty data
# ---------------------------------------------------------------------------

LEFT_ROWS = [
    ("L01", "Acme Corp",                "USA",   "1985"),
    ("L02", "Globex Incorporated",      "USA",   "1989"),
    ("L03", "Initech, LLC",             "USA",   "1996"),
    ("L04", "Soylent Industries",       "USA",   "1973"),
    ("L05", "Umbrella Corp.",           "USA",   "1968"),
    ("L06", "Hooli Inc",                "USA",   "2014"),
    ("L07", "Pied Piper Software",      "USA",   "2014"),
    ("L08", "Massive Dynamic",          "USA",   "1994"),
    ("L09", "Stark Industries",         "USA",   "1939"),
    ("L10", "Wayne Enterprises",        "USA",   "1939"),
]
RIGHT_ROWS = [
    ("R01", "ACME CORPORATION",         120),
    ("R02", "Globex, Inc.",             340),
    ("R03", "INITECH",                  85),
    ("R04", "Soylent Industies",        450),       # typo
    ("R05", "Umbrella Corporation",     900),
    ("R06", "Hooli Incorporated",       1200),
    ("R07", "Pied Piper SW",            45),        # abbreviation
    ("R08", "Massive Dynamic Co",       210),
    ("R09", "Stark Inds",               670),       # abbreviation
    ("R10", "Wayne Enterprises Group",  1400),
    ("R11", "TotallyNewCo",             10),        # has no match in left
]

LEFT = pd.DataFrame(LEFT_ROWS,  columns=["left_id", "name", "country", "founded"])
RIGHT = pd.DataFrame(RIGHT_ROWS, columns=["right_id", "name", "employees"])


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "llc", "ltd", "limited", "group", "holdings", "industries",
    "industies",  # cover the typo too
    "the", "and", "&",
}
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    n = name.lower()
    n = _PUNCT_RE.sub(" ", n)
    tokens = [t for t in n.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def tokens(name: str) -> set[str]:
    return set(normalize(name).split())


# ---------------------------------------------------------------------------
# Similarity (token-set Jaccard with a Levenshtein-tolerant token compare)
# ---------------------------------------------------------------------------

def _lev(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def _tokens_match(t1: str, t2: str) -> bool:
    """Two tokens match if they're identical OR within a tolerant edit distance."""
    if t1 == t2:
        return True
    short, long = sorted((t1, t2), key=len)
    if len(long) - len(short) > 3:
        return False
    # Allow ~25% edits, min 1.
    tol = max(1, len(long) // 4)
    return _lev(t1, t2) <= tol


def similarity(name_a: str, name_b: str) -> float:
    """Fuzzy token-set ratio in [0, 1]."""
    ta, tb = tokens(name_a), tokens(name_b)
    if not ta or not tb:
        return 0.0

    # Greedy bipartite match between fuzzy tokens.
    matched_b = set()
    matches = 0
    for x in ta:
        for y in tb - matched_b:
            if _tokens_match(x, y):
                matched_b.add(y)
                matches += 1
                break
    union = len(ta | tb)
    return matches / union if union else 0.0


# ---------------------------------------------------------------------------
# Blocked fuzzy join
# ---------------------------------------------------------------------------

def fuzzy_join(left: pd.DataFrame, right: pd.DataFrame, *,
               left_key: str = "name", right_key: str = "name",
               threshold: float = 0.55,
               block_chars: int = 2) -> pd.DataFrame:
    """Return an outer join with similarity scores."""

    # Build blocks on first N alphanumeric chars of the normalized name.
    def block_key(name: str) -> str:
        n = normalize(name).replace(" ", "")
        return n[:block_chars] if n else ""

    blocks: dict[str, list[int]] = defaultdict(list)
    for idx, n in zip(right.index, right[right_key]):
        blocks[block_key(n)].append(idx)

    matched_rows = []
    used_right: set[int] = set()

    for li, lrow in left.iterrows():
        ln = lrow[left_key]
        candidates: Iterable[int] = blocks.get(block_key(ln), [])
        best_score = 0.0
        best_idx = None
        for ri in candidates:
            sc = similarity(ln, right.loc[ri, right_key])
            if sc > best_score:
                best_score = sc
                best_idx = ri
        if best_idx is not None and best_score >= threshold:
            used_right.add(best_idx)
            merged = {**lrow.to_dict(), **right.loc[best_idx].to_dict(),
                      "similarity": round(best_score, 3)}
            matched_rows.append(merged)
        else:
            matched_rows.append({**lrow.to_dict(), "similarity": round(best_score, 3),
                                 "right_id": None})

    # Unmatched right rows
    for ri in right.index:
        if ri not in used_right:
            row = {col: None for col in left.columns}
            row.update(right.loc[ri].to_dict())
            row["similarity"] = None
            matched_rows.append(row)

    return pd.DataFrame(matched_rows)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pd.set_option("display.width", 140)
    pd.set_option("display.max_columns", 20)

    print("=== LEFT ===")
    print(LEFT.to_string(index=False))
    print("\n=== RIGHT ===")
    print(RIGHT.to_string(index=False))

    joined = fuzzy_join(LEFT, RIGHT, threshold=0.55)
    print("\n=== Fuzzy outer join ===")
    cols = ["left_id", "name", "right_id", "employees", "similarity"]
    # Two name columns after merge — disambiguate.
    if "name_x" in joined.columns:
        cols = ["left_id", "name_x", "right_id", "name_y", "employees", "similarity"]
    elif joined.columns.tolist().count("name") > 1:
        joined.columns = [c if joined.columns.tolist().count(c) == 1 else f"{c}_dup{i}"
                          for i, c in enumerate(joined.columns)]
    print(joined.to_string(index=False))

    matched = joined.dropna(subset=["left_id", "right_id"])
    only_left  = joined[joined["right_id"].isna() & joined["left_id"].notna()]
    only_right = joined[joined["left_id"].isna() & joined["right_id"].notna()]
    print(f"\nMatched: {len(matched)}  |  Only-left: {len(only_left)}  |  Only-right: {len(only_right)}")
    print(f"Average similarity on matches: {matched['similarity'].mean():.3f}")
