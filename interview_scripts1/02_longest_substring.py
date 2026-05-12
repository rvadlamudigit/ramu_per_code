"""
Problem 2 — Longest Substring Without Repeating Characters
==========================================================

Given a string `s`, return the length of the longest substring of `s` that
contains no repeated characters.

Constraints
-----------
* 0 <= len(s) <= 5 * 10^4
* `s` may contain English letters, digits, symbols, or spaces.

Examples
--------
Input : "abcabcbb"   ->  Output: 3   (substring "abc")
Input : "bbbbb"      ->  Output: 1   (substring "b")
Input : "pwwkew"     ->  Output: 3   (substring "wke")
Input : ""           ->  Output: 0

Approach
--------
Sliding window over the string with two pointers `left` and `right`. Maintain
a dict `last_seen[char] = index`. When the right pointer encounters a
character that was last seen at index >= left, jump `left` to
`last_seen[char] + 1`. Track the maximum window size as we go.

Time:  O(N)
Space: O(min(N, alphabet_size))
"""


def length_of_longest_substring(s: str) -> int:
    last_seen: dict[str, int] = {}
    left = 0
    best = 0
    for right, ch in enumerate(s):
        if ch in last_seen and last_seen[ch] >= left:
            left = last_seen[ch] + 1
        last_seen[ch] = right
        best = max(best, right - left + 1)
    return best


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {"input": "abcabcbb", "expected": 3},
        {"input": "bbbbb",    "expected": 1},
        {"input": "pwwkew",   "expected": 3},
        {"input": "",         "expected": 0},
        {"input": " ",        "expected": 1},
        {"input": "dvdf",     "expected": 3},
        {"input": "abba",     "expected": 2},
    ]

    for i, c in enumerate(cases, 1):
        got = length_of_longest_substring(c["input"])
        ok = got == c["expected"]
        print(f"Case {i}: input={c['input']!r:>12}  expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}")
