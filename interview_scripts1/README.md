# Python Interview Problems — Medium Level

Eight self-contained Python problems suitable for a mid-level engineering interview.
Each script:

* has the problem description in the module docstring,
* includes worked examples (input / expected output),
* provides a clean reference solution,
* runs a small test harness when executed directly (`python <file>.py`).

| # | File | Topic | Core technique |
|---|------|-------|----------------|
| 1 | `01_group_anagrams.py`       | Group anagrams                            | hash map, sorted-key bucketing |
| 2 | `02_longest_substring.py`    | Longest substring without repeats         | sliding window |
| 3 | `03_top_k_frequent.py`       | Top-K frequent elements                   | counter + heap |
| 4 | `04_lru_cache.py`            | LRU cache                                 | doubly-linked list + hash map |
| 5 | `05_word_break.py`           | Word break (sentence segmentation)        | dynamic programming |
| 6 | `06_number_of_islands.py`    | Count islands in a grid                   | DFS / BFS on a matrix |
| 7 | `07_merge_intervals.py`      | Merge overlapping intervals               | sort + sweep |
| 8 | `08_course_schedule.py`      | Detect cycle in course prerequisites      | topological sort / DFS coloring |

Run any script directly:

```bash
python 01_group_anagrams.py
```

Each script prints the **input** and the **expected vs. actual output** for every test case.
