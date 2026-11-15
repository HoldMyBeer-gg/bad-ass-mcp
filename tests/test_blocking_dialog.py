"""wait_for_element names the dialog that blocks its window.

The goblin showcase, November 14th: wait_for_element sat on the Creation
Kit's main window for minutes while a modal "Warning" box covered it, then
timed out with "element not found" and no word about the box. A modal
disables its owner, so a wait whose window stays disabled behind an owned
dialog now stops early and says which dialog to deal with.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time
from unittest.mock import patch

import pytest

from bad_ass_mcp import server
from bad_ass_mcp.backend.base import DesktopBackend
from bad_ass_mcp.types import BlockedByDialog, WindowInfo
from tests.fake_backend import FAKE_WINDOW_ID, FakeBackend

_WARNING = WindowInfo(id="77", name="Warning", pid=1, focused=True)


class _Blocked(FakeBackend):
    """The fake window behind a modal for `blocked_for` seconds."""

    POLL_S = 0.01

    def __init__(self, blocked_for: float = 1e9):
        super().__init__()
        self._clears = time.monotonic() + blocked_for

    def blocking_dialog(self, window_id):
        return _WARNING if time.monotonic() < self._clears else None


def test_backends_report_no_dialog_by_default():
    assert FakeBackend().blocking_dialog(FAKE_WINDOW_ID) is None


def test_wait_stops_early_and_names_the_dialog():
    b = _Blocked()
    t0 = time.monotonic()
    with (
        patch.object(DesktopBackend, "BLOCKED_GRACE_S", 0.2),
        pytest.raises(BlockedByDialog) as raised,
    ):
        b.wait_for_element(FAKE_WINDOW_ID, name="Nope", timeout=30)
    assert time.monotonic() - t0 < 2
    assert raised.value.dialog is _WARNING
    assert "Warning" in str(raised.value)


def test_a_dialog_that_closes_within_the_grace_does_not_end_the_wait():
    """A progress box that comes and goes must not fail a wait that would
    have found its element right after."""
    b = _Blocked(blocked_for=0.1)
    with patch.object(DesktopBackend, "BLOCKED_GRACE_S", 0.3):
        assert b.wait_for_element(FAKE_WINDOW_ID, name="Nope", timeout=0.6) is None


def test_an_element_found_behind_a_dialog_is_still_returned():
    b = _Blocked()
    with patch.object(DesktopBackend, "BLOCKED_GRACE_S", 0.0):
        el = b.wait_for_element(FAKE_WINDOW_ID, name="OK", timeout=1)
    assert el.id == "btn-ok"


def test_server_wait_reports_the_blocking_dialog():
    b = _Blocked()
    with (
        patch.object(server, "_backend", lambda: b),
        patch.object(DesktopBackend, "BLOCKED_GRACE_S", 0.05),
    ):
        out = asyncio.run(server.wait_for_element(FAKE_WINDOW_ID, name="Nope", timeout=30))
    assert "Warning" in out["error"]
    assert out["dialog"]["id"] == "77"


# ── the real thing on Windows: a window and a modal box of our own ──────


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 modal dialogs")
def test_windows_finds_the_modal_box_that_disabled_its_owner():
    from bad_ass_mcp.backend.windows import WindowsBackend

    user32 = ctypes.windll.user32
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.FindWindowW.restype = ctypes.c_void_p
    owner: list[int] = []
    title = f"Warning bam-test {time.monotonic_ns()}"

    def ui():
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "bam owner", 0x10CF0000, 50, 50, 300, 200, None, None, None, None
        )
        owner.append(hwnd)
        user32.MessageBoxW(ctypes.c_void_p(hwnd), "Blocking", title, 0)
        user32.DestroyWindow(ctypes.c_void_p(hwnd))

    t = threading.Thread(target=ui, daemon=True)
    t.start()
    box = None
    for _ in range(50):
        box = user32.FindWindowW(None, title)
        if box:
            break
        time.sleep(0.05)
    assert box, "the message box never appeared"
    try:
        backend = WindowsBackend()
        found = backend.blocking_dialog(str(owner[0]))
        assert found is not None
        assert found.id == str(box)
        assert found.name == title
    finally:
        user32.PostMessageW(ctypes.c_void_p(box), 0x0010, 0, 0)  # WM_CLOSE
        t.join(timeout=5)
    assert backend.blocking_dialog(str(owner[0])) is None
