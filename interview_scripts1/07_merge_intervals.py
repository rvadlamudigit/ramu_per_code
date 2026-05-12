"""
Problem 7 — Merge Intervals
===========================

Given an array of intervals where each interval is `[start, end]`, merge all
overlapping intervals and return an array of non-overlapping intervals that
cover all the intervals in the input. Two intervals are considered
overlapping if they share any point (touching intervals count as
overlapping: e.g. [1,4] and [4,5] merge into [1,5]).

Constraints
-----------
* 1 <= len(intervals) <= 10^4
* intervals[i] = [start_i, end_i], 0 <= start_i <= end_i <= 10^4

Examples
--------
Input : [[1,3],[2,6],[8,10],[15,18]]   -> [[1,6],[8,10],[15,18]]
Input : [[1,4],[4,5]]                  -> [[1,5]]
Input : [[1,4],[0,4]]                  -> [[0,4]]
Input : [[1,4],[2,3]]                  -> [[1,4]]   (one nested inside the other)

Approach
--------
1. Sort by start time — O(N log N).
2. Sweep through the sorted list; for each interval either extend the last
   merged interval (when overlap/touch) or start a new merged interval.
   O(N).

Total: O(N log N) time, O(N) extra space for the output list.
"""

from typing import List


def merge_intervals(intervals: List[List[int]]) -> List[List[int]]:
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged: list[list[int]] = [intervals[0][:]]
    for start, end in intervals[1:]:
        last = merged[-1]
        if start <= last[1]:           # overlap or touch
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {"input": [[1, 3], [2, 6], [8, 10], [15, 18]], "expected": [[1, 6], [8, 10], [15, 18]]},
        {"input": [[1, 4], [4, 5]],                    "expected": [[1, 5]]},
        {"input": [[1, 4], [0, 4]],                    "expected": [[0, 4]]},
        {"input": [[1, 4], [2, 3]],                    "expected": [[1, 4]]},
        {"input": [[1, 10], [2, 3], [4, 5], [6, 7]],   "expected": [[1, 10]]},
        {"input": [[1, 2]],                            "expected": [[1, 2]]},
    ]

    for i, c in enumerate(cases, 1):
        got = merge_intervals([row[:] for row in c["input"]])
        ok = got == c["expected"]
        print(f"Case {i}: input={c['input']}")
        print(f"        expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}\n")
