"""
Program 2 — Sliding-window string analytics.

Two related problems solved with the same generalized sliding window:

  (a) Longest substring with no repeating characters.
  (b) Longest substring with at most K distinct characters.

Both run in O(n). The window expands on the right and contracts on
the left whenever the constraint is violated. We return rich results
(start index, length, content) so callers can highlight the match.

Demonstrates:
  * Generic two-pointer / sliding window pattern
  * Reusable predicate-driven implementation
  * dataclasses for structured return values
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass
class WindowMatch:
    start: int
    end: int          # inclusive
    length: int
    content: str

    def highlight(self, full: str, marker: str = "[]") -> str:
        l, r = marker[0], marker[1]
        return full[: self.start] + l + full[self.start : self.end + 1] + r + full[self.end + 1 :]


def longest_unique_substring(s: str) -> WindowMatch:
    """Longest substring with all distinct characters."""
    last_seen: dict[str, int] = {}
    best = WindowMatch(0, -1, 0, "")
    left = 0
    for right, ch in enumerate(s):
        # If we've seen ch inside the current window, jump left past it.
        if ch in last_seen and last_seen[ch] >= left:
            left = last_seen[ch] + 1
        last_seen[ch] = right
        size = right - left + 1
        if size > best.length:
            best = WindowMatch(left, right, size, s[left : right + 1])
    return best


def longest_with_k_distinct(s: str, k: int) -> WindowMatch:
    """Longest substring containing at most k distinct characters."""
    if k <= 0 or not s:
        return WindowMatch(0, -1, 0, "")
    counts: Counter[str] = Counter()
    best = WindowMatch(0, -1, 0, "")
    left = 0
    for right, ch in enumerate(s):
        counts[ch] += 1
        # Shrink window until we have at most k distinct chars.
        while len(counts) > k:
            counts[s[left]] -= 1
            if counts[s[left]] == 0:
                del counts[s[left]]
            left += 1
        size = right - left + 1
        if size > best.length:
            best = WindowMatch(left, right, size, s[left : right + 1])
    return best


def all_unique_windows(s: str, *, min_length: int = 2) -> list[WindowMatch]:
    """Return every maximal substring with all distinct characters."""
    out: list[WindowMatch] = []
    last_seen: dict[str, int] = {}
    left = 0
    for right, ch in enumerate(s):
        if ch in last_seen and last_seen[ch] >= left:
            # Emit the maximal window ending just before ch's previous index.
            length = right - left
            if length >= min_length:
                out.append(WindowMatch(left, right - 1, length, s[left:right]))
            left = last_seen[ch] + 1
        last_seen[ch] = right
    # Tail window
    length = len(s) - left
    if length >= min_length:
        out.append(WindowMatch(left, len(s) - 1, length, s[left:]))
    return out


if __name__ == "__main__":
    text = "abcabcbb_pwwkew_eceba_aabbccdd"
    print(f"Input: {text!r}\n")

    a = longest_unique_substring(text)
    print(f"(a) Longest unique substring: {a.content!r}  len={a.length}  span=[{a.start},{a.end}]")
    print(f"    highlighted: {a.highlight(text)}\n")

    for k in (1, 2, 3):
        b = longest_with_k_distinct(text, k)
        print(f"(b) Longest with at most {k} distinct: {b.content!r}  len={b.length}")
    print()

    print("(c) All maximal unique windows (length >= 3):")
    for w in all_unique_windows(text, min_length=3):
        print(f"    {w.content!r}  span=[{w.start},{w.end}]  len={w.length}")
