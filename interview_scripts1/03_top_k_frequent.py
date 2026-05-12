"""
Problem 3 — Top K Frequent Elements
===================================

Given an integer array `nums` and an integer `k`, return the `k` most
frequent elements. The answer may be returned in any order. It is guaranteed
that the answer is unique (no ties at the boundary).

Constraints
-----------
* 1 <= len(nums) <= 10^5
* k is in the range [1, number of unique elements in nums]

Examples
--------
Input : nums=[1,1,1,2,2,3], k=2       -> Output: [1, 2]
Input : nums=[1], k=1                 -> Output: [1]
Input : nums=[4,4,4,5,5,6,6,6,6], k=2 -> Output: [6, 4]

Approach
--------
1. Build a frequency counter with collections.Counter — O(N).
2. Use a min-heap of size k on (count, value). Push every (count, value); pop
   when the heap exceeds size k. What remains are the k most frequent.
   Total: O(N log k).

(`Counter.most_common(k)` does the same thing internally; we implement it by
hand to make the heap usage explicit, which is what an interviewer is after.)
"""

import heapq
from collections import Counter
from typing import List


def top_k_frequent(nums: List[int], k: int) -> List[int]:
    counts = Counter(nums)
    heap: list[tuple[int, int]] = []
    for value, count in counts.items():
        heapq.heappush(heap, (count, value))
        if len(heap) > k:
            heapq.heappop(heap)
    # Return values ordered from most to least frequent for readability.
    return [v for _, v in sorted(heap, key=lambda x: -x[0])]


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {"nums": [1, 1, 1, 2, 2, 3],            "k": 2, "expected": [1, 2]},
        {"nums": [1],                           "k": 1, "expected": [1]},
        {"nums": [4, 4, 4, 5, 5, 6, 6, 6, 6],   "k": 2, "expected": [6, 4]},
        {"nums": [-1, -1, -1, 2, 2, 100],       "k": 1, "expected": [-1]},
    ]

    for i, c in enumerate(cases, 1):
        got = top_k_frequent(c["nums"], c["k"])
        ok = sorted(got) == sorted(c["expected"])
        print(f"Case {i}: nums={c['nums']}, k={c['k']}")
        print(f"        expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}\n")
