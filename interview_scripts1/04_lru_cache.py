"""
Problem 4 — LRU Cache
=====================

Design a data structure that follows the constraints of a Least Recently Used
(LRU) cache.

Implement the class `LRUCache`:

    LRUCache(capacity: int)
        Initialise the cache with positive size `capacity`.

    get(key: int) -> int
        Return the value of the key if it exists, otherwise return -1.
        A `get` counts as a use and refreshes the key's recency.

    put(key: int, value: int) -> None
        Update the value of the key if it exists. Otherwise add the
        key-value pair. If the number of keys exceeds `capacity`, evict the
        least recently used key. A `put` (insert or update) also refreshes
        recency.

Both `get` and `put` must run in O(1) average time.

Constraints
-----------
* 1 <= capacity <= 3000
* 0 <= key, value <= 10^9
* up to 2 * 10^5 calls in total.

Example
-------
ops    = ["LRUCache","put","put","get","put","get","put","get","get","get"]
args   = [   [2],   [1,1],[2,2], [1], [3,3], [2], [4,4], [1], [3], [4]]
output = [  null,   null, null,   1,  null,  -1,  null,  -1,   3,   4]

Approach
--------
HashMap + doubly-linked list:
  - the dict maps `key -> Node` for O(1) lookup,
  - the doubly-linked list keeps nodes ordered by recency
      (head = most recently used, tail = least recently used),
  - on every get/put we move the touched node to the head,
  - on overflow we drop the tail node.
"""

from typing import Optional


class _Node:
    __slots__ = ("key", "value", "prev", "next")

    def __init__(self, key: int, value: int):
        self.key = key
        self.value = value
        self.prev: Optional["_Node"] = None
        self.next: Optional["_Node"] = None


class LRUCache:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._map: dict[int, _Node] = {}
        # Sentinel head/tail to avoid edge-case checks.
        self._head = _Node(0, 0)
        self._tail = _Node(0, 0)
        self._head.next = self._tail
        self._tail.prev = self._head

    # ---- private helpers ----------------------------------------------------
    def _unlink(self, node: _Node) -> None:
        node.prev.next = node.next
        node.next.prev = node.prev

    def _push_front(self, node: _Node) -> None:
        node.prev = self._head
        node.next = self._head.next
        self._head.next.prev = node
        self._head.next = node

    # ---- public API ---------------------------------------------------------
    def get(self, key: int) -> int:
        node = self._map.get(key)
        if node is None:
            return -1
        self._unlink(node)
        self._push_front(node)
        return node.value

    def put(self, key: int, value: int) -> None:
        node = self._map.get(key)
        if node is not None:
            node.value = value
            self._unlink(node)
            self._push_front(node)
            return

        node = _Node(key, value)
        self._map[key] = node
        self._push_front(node)

        if len(self._map) > self.capacity:
            lru = self._tail.prev
            self._unlink(lru)
            del self._map[lru.key]


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    ops    = ["LRUCache", "put", "put", "get", "put", "get", "put", "get", "get", "get"]
    args   = [[2],        [1, 1],[2, 2],[1],   [3, 3],[2],   [4, 4],[1],   [3],   [4]]
    expect = [None,       None,  None,  1,     None,  -1,    None,  -1,    3,     4]

    cache = None
    out = []
    for op, a in zip(ops, args):
        if op == "LRUCache":
            cache = LRUCache(*a)
            out.append(None)
        elif op == "put":
            out.append(cache.put(*a))
        elif op == "get":
            out.append(cache.get(*a))

    print(f"ops     ={ops}")
    print(f"args    ={args}")
    print(f"expected={expect}")
    print(f"got     ={out}")
    print("PASS" if out == expect else "FAIL")
