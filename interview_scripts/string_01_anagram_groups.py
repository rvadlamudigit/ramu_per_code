"""
Program 1 — Anagram groups with frequency analysis.

Given a list of words, group anagrams together and report the groups
sorted by size (largest group first). Optionally normalize case and
strip non-letter characters.

Demonstrates:
  * Using a sorted-tuple key (or Counter.frozenset) as a hash key
  * collections.defaultdict
  * Stable secondary sort
  * Generator-based input filtering
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable


def _normalize(word: str, *, case_insensitive: bool = True, letters_only: bool = True) -> str:
    if case_insensitive:
        word = word.lower()
    if letters_only:
        word = re.sub(r"[^a-z]", "", word)
    return word


def anagram_key(word: str) -> tuple[tuple[str, int], ...]:
    """A canonical key that two anagrams share. Counter handles repeats."""
    return tuple(sorted(Counter(word).items()))


def group_anagrams(
    words: Iterable[str],
    *,
    case_insensitive: bool = True,
    letters_only: bool = True,
    min_group_size: int = 2,
) -> list[list[str]]:
    """Return a list of anagram groups, largest first, ties broken alphabetically."""
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for raw in words:
        norm = _normalize(raw, case_insensitive=case_insensitive, letters_only=letters_only)
        if not norm:
            continue
        buckets[anagram_key(norm)].append(raw)

    groups = [sorted(g) for g in buckets.values() if len(g) >= min_group_size]
    # Largest first, then alphabetical by first element for stable output.
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def summary(groups: list[list[str]]) -> dict:
    """Aggregate stats over the result."""
    sizes = [len(g) for g in groups]
    return {
        "total_groups": len(groups),
        "total_words_in_groups": sum(sizes),
        "largest_group_size": max(sizes, default=0),
        "average_group_size": round(sum(sizes) / len(sizes), 2) if sizes else 0,
    }


if __name__ == "__main__":
    sample = [
        "listen", "silent", "enlist", "tinsel",
        "evil", "vile", "live", "veil",
        "rat", "tar", "art",
        "hello",
        "Dormitory", "dirty room",     # phrase anagram
        "The Morse Code", "Here come dots",
        "stressed", "desserts",
        "lonely",
    ]
    groups = group_anagrams(sample)
    print("=== Anagram groups (largest first) ===")
    for i, g in enumerate(groups, 1):
        print(f"  [{len(g)}] group {i}: {g}")
    print("\n=== Summary ===")
    for k, v in summary(groups).items():
        print(f"  {k}: {v}")
