"""
Program 4 — Spell checker with weighted edit distance.

Two layers:

  1. Classic Levenshtein distance (DP, O(m*n)).
  2. A weighted variant that costs adjacent-keyboard substitutions less
     than far-away ones (so 'helo' -> 'hello' is cheaper than 'helo' -> 'help').

Then a simple suggestion engine that finds the top-K closest words from
a dictionary, ranked by (weighted distance, word frequency).

Demonstrates:
  * 2D DP with rolling rows
  * Heap-based top-K
  * Composition: building suggestions on top of the distance fn
"""
from __future__ import annotations

import heapq
from collections import Counter
from functools import lru_cache
from typing import Iterable

# ---------------------------------------------------------------------------
# Distance functions
# ---------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    """Classic edit distance with O(min(m,n)) memory."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,                       # delete
                cur[j - 1] + 1,                    # insert
                prev[j - 1] + (ca != cb),          # substitute
            )
        prev = cur
    return prev[-1]


# Keyboard layout used to weight substitutions. Adjacency is a graph
# (letter -> set of neighbors). Substituting between neighbors costs 0.5;
# any other substitution costs 1.0.
_KEYBOARD_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


@lru_cache(maxsize=1)
def _keyboard_neighbors() -> dict[str, set[str]]:
    nb: dict[str, set[str]] = {}
    for r, row in enumerate(_KEYBOARD_ROWS):
        for c, ch in enumerate(row):
            s = nb.setdefault(ch, set())
            for dr, dc in ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (1, 1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < len(_KEYBOARD_ROWS) and 0 <= nc < len(_KEYBOARD_ROWS[nr]):
                    s.add(_KEYBOARD_ROWS[nr][nc])
    return nb


def weighted_distance(a: str, b: str) -> float:
    """Levenshtein with cheaper substitutions for adjacent keys."""
    nb = _keyboard_neighbors()
    a, b = a.lower(), b.lower()
    if not a:
        return float(len(b))
    if not b:
        return float(len(a))
    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)] + [0.0] * len(b)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                sub_cost = 0.0
            elif cb in nb.get(ca, ()):
                sub_cost = 0.5
            else:
                sub_cost = 1.0
            cur[j] = min(
                prev[j] + 1.0,
                cur[j - 1] + 1.0,
                prev[j - 1] + sub_cost,
            )
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Suggestion engine
# ---------------------------------------------------------------------------

class SpellChecker:
    """Wrap a dictionary + per-word frequencies for ranked suggestions."""

    def __init__(self, words: Iterable[str], frequencies: dict[str, int] | None = None):
        self.words: list[str] = sorted(set(w.lower() for w in words))
        self.freq = Counter()
        if frequencies:
            self.freq.update({w.lower(): n for w, n in frequencies.items()})

    def is_known(self, word: str) -> bool:
        return word.lower() in self.freq or word.lower() in self.words

    def suggest(self, word: str, k: int = 5, max_distance: float = 3.0) -> list[tuple[str, float]]:
        """Return up to k (word, distance) pairs ranked by distance then freq."""
        word = word.lower()
        if self.is_known(word):
            return [(word, 0.0)]

        # Use a heap so we don't sort the whole dictionary for big lists.
        heap: list[tuple[float, int, str]] = []
        for cand in self.words:
            # Quick length filter — words too different in length can't win.
            if abs(len(cand) - len(word)) > max_distance:
                continue
            d = weighted_distance(word, cand)
            if d <= max_distance:
                # Negative freq so higher freq wins ties (smaller in min-heap terms).
                heapq.heappush(heap, (d, -self.freq.get(cand, 0), cand))

        heap.sort()
        return [(c, d) for d, _, c in heap[:k]]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dictionary = [
        "hello", "help", "yellow", "fellow", "hollow",
        "world", "word", "ward", "weird",
        "python", "pythons", "pylon",
        "string", "strong", "stringer",
        "manipulate", "manipulation",
        "data", "database", "dataset",
        "complex", "complete", "compile",
    ]
    freq = {"hello": 1000, "world": 950, "python": 800, "data": 700, "string": 500}

    sc = SpellChecker(dictionary, freq)

    typos = ["helo", "wrold", "pyhton", "stirng", "manipluate", "dat", "complx"]

    print(f"{'typo':<12} | {'top suggestions (word, weighted distance)'}")
    print("-" * 70)
    for t in typos:
        suggestions = sc.suggest(t, k=3)
        rendered = ", ".join(f"{w}({d:.1f})" for w, d in suggestions) or "(no match)"
        print(f"{t:<12} | {rendered}")

    print()
    print(f"levenshtein('kitten','sitting') = {levenshtein('kitten', 'sitting')}")
    print(f"weighted_distance('helo','hello') = {weighted_distance('helo', 'hello'):.2f}")
    print(f"weighted_distance('xelo','hello') = {weighted_distance('xelo', 'hello'):.2f}  (h~g/j neighbors)")
