"""
Problem 6 — Number of Islands
=============================

Given an m x n 2D grid where each cell is '1' (land) or '0' (water), return
the number of islands. An island is a group of land cells connected
4-directionally (up / down / left / right). All cells outside the grid are
treated as water.

Constraints
-----------
* 1 <= m, n <= 300
* grid[i][j] is '0' or '1'.

Examples
--------
Input :
  [["1","1","1","1","0"],
   ["1","1","0","1","0"],
   ["1","1","0","0","0"],
   ["0","0","0","0","0"]]
Output: 1

Input :
  [["1","1","0","0","0"],
   ["1","1","0","0","0"],
   ["0","0","1","0","0"],
   ["0","0","0","1","1"]]
Output: 3

Approach
--------
Scan every cell. When we find a '1', increment the island counter and run a
DFS that marks every connected land cell as visited (we flip it to '0' in
place). Each cell is visited at most once.

Time:  O(m * n)
Space: O(m * n) worst case for the DFS stack.
"""

from typing import List


def num_islands(grid: List[List[str]]) -> int:
    if not grid or not grid[0]:
        return 0

    rows, cols = len(grid), len(grid[0])
    # Work on a mutable copy so we don't mutate the caller's grid.
    grid = [row[:] for row in grid]
    count = 0

    def dfs(r: int, c: int) -> None:
        # Iterative DFS to avoid recursion-limit issues on big grids.
        stack = [(r, c)]
        while stack:
            x, y = stack.pop()
            if 0 <= x < rows and 0 <= y < cols and grid[x][y] == "1":
                grid[x][y] = "0"
                stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    for r in range(rows):
        for c in range(cols):
            if grid[r][c] == "1":
                count += 1
                dfs(r, c)
    return count


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cases = [
        {
            "grid": [
                ["1","1","1","1","0"],
                ["1","1","0","1","0"],
                ["1","1","0","0","0"],
                ["0","0","0","0","0"],
            ],
            "expected": 1,
        },
        {
            "grid": [
                ["1","1","0","0","0"],
                ["1","1","0","0","0"],
                ["0","0","1","0","0"],
                ["0","0","0","1","1"],
            ],
            "expected": 3,
        },
        {
            "grid": [["0"]],
            "expected": 0,
        },
        {
            "grid": [["1"]],
            "expected": 1,
        },
        {
            "grid": [
                ["1","0","1","0","1"],
                ["0","1","0","1","0"],
                ["1","0","1","0","1"],
            ],
            "expected": 8,
        },
    ]

    for i, c in enumerate(cases, 1):
        got = num_islands(c["grid"])
        ok = got == c["expected"]
        print(f"Case {i}: grid ({len(c['grid'])}x{len(c['grid'][0])}) expected={c['expected']}  got={got}  {'PASS' if ok else 'FAIL'}")
