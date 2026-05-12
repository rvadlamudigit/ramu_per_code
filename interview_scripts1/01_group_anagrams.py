"""
Problem 1 — Group Anagrams
==========================

Given an array of strings, group the anagrams together. An anagram is a word
formed by rearranging the letters of another word, using each original letter
exactly once.

Return the groups in any order; the words inside each group may be in any
order as well.

Constraints
-----------
* 1 <= len(strs) <= 10_000
* 0 <= len(strs[i]) <= 100
* strs[i] consists of lowercase English letters.

Example
-------
Input :  ["eat", "tea", "tan", "ate", "nat", "bat"]
Output:  [["bat"], ["nat", "tan"], ["ate", "eat", "tea"]]

Approach
--------
Two words are anagrams iff their sorted letters are equal. Bucket words by
their sorted-letter key in a dictionary. O(N * K log K) time, O(N * K) space,
where K is the maximum word length.
"""

from collections import defaultdict
from typing import List


def group_anagrams(words: List[str]) -> List[List[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for w in words:
        key = "".join(sorted(w))
        buckets[key].append(w)
    return [sorted(group) for group in buckets.values()]


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
def _normalize(groups: List[List[str]]) -> List[List[str]]:
    """Sort each group and the list of groups so equality is order-free."""
    return sorted(sorted(g) for g in groups)


if __name__ == "__main__":
    cases = [
        {
            "input":    ["eat", "tea", "tan", "ate", "nat", "bat"],
            "expected": [["bat"], ["nat", "tan"], ["ate", "eat", "tea"]],
        },
        {
            "input":    [""],
            "expected": [[""]],
        },
        {
            "input":    ["a"],
            "expected": [["a"]],
        },
        {
            "input":    ["abc", "bca", "cab", "xyz", "zxy", "foo"],
            "expected": [["abc", "bca", "cab"], ["xyz", "zxy"], ["foo"]],
        },
    ]

    for i, c in enumerate(cases, 1):
        got = group_anagrams(c["input"])
        ok = _normalize(got) == _normalize(c["expected"])
        print(f"Case {i}: input={c['input']}")
        print(f"        expected={_normalize(c['expected'])}")
        print(f"        got     ={_normalize(got)}")
        print(f"        {'PASS' if ok else 'FAIL'}\n")
