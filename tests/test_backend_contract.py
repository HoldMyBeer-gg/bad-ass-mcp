"""
Contract tests: every DesktopBackend implementation must pass these.
Run against FakeBackend here; the Linux/Windows/macOS backends import
and run the same suite via pytest parametrize or inheritance.
"""

import pytest

from bad_ass_mcp.types import ActionResult, ElementHandle, StaleHandleError, WindowInfo

from .fake_backend import FAKE_WINDOW_ID, FakeBackend


@pytest.fixture
def backend():
    return FakeBackend()


# ── list_windows ─────────────────────────────────────────────────────


def test_list_windows_returns_window_info_list(backend):
    windows = backend.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1
    assert all(isinstance(w, WindowInfo) for w in windows)


def test_list_windows_has_required_fields(backend):
    w = backend.list_windows()[0]
    assert isinstance(w.id, str) and w.id
    assert isinstance(w.name, str)
    assert isinstance(w.pid, int)
    assert isinstance(w.focused, bool)


# ── get_tree ─────────────────────────────────────────────────────────


def test_get_tree_returns_element_handle(backend):
    tree = backend.get_tree(FAKE_WINDOW_ID)
    assert isinstance(tree, ElementHandle)


def test_get_tree_root_has_children(backend):
    tree = backend.get_tree(FAKE_WINDOW_ID)
    assert len(tree.children) > 0


# ── find_elements ─────────────────────────────────────────────────────


def test_find_by_role(backend):
    buttons = backend.find_elements(FAKE_WINDOW_ID, role="button")
    assert len(buttons) >= 1
    assert all(el.role == "button" for el in buttons)


def test_find_by_name(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, name="OK")
    assert len(results) >= 1
    assert results[0].name == "OK"


def test_find_by_role_and_name(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, role="button", name="Cancel")
    assert len(results) == 1
    assert results[0].role == "button"
    assert results[0].name == "Cancel"


def test_find_nonexistent_returns_empty(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, name="Definitely Not Here")
    assert results == []


# ── click ─────────────────────────────────────────────────────────────


def test_click_button_succeeds(backend):
    result = backend.click("btn-ok")
    assert isinstance(result, ActionResult)
    assert result.ok is True
    assert result.error is None


def test_click_stale_handle_raises(backend):
    backend.invalidate("btn-ok")
    with pytest.raises(StaleHandleError):
        backend.click("btn-ok")


# ── type_text ─────────────────────────────────────────────────────────


def test_type_text_into_editable_field(backend):
    result = backend.type_text("txt-name", "hello")
    assert result.ok is True
    assert backend.get_value("txt-name") == "hello"


def test_type_text_into_non_editable_fails(backend):
    result = backend.type_text("btn-ok", "oops")
    assert result.ok is False
    assert result.error is not None


def test_type_text_stale_handle_raises(backend):
    backend.invalidate("txt-name")
    with pytest.raises(StaleHandleError):
        backend.type_text("txt-name", "hello")


# ── select_option ─────────────────────────────────────────────────────


def test_select_valid_option(backend):
    result = backend.select_option("combo-size", "Large")
    assert result.ok is True
    assert backend.get_value("combo-size") == "Large"


def test_select_invalid_option_fails(backend):
    result = backend.select_option("combo-size", "Enormous")
    assert result.ok is False
    assert result.error is not None


# ── get_value ─────────────────────────────────────────────────────────


def test_get_value_returns_current_value(backend):
    assert backend.get_value("combo-size") == "Medium"


def test_get_value_reflects_type_text(backend):
    backend.type_text("txt-name", "world")
    assert backend.get_value("txt-name") == "world"


def test_get_value_stale_handle_raises(backend):
    backend.invalidate("txt-name")
    with pytest.raises(StaleHandleError):
        backend.get_value("txt-name")


# ── wait_for_window ───────────────────────────────────────────────────


def test_wait_for_window_appears(backend):
    backend.schedule_window(delay=0.02)
    result = backend.wait_for_window("Fake App", timeout=1.0)
    assert result is not None
    assert isinstance(result, WindowInfo)


def test_wait_for_window_times_out(backend):
    result = backend.wait_for_window("Ghost Window", timeout=0.05)
    assert result is None


# ── wait_for_element ──────────────────────────────────────────────────


def test_wait_for_element_finds_existing(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK")
    assert el is not None
    assert el.name == "OK"


def test_wait_for_element_with_state(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK", state="enabled")
    assert el is not None


def test_wait_for_element_missing_state_returns_none(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK", state="checked")
    assert el is None


# ── screenshot ────────────────────────────────────────────────────────


def test_screenshot_returns_bytes(backend):
    data = backend.screenshot()
    assert isinstance(data, bytes)
    assert len(data) > 0
