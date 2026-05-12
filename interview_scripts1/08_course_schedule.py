"""
Problem 8 — Course Schedule (Cycle detection in a DAG)
======================================================

There are `num_courses` courses labelled 0 .. num_courses-1. You are given a
list `prerequisites` where prerequisites[i] = [a, b] means you must take
course `b` before course `a`. Return True if you can finish all courses,
otherwise return False.

This is equivalent to asking: does the directed graph (b -> a edge for each
pair) contain a cycle? If yes, no valid ordering exists.

Constraints
-----------
* 1 <= num_courses <= 2000
* 0 <= len(prerequisites) <= 5000
* prerequisites[i].length == 2
* 0 <= a, b < num_courses
* All pairs are unique.

Examples
--------
Input : num_courses=2, prerequisites=[[1,0]]            -> True
Input : num_courses=2, prerequisites=[[1,0],[0,1]]      -> False
Input : num_courses=4, prerequisites=[[1,0],[2,1],[3,2]] -> True

Approach
--------
DFS with three-state coloring (Tarjan-style cycle detection):

    WHITE = unvisited
    GRAY  = in the current DFS stack
    BLACK = fully processed

If a DFS step ever hits a GRAY neighbour we've found a back-edge => cycle.

Time:  O(V + E)
Space: O(V + E)
"""

from collections import defaultdict
from typing import List


WHITE, GRAY, BLACK = 0, 1, 2


def can_finish(num_courses: int, prerequisites: List[List[int]]) -> bool:
    graph: dict[int, list[int]] = defaultdict(list)
    for a, b in prerequisites:
        graph[b].append(a)              # edge: b -> a  (take b before a)

    color = [WHITE] * num_courses

    def has_cycle(node: int) -> bool:
        stack = [(node, iter(graph[node]))]
        color[node] = GRAY
        while stack:
            cur, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[cur] = BLACK
                stack.pop()
                continue
            if color[nxt] == GRAY:
                return True
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                stack.append((nxt, iter(graph[nxt])))
        return False

    for course in range(num_courses):
        if color[course] == WHITE and has_cycle(course):
            return False
    return True


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {"n": 2, "pre": [[1, 0]],                              "expected": True},
        {"n": 2, "pre": [[1, 0], [0, 1]],                      "expected": False},
        {"n": 4, "pre": [[1, 0], [2, 1], [3, 2]],              "expected": True},
        {"n": 5, "pre": [[1, 0], [2, 1], [3, 2], [4, 3], [1, 4]], "expected": False},
        {"n": 1, "pre": [],                                    "expected": True},
        {"n": 3, "pre": [[0, 1], [0, 2], [1, 2]],              "expected": True},
    ]

    for i, c in enumerate(cases, 1):
        got = can_finish(c["n"], c["pre"])
        ok = got == c["expected"]
        print(f"Case {i}: num_courses={c['n']}, prerequisites={c['pre']}")
        print(f"        expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}\n")
