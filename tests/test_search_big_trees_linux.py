"""Finding elements in big AT-SPI trees.

The Linux half of test_search_big_trees.py. Every AT-SPI read is a call
across the accessibility bus, so a search that walks a list of thousands
of rows depth first pays for each of them before it reaches the button
beside the list. Searches go breadth first, read a big container's
children a page at a time behind everything else, stop once the caller
has as many matches as it asked for, and give up with a clear error at a
deadline.
"""

from __future__ import annotations

import time

import pytest

from bad_ass_mcp.types import SearchTimeout
from tests.test_linux_internals import _make_backend


class Node:
    """A fake AT-SPI accessible that counts how many children are fetched."""

    fetched = 0

    def __init__(self, name, role, children=(), delay=0.0):
        self.name, self.role = name, role
        self.kids = list(children)
        self.delay = delay

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        if self.delay:
            time.sleep(self.delay)
        return len(self.kids)

    def get_child_at_index(self, i):
        Node.fetched += 1
        return self.kids[i]


def _big_window(rows=5000):
    """A tree on the left and a list with `rows` rows."""
    rows_ = [Node(f"Record{i}", "list item", [Node(f"cell{i}", "label")]) for i in range(rows)]
    tree = Node(
        "tree",
        "tree",
        [
            Node("Actors", "tree item", [Node("Actor", "tree item")]),
            Node(
                "Items", "tree item", [Node("Armor", "tree item"), Node("ArmorAddon", "tree item")]
            ),
        ],
    )
    return Node("Object Window", "frame", [Node("records", "list", rows_), tree])


@pytest.fixture
def backend():
    Node.fetched = 0
    return _make_backend()[0]


def test_a_shallow_target_is_found_without_reading_the_big_list(backend):
    found = backend._search(_big_window(), "tree item", "ArmorAddon", limit=1)
    assert [n.name for n in found] == ["ArmorAddon"]
    assert Node.fetched < 120


def test_matches_come_shallow_first(backend):
    root = Node(
        "w",
        "frame",
        [
            Node("a", "panel", [Node("OK", "button", [Node("OK", "button")])]),
            Node("OK", "button"),
        ],
    )
    assert backend._search(root, None, "OK")[0] is root.kids[1]


def test_without_a_limit_every_match_is_still_found(backend):
    found = backend._search(_big_window(rows=300), None, "cell299")
    assert len(found) == 1


def test_a_row_deep_in_the_big_list_is_still_reachable_with_a_limit(backend):
    found = backend._search(_big_window(rows=500), None, "Record480", limit=1)
    assert [n.name for n in found] == ["Record480"]


def test_the_starting_node_can_match(backend):
    root = Node("w", "frame", [Node("x", "panel")])
    assert backend._search(root, "frame", "w") == [root]


def test_a_node_that_fails_to_answer_is_skipped(backend):
    class Dead(Node):
        def get_role_name(self):
            raise RuntimeError("defunct")

    root = Node("w", "frame", [Dead("gone", "panel"), Node("OK", "button")])
    assert [n.name for n in backend._search(root, None, "OK")] == ["OK"]


def test_a_search_past_its_deadline_says_so_instead_of_hanging(backend):
    slow = Node(
        "w",
        "frame",
        [Node(f"n{i}", "panel", [Node("x", "panel")], delay=0.01) for i in range(200)],
    )
    t0 = time.monotonic()
    with pytest.raises(SearchTimeout) as raised:
        backend._search(slow, None, "never", deadline=time.monotonic() + 0.2)
    assert time.monotonic() - t0 < 1.0
    assert "searched" in str(raised.value)


def test_find_elements_passes_limit_and_has_a_default_deadline(backend):
    seen = {}

    def fake_search(node, role, name, limit=None, deadline=None):
        seen.update(limit=limit, deadline=deadline)
        return []

    backend._search = fake_search
    backend._find_app_waking = lambda window_id: object()
    backend.find_elements("123", name="OK", limit=1)
    assert seen["limit"] == 1
    assert seen["deadline"] is not None and seen["deadline"] > time.monotonic()
