"""Finding elements in windows with huge accessibility trees.

The goblin showcase, November 15th: once the Creation Kit loaded Skyrim's
data its Object Window held thousands of list rows, and every
find_elements walked all of them depth first, three cross-process calls a
node, collecting every match. A lookup for a tree item on the left took
minutes or never came back, and a BAM run attached to that window hung
before its first step. Searches now go breadth first, take big child lists
a page at a time behind everything else, stop at the first match when one
is all the caller needs, read each node's name and type with its parent's
children in one call, and give up with a clear error at a deadline.
"""

from __future__ import annotations

import time

import pytest

from bad_ass_mcp.backend import windows as w
from bad_ass_mcp.types import SearchTimeout
from tests.fake_backend import FAKE_WINDOW_ID, FakeBackend

TREE_ITEM = 50024
LIST_ITEM = 50007
LIST = 50008
PANE = 50033


class Node:
    """A fake UIA element, linked to its parent and siblings."""

    def __init__(self, name, ctype, children=(), delay=0.0):
        self.CachedName = self.CurrentName = name
        self.CachedControlType = self.CurrentControlType = ctype
        self.kids = list(children)
        self.delay = delay
        self.parent = None
        for i, k in enumerate(self.kids):
            k.parent, k.i = self, i

    def FindAllBuildCache(self, scope, cond, cache):
        raise AssertionError("a whole child list must not be fetched at once")

    def FindAll(self, scope, cond):
        raise AssertionError("searches must read children with their properties cached")


class Walker:
    """Counts every step: children are streamed one at a time."""

    steps = 0

    def GetFirstChildElementBuildCache(self, el, cache):
        Walker.steps += 1
        if el.delay:
            time.sleep(el.delay)
        return el.kids[0] if el.kids else None

    def GetNextSiblingElementBuildCache(self, el, cache):
        Walker.steps += 1
        p = el.parent
        return p.kids[el.i + 1] if p and el.i + 1 < len(p.kids) else None


def _ck_window(rows=5000):
    """A tree on the left and a list with `rows` rows, like the CK's Object Window."""
    rows_ = [Node(f"Record{i}", LIST_ITEM, [Node(f"cell{i}", 50020)]) for i in range(rows)]
    tree = Node(
        "tree",
        50023,
        [
            Node("Actors", TREE_ITEM, [Node("Actor", TREE_ITEM)]),
            Node("Items", TREE_ITEM, [Node("Armor", TREE_ITEM), Node("ArmorAddon", TREE_ITEM)]),
        ],
    )
    return Node("Object Window", PANE, [Node("records", LIST, rows_), tree])


def _backend():
    b = object.__new__(w.WindowsBackend)
    b._cache = object()
    b._walker = Walker()
    return b


@pytest.fixture(autouse=True)
def _reset():
    Walker.steps = 0


def test_a_shallow_target_is_found_without_reading_the_big_list():
    root = _ck_window()
    found = _backend()._search(root, "treeitem", "ArmorAddon", limit=1)
    assert [n.CachedName for n in found] == ["ArmorAddon"]
    assert Walker.steps < 120


def test_matches_come_shallow_first():
    root = Node(
        "w",
        PANE,
        [
            Node("a", PANE, [Node("OK", 50000, [Node("OK", 50000)])]),
            Node("OK", 50000),
        ],
    )
    names_depths = [n for n in _backend()._search(root, None, "OK")]
    assert names_depths[0] is root.kids[1]


def test_without_a_limit_every_match_is_still_found():
    root = _ck_window(rows=300)
    found = _backend()._search(root, None, "cell299")
    assert len(found) == 1


def test_a_row_deep_in_the_big_list_is_still_reachable_with_a_limit():
    root = _ck_window(rows=500)
    found = _backend()._search(root, None, "Record480", limit=1)
    assert [n.CachedName for n in found] == ["Record480"]


def test_a_search_past_its_deadline_says_so_instead_of_hanging():
    slow = Node(
        "w",
        PANE,
        [Node(f"n{i}", PANE, [Node("x", PANE)], delay=0.01) for i in range(200)],
        delay=0.01,
    )
    t0 = time.monotonic()
    with pytest.raises(SearchTimeout) as raised:
        _backend()._search(slow, None, "never", deadline=time.monotonic() + 0.2)
    assert time.monotonic() - t0 < 1.0
    assert "searched" in str(raised.value)


def test_find_elements_passes_limit_and_has_a_default_deadline(monkeypatch):
    b = _backend()
    seen = {}

    def fake_search(el, role, name, limit=None, deadline=None):
        seen.update(limit=limit, deadline=deadline)
        return []

    b._search = fake_search
    b._chromium_root = lambda hwnd: hwnd
    b._uia = type("U", (), {"ElementFromHandle": lambda self, h: object()})()
    monkeypatch.setattr(
        w, "_user32", type("U32", (), {"IsWindow": staticmethod(lambda h: True)})(), raising=False
    )
    b.find_elements("123", name="OK", limit=1)
    assert seen["limit"] == 1
    assert seen["deadline"] is not None and seen["deadline"] > time.monotonic()


# ── callers that need one element ask for one ──────────────────────────


class _Recording(FakeBackend):
    def __init__(self):
        super().__init__()
        self.limits = []

    def find_elements(self, window_id, *, role=None, name=None, index=0, limit=None):
        self.limits.append(limit)
        return super().find_elements(window_id, role=role, name=name, index=index)


def test_learn_layout_asks_for_one_match():
    b = _Recording()
    b.learn_layout(FAKE_WINDOW_ID, {"ok": {"name": "OK"}})
    assert b.limits == [1]


def test_waiting_for_an_element_asks_for_one_match():
    b = _Recording()
    b.wait_for_element(FAKE_WINDOW_ID, name="OK", timeout=0.1)
    assert b.limits and set(b.limits) == {1}


def test_waiting_for_an_element_in_a_state_reads_all_matches():
    b = _Recording()
    b.wait_for_element(FAKE_WINDOW_ID, name="OK", state="focused", timeout=0.05)
    assert set(b.limits) == {None}


def test_the_children_cache_carries_name_and_control_type():
    added = []

    class Cache:
        def AddProperty(self, pid):
            added.append(pid)

    b = object.__new__(w.WindowsBackend)
    b._uia = type("U", (), {"CreateCacheRequest": lambda self: Cache()})()
    b._children_cache()
    assert sorted(added) == [30003, 30005]
