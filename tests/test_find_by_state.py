"""Finding an element by state without walking the whole window.

The goblin showcase, November 15th: the Creation Kit's Select Form dialog
lists every form in the game. A BAM phase learned its filter box as "the
focused textfield", and a state lookup collected every textfield in the
window before checking which one had focus, so the run hit the search
deadline. A "focused" lookup now asks the platform for the focused
element, and other states are checked during the search so it can stop at
the first match.
"""

from __future__ import annotations

from tests.fake_backend import FAKE_WINDOW_ID, FakeBackend
from tests.test_search_big_trees import (
    AXCalls,
    AXNode,
    Node,
    Walker,
    _backend,
    _ck_window,
    _fake_ax,
    _finder_window,
    _mac,
)

EDIT = 50004
CHECKBOX = 50002


class Uia:
    def __init__(self, focused):
        self.focused = focused
        self.RawViewWalker = self

    def GetFocusedElement(self):
        return self.focused

    def GetParentElement(self, el):
        return el.parent

    def CompareElements(self, a, b):
        return a is b


def _dialog(rows=5000):
    root = _ck_window(rows)
    box = Node("", EDIT)
    box.parent = root
    root.kids.append(box)
    return root, box


def test_the_focused_element_comes_from_the_platform_not_a_walk():
    root, box = _dialog()
    b = _backend()
    b._uia = Uia(box)
    assert b._focused_within(root, "textfield", None) is box
    assert Walker.steps == 0


def test_focus_in_another_window_does_not_count():
    root, _ = _dialog(rows=10)
    elsewhere = Node("", EDIT)
    b = _backend()
    b._uia = Uia(elsewhere)
    assert b._focused_within(root, "textfield", None) is None


def test_the_focused_element_must_match_role_and_name():
    root, box = _dialog(rows=10)
    b = _backend()
    b._uia = Uia(box)
    assert b._focused_within(root, "button", None) is None
    assert b._focused_within(root, "textfield", "Filter") is None


def test_no_focus_at_all_is_no_match():
    root, _ = _dialog(rows=10)
    b = _backend()
    b._uia = Uia(None)
    assert b._focused_within(root, None, None) is None


def test_a_state_is_checked_during_the_search_so_it_stops_at_the_first_match():
    rows = [Node(f"box{i}", CHECKBOX) for i in range(5000)]
    root = Node("w", 50033, [Node("list", 50008, rows)])
    ticked = {"box3"}
    found = _backend()._search(
        root, "checkbox", None, limit=1, accept=lambda el: el.CurrentName in ticked
    )
    assert [n.CurrentName for n in found] == ["box3"]
    assert Walker.steps < 120


def test_accept_rejects_without_ending_the_search():
    root = _ck_window(rows=20)
    found = _backend()._search(
        root, "listitem", None, accept=lambda el: el.CurrentName.endswith("7")
    )
    assert [n.CurrentName for n in found] == ["Record7", "Record17"]


class Spy(FakeBackend):
    def __init__(self):
        super().__init__()
        self.calls = []

    def find_with_state(self, window_id, **kw):
        self.calls.append(kw)
        return super().find_with_state(window_id, **kw)


def test_the_default_keeps_role_and_name_matches_that_carry_the_state():
    b = FakeBackend()
    found = b.find_with_state(FAKE_WINDOW_ID, role="text", state="editable")
    assert found and all("editable" in e.states for e in found)
    assert b.find_with_state(FAKE_WINDOW_ID, role="text", state="focused") == []


def test_the_default_honours_the_limit():
    b = FakeBackend()
    assert len(b.find_with_state(FAKE_WINDOW_ID, state="enabled", limit=1)) <= 1


def test_waiting_for_a_state_asks_for_one_element_with_that_state():
    b = Spy()
    b.wait_for_element(FAKE_WINDOW_ID, role="text", state="editable", timeout=0)
    assert b.calls == [{"role": "text", "name": None, "state": "editable", "limit": 1}]


def test_a_bam_state_lookup_asks_for_one_element_with_that_state(tmp_path):
    from bad_ass_mcp.bam.runner import run_script
    from tests.test_bam import CONFIG, write

    script = write(
        tmp_path,
        "state",
        {"phases": [{"learn": {"name": {"role": "text", "state": "editable"}}, "steps": []}]},
    )
    b = Spy()
    assert run_script(b, script, "linux", CONFIG, tmp_path / "out")["status"] == "PASS"
    assert b.calls[0] == {"role": "text", "name": None, "state": "editable", "limit": 1}


# ── macOS: the focused element from the app, other states in the search ──


def _mac_dialog(rows=5000, focused=True):
    root = _finder_window(rows)
    box = AXNode("AXTextField", "")
    root.kids[0].kids.append(box)
    root.attrs["AXFocusedUIElement"] = box if focused else None
    return root, box


def test_macos_focused_element_comes_from_the_app_not_a_walk():
    root, box = _mac_dialog()
    with _fake_ax():
        assert _mac()._focused_within(root, "textfield", None) is box
    assert AXCalls.reads == 1  # the focused element's own attributes


def test_macos_focused_element_must_match_role_and_name():
    root, box = _mac_dialog(rows=10)
    with _fake_ax():
        assert _mac()._focused_within(root, "button", None) is None
        assert _mac()._focused_within(root, "textfield", "Filter") is None
        assert _mac()._focused_within(root, None, None) is box


def test_macos_no_focus_at_all_is_no_match():
    root, _ = _mac_dialog(rows=10, focused=False)
    with _fake_ax():
        assert _mac()._focused_within(root, None, None) is None


def test_macos_a_state_is_checked_during_the_search_so_it_stops_at_the_first_match():
    rows = [AXNode("AXCheckBox", f"box{i}", AXValue=int(i == 3)) for i in range(5000)]
    root = AXNode("AXApplication", "app", [AXNode("AXWindow", "w", [AXNode("AXList", "l", rows)])])
    with _fake_ax(strict=False):
        found = _mac()._search(
            root, "checkbox", None, limit=1, accept=lambda el: "checked" in _mac()._states_of(el)
        )
    assert [n.name for n in found] == ["box3"]
    assert AXCalls.reads < 60


def test_macos_accept_rejects_without_ending_the_search():
    with _fake_ax():
        found = _mac()._search(
            _finder_window(rows=20), "row", None, accept=lambda el: el.name.endswith("7")
        )
    assert [n.name for n in found] == ["Record7", "Record17"]


def test_macos_find_with_state_uses_the_focused_element_or_the_search():
    root, box = _mac_dialog(rows=10)
    b = _mac()
    b._find_app_element = lambda window_id: root
    b._pid_for_window = lambda window_id: 1
    b._to_handle = lambda el, pid: el
    searched = {}

    def fake_search(el, role, name, limit=None, deadline=None, accept=None):
        searched.update(limit=limit, deadline=deadline, accept=accept)
        return []

    with _fake_ax():
        assert b.find_with_state("1", role="textfield", state="focused") == [box]
        b._search = fake_search
        assert b.find_with_state("1", role="checkbox", state="checked", limit=1) == []
    assert searched["limit"] == 1 and searched["deadline"] is not None
    assert callable(searched["accept"])
