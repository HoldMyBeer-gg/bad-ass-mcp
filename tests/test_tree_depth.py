"""A depth cap must say where it cut, so 'empty' and 'not walked' differ.

On Windows a WebView2 page sits under ~5 nameless wrappers. get_tree with
max_depth=5 stopped inside them, pruning dropped the empty wrappers, and the
'Web content' pane looked childless, exactly like a tree that hadn't loaded
(rusty-win-crab, live MCP on f9307b2)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bad_ass_mcp.backend.base import prune_tree
from bad_ass_mcp.types import ElementHandle


def _wrapper(*children, truncated=False):
    return ElementHandle(id="w", role="pane", name="", children=list(children), truncated=truncated)


def test_pruning_a_cut_wrapper_marks_the_surviving_parent():
    pane = ElementHandle(
        id="p", role="pane", name="Web content", children=[_wrapper(_wrapper(truncated=True))]
    )
    root = ElementHandle(id="r", role="window", name="Hold My Beer", children=[pane])
    prune_tree(root)
    assert pane.children == []
    assert pane.truncated is True


def test_an_uncut_empty_wrapper_leaves_no_mark():
    pane = ElementHandle(id="p", role="pane", name="Web content", children=[_wrapper()])
    prune_tree(ElementHandle(id="r", role="window", name="w", children=[pane]))
    assert pane.truncated is False


def test_the_serialised_tree_flags_cut_nodes():
    from bad_ass_mcp.server import _serialise_tree

    pane = ElementHandle(id="p", role="pane", name="Web content", truncated=True)
    data = _serialise_tree(ElementHandle(id="r", role="window", name="w", children=[pane]))
    assert data["children"][0]["truncated_by_depth"] is True
    assert "truncated_by_depth" not in data


def test_the_webview_retry_does_not_wait_on_a_tree_cut_by_depth():
    from bad_ass_mcp.backend.windows import _until_populated

    cut = ElementHandle(id="r", role="pane", name="x", truncated=True)
    walks = []
    with patch("time.sleep") as slept:
        tree = _until_populated(lambda: walks.append(1) or cut, timeout=2.0)
    assert tree is cut and len(walks) == 1
    slept.assert_not_called()


def test_linux_walk_marks_a_node_whose_children_the_cap_skipped():
    from tests.test_linux_internals import _make_backend

    backend, _mod = _make_backend()
    leaf = MagicMock(get_child_count=MagicMock(return_value=0))
    node = MagicMock(get_child_count=MagicMock(return_value=1))
    node.get_child_at_index.return_value = leaf
    with patch.object(
        backend, "_to_handle", side_effect=lambda n: ElementHandle(id="h", role="pane", name="")
    ):
        cut = backend._walk(node, max_depth=0)
        full = backend._walk(node, max_depth=5)
    assert cut.truncated is True and cut.children == []
    assert full.truncated is False and full.children[0].truncated is False
