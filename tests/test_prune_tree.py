"""
Tests for the shared tree-pruning pass in backend.base.

prune_tree collapses empty, nameless layout wrappers (the AXGroup chains
Chromium generates) while preserving every named or interactive node. It is
platform-independent, so it's exercised directly against ElementHandle trees
rather than through a live backend.
"""

from __future__ import annotations

from bad_ass_mcp.backend.base import _is_noise_wrapper, prune_tree
from bad_ass_mcp.types import ElementHandle


def _h(role="group", name="", value=None, states=None, children=None):
    return ElementHandle(
        id="x",
        role=role,
        name=name,
        value=value,
        states=set(states or []),
        children=list(children or []),
    )


# ── _is_noise_wrapper ─────────────────────────────────────────────────


def test_empty_nameless_group_is_noise():
    assert _is_noise_wrapper(_h(role="group"))


def test_named_group_is_not_noise():
    assert not _is_noise_wrapper(_h(role="group", name="Tabs"))


def test_group_with_value_is_not_noise():
    assert not _is_noise_wrapper(_h(role="group", value="hello"))


def test_group_with_meaningful_state_is_not_noise():
    # focusable/selectable wrapper: a caller might target it, keep it.
    assert not _is_noise_wrapper(_h(role="group", states={"focused"}))


def test_button_is_never_noise():
    # Interactive role, even if unnamed (icon buttons), must survive.
    assert not _is_noise_wrapper(_h(role="button"))


def test_webarea_is_not_structural():
    assert not _is_noise_wrapper(_h(role="webarea"))


# ── prune_tree ────────────────────────────────────────────────────────


def test_collapses_chain_of_empty_groups():
    # window > group > group > group > button("Go")
    leaf = _h(role="button", name="Go")
    deep = _h(children=[_h(children=[_h(children=[leaf])])])
    root = _h(role="window", name="W", children=[deep])

    pruned = prune_tree(root)

    # The three empty groups collapse; the button grafts directly onto window.
    assert len(pruned.children) == 1
    assert pruned.children[0].name == "Go"
    assert pruned.children[0].role == "button"


def test_preserves_named_wrapper():
    toolbar = _h(role="toolbar", name="Address", children=[_h(role="button", name="Reload")])
    root = _h(role="window", name="W", children=[_h(children=[toolbar])])

    pruned = prune_tree(root)

    # The unnamed group collapses, but the named toolbar and its button stay.
    assert len(pruned.children) == 1
    tb = pruned.children[0]
    assert tb.name == "Address"
    assert [c.name for c in tb.children] == ["Reload"]


def test_root_is_never_dropped_even_if_empty():
    # A nameless group as root (unlikely, but must not vanish).
    root = _h(role="group", name="", children=[_h(role="button", name="OK")])
    pruned = prune_tree(root)
    assert pruned is root
    assert [c.name for c in pruned.children] == ["OK"]


def test_grafts_multiple_children_up():
    # An empty group holding several real nodes: all lift to the parent.
    wrapper = _h(children=[_h(role="button", name="A"), _h(role="button", name="B")])
    root = _h(role="window", name="W", children=[wrapper])

    pruned = prune_tree(root)

    assert [c.name for c in pruned.children] == ["A", "B"]


def test_no_named_content_collapses_to_bare_root():
    # The husk case inverted: a tree that is ALL empty groups prunes to just
    # the root with no children (nothing real was there to keep).
    root = _h(role="window", name="W", children=[_h(children=[_h(children=[_h()])])])
    pruned = prune_tree(root)
    assert pruned.children == []


def test_real_content_survives_deep_nesting():
    # Mirror the live Chromium shape: content buried under a long wrapper
    # chain still surfaces after pruning.
    leaf = _h(role="link", name="HoldMyBeer-gg/bad-ass-mcp")
    node = leaf
    for _ in range(20):
        node = _h(children=[node])
    root = _h(role="window", name="W", children=[node])

    pruned = prune_tree(root)

    # All 20 wrappers gone; the link is a direct child of the window.
    assert len(pruned.children) == 1
    assert pruned.children[0].name == "HoldMyBeer-gg/bad-ass-mcp"
