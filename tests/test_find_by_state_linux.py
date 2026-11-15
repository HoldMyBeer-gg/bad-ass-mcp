"""Finding an element by state on Linux without collecting every match.

The Linux half of test_find_by_state.py. A state lookup used to gather
every role and name match in the window, turn each into a handle, and
only then look at states. The state is now checked during the search, so
a lookup that needs one element stops at the first one that carries it.
"""

from __future__ import annotations

import pytest

from tests.test_linux_internals import _make_backend
from tests.test_search_big_trees_linux import Node


class Stateful(Node):
    """A fake accessible with a state set, counting how often it is read."""

    reads = 0

    def __init__(self, name, role, states=(), children=()):
        super().__init__(name, role, children)
        self.states = set(states)

    def get_state_set(self):
        Stateful.reads += 1
        return self

    def contains(self, state_type):
        return state_type in self.states


@pytest.fixture
def linux():
    Node.fetched = 0
    Stateful.reads = 0
    return _make_backend()


def _window(backend, mod, rows=5000, ticked=("box3",)):
    checked = mod.Atspi.StateType.CHECKED
    boxes = [
        Stateful(f"box{i}", "check box", [checked] if f"box{i}" in ticked else [])
        for i in range(rows)
    ]
    root = Node("w", "frame", [Node("list", "list", boxes)])
    backend._find_app_waking = lambda window_id: root
    return root


def test_a_state_is_checked_during_the_search_so_it_stops_at_the_first_match(linux):
    backend, mod = linux
    _window(backend, mod)
    found = backend.find_with_state("1", role="check box", state="checked", limit=1)
    assert [e.name for e in found] == ["box3"]
    assert "checked" in found[0].states
    assert Node.fetched < 120


def test_only_role_and_name_matches_have_their_state_read(linux):
    backend, mod = linux
    focused = mod.Atspi.StateType.FOCUSED
    root = Node(
        "w",
        "frame",
        [Stateful(f"b{i}", "button", [focused]) for i in range(30)]
        + [Stateful("box", "entry", [focused])],
    )
    backend._find_app_waking = lambda window_id: root
    found = backend.find_with_state("1", role="entry", state="focused")
    assert [e.name for e in found] == ["box"]
    # Once by the search, once more when the match becomes a handle.
    assert Stateful.reads == 2


def test_without_a_limit_every_element_in_the_state_is_found(linux):
    backend, mod = linux
    _window(backend, mod, rows=200, ticked=("box7", "box170"))
    found = backend.find_with_state("1", role="check box", state="checked")
    assert [e.name for e in found] == ["box7", "box170"]


def test_a_state_nothing_carries_finds_nothing(linux):
    backend, mod = linux
    _window(backend, mod, rows=20)
    assert backend.find_with_state("1", role="check box", state="focused") == []


def test_a_state_the_backend_does_not_report_finds_nothing(linux):
    backend, mod = linux
    _window(backend, mod, rows=20)
    assert backend.find_with_state("1", role="check box", state="sparkly") == []


def test_a_missing_window_finds_nothing(linux):
    backend, _ = linux
    backend._find_app_waking = lambda window_id: None
    assert backend.find_with_state("1", state="focused") == []


def test_accept_rejects_without_ending_the_search(linux):
    backend, _ = linux
    root = Node("w", "frame", [Node(f"Record{i}", "list item") for i in range(20)])
    found = backend._search(root, "list item", None, accept=lambda n: n.name.endswith("7"))
    assert [n.name for n in found] == ["Record7", "Record17"]
