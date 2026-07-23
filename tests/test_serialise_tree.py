"""
Tests for get_tree's budget-aware serializer in server.py.

_serialise_tree caps the emitted JSON so a content-heavy page's tree always
fits the MCP client's per-tool-result limit. It drops nodes breadth-first
(deep leaves go first, shallow structure survives) and marks the result
truncated with a dropped-node count. Exercised directly against ElementHandle
trees; no live backend needed.
"""

from __future__ import annotations

import json

from bad_ass_mcp.server import _serialise_tree
from bad_ass_mcp.types import ElementHandle


def _h(role="group", name="", value=None, states=None, children=None):
    return ElementHandle(
        id="00000000-0000-0000-0000-000000000000",
        role=role,
        name=name,
        value=value,
        states=set(states or []),
        children=list(children or []),
    )


def _count(d: dict) -> int:
    return 1 + sum(_count(c) for c in d["children"])


# ── under budget: nothing is cut ──────────────────────────────────────


def test_small_tree_not_truncated():
    root = _h(role="window", name="W", children=[_h(role="button", name="A")])
    out = _serialise_tree(root, byte_budget=100_000)

    assert "truncated" not in out
    assert "dropped_nodes" not in out
    assert _count(out) == 2
    assert out["children"][0]["name"] == "A"


def test_fields_are_preserved():
    root = _h(role="button", name="Go", value="v", states={"enabled", "focused"})
    out = _serialise_tree(root, byte_budget=100_000)

    assert out["role"] == "button"
    assert out["name"] == "Go"
    assert out["value"] == "v"
    assert out["states"] == ["enabled", "focused"]  # sorted


# ── over budget: breadth-first cut + marker ───────────────────────────


def test_large_tree_is_truncated_and_marked():
    # 200 children under the root, each with a chunky name so we blow a small
    # budget well before all of them fit.
    kids = [_h(role="button", name=f"button-number-{i:04d}-with-padding") for i in range(200)]
    root = _h(role="window", name="W", children=kids)

    out = _serialise_tree(root, byte_budget=2_000)

    assert out["truncated"] is True
    assert out["dropped_nodes"] > 0
    # Some children made it, not all.
    assert 0 < len(out["children"]) < 200
    # Count is honest: emitted + dropped == original total (201 nodes).
    assert _count(out) + out["dropped_nodes"] == 201


def test_result_stays_within_budget():
    kids = [_h(role="link", name=f"link-{i:04d}-{'x' * 40}") for i in range(500)]
    root = _h(role="window", name="W", children=kids)

    out = _serialise_tree(root, byte_budget=5_000)
    size = len(json.dumps(out))

    # The marker fields add a little, but we stay in the same ballpark as the
    # budget — nowhere near the unbounded ~full-tree size.
    assert size < 5_000 * 1.2


def test_breadth_first_keeps_shallow_over_deep():
    # A shallow named button as a direct child, plus a deep chain that costs
    # more than the budget. The shallow node must survive; the deep chain is
    # what gets dropped.
    deep = _h(role="link", name="DEEP")
    for _ in range(50):
        deep = _h(role="group", name="w" * 50, children=[deep])
    shallow = _h(role="button", name="SHALLOW")
    root = _h(role="window", name="W", children=[shallow, deep])

    out = _serialise_tree(root, byte_budget=1_500)

    names_at_top = [c["name"] for c in out["children"]]
    assert "SHALLOW" in names_at_top
    assert out["truncated"] is True


# ── root is always emitted whole ──────────────────────────────────────


def test_root_emitted_even_when_alone_over_budget():
    # Absurdly tiny budget: the root itself exceeds it. Still return the root
    # (a non-empty result beats an empty one); all children are dropped.
    root = _h(role="window", name="W", children=[_h(role="button", name="A")])
    out = _serialise_tree(root, byte_budget=1)

    assert out["role"] == "window"
    assert out["name"] == "W"
    assert out["children"] == []
    assert out["truncated"] is True
    assert out["dropped_nodes"] == 1
