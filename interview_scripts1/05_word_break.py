"""
Problem 5 — Word Break
======================

Given a string `s` and a dictionary of strings `word_dict`, return True if
`s` can be segmented into a space-separated sequence of one or more
dictionary words. The same dictionary word may be reused multiple times.

Constraints
-----------
* 1 <= len(s) <= 300
* 1 <= len(word_dict) <= 1000
* 1 <= len(word) <= 20
* All strings are lowercase English letters; words in `word_dict` are unique.

Examples
--------
Input : s="leetcode", word_dict=["leet","code"]           -> True
Input : s="applepenapple", word_dict=["apple","pen"]      -> True
Input : s="catsandog", word_dict=["cats","dog","sand",
                                  "and","cat"]            -> False

Approach
--------
Bottom-up dynamic programming.
  dp[i] = True iff s[:i] can be segmented.
  dp[0] = True (empty prefix).
  dp[i] = True iff there exists j < i such that dp[j] is True
                  AND s[j:i] is in the dictionary.

Time : O(N^2 * L) — N = len(s), L = average word length for hash lookup.
Space: O(N).
"""

from typing import List


def word_break(s: str, word_dict: List[str]) -> bool:
    words = set(word_dict)
    max_word_len = max((len(w) for w in words), default=0)
    n = len(s)
    dp = [False] * (n + 1)
    dp[0] = True
    for i in range(1, n + 1):
        # Only need to look back at most max_word_len characters.
        start = max(0, i - max_word_len)
        for j in range(start, i):
            if dp[j] and s[j:i] in words:
                dp[i] = True
                break
    return dp[n]


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {"s": "leetcode",      "dict": ["leet", "code"],                          "expected": True},
        {"s": "applepenapple", "dict": ["apple", "pen"],                          "expected": True},
        {"s": "catsandog",     "dict": ["cats", "dog", "sand", "and", "cat"],     "expected": False},
        {"s": "a",             "dict": ["a"],                                     "expected": True},
        {"s": "ab",            "dict": ["a"],                                     "expected": False},
        {"s": "aaaaaaa",       "dict": ["aaaa", "aaa"],                           "expected": True},
    ]

    for i, c in enumerate(cases, 1):
        got = word_break(c["s"], c["dict"])
        ok = got == c["expected"]
        print(f"Case {i}: s={c['s']!r:<18} dict={c['dict']}")
        print(f"        expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}\n")
