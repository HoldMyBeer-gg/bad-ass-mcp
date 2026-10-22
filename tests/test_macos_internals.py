"""
Regression tests for macOS backend internal helpers.

_ax_value_to_rect, error constants, and the import guard run on all
platforms (no PyObjC required).  _resolve and _window_geometry tests
are marked darwin-only and use unittest.mock to avoid live AX calls.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from bad_ass_mcp.backend.macos import (
    _ax_value_to_rect,
    _kAXErrorInvalidUIElement,
    _kAXErrorSuccess,
)
from bad_ass_mcp.types import StaleHandleError


class _FakeAXRect:
    """Mimics PyObjC AXValueRef.__str__ for a CGRect value."""

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self._s = f"x:{x} y:{y} w:{w} h:{h}"

    def __str__(self) -> str:
        return f"<AXValue> {{value = {self._s} type = kAXValueCGRectType}}"


# ── _ax_value_to_rect ─────────────────────────────────────────────────


def test_ax_value_to_rect_parses_cgrect():
    assert _ax_value_to_rect(_FakeAXRect(0.0, 34.0, 1710.0, 978.0)) == (
        0.0,
        34.0,
        1710.0,
        978.0,
    )


def test_ax_value_to_rect_fractional_origin():
    assert _ax_value_to_rect(_FakeAXRect(10.5, 20.7, 800.0, 600.0)) == (
        10.5,
        20.7,
        800.0,
        600.0,
    )


def test_ax_value_to_rect_negative_origin():
    # Windows on secondary monitors can have negative screen coordinates
    result = _ax_value_to_rect(_FakeAXRect(-10.0, -5.0, 1280.0, 800.0))
    assert result == (-10.0, -5.0, 1280.0, 800.0)


def test_ax_value_to_rect_returns_none_for_garbage():
    class _Bad:
        def __str__(self) -> str:
            return "not an ax value"

    assert _ax_value_to_rect(_Bad()) is None


def test_ax_value_to_rect_returns_none_for_cgpoint_only():
    # CGPoint string has no w:/h: fields — must not return a partial match
    class _Point:
        def __str__(self) -> str:
            return "<AXValue> {value = x:5.0 y:10.0 type = kAXValueCGPointType}"

    assert _ax_value_to_rect(_Point()) is None


# ── _ax_name label fallback ───────────────────────────────────────────


def _fake_ax_get(attrs: dict):
    """Return an _ax_get stand-in backed by a dict of attr -> value.

    Elements are represented as dicts; a linked AXTitleUIElement is itself
    such a dict. Missing attrs return None, matching real _ax_get.
    """

    def getter(element, attr):
        if not isinstance(element, dict):
            return None
        return element.get(attr)

    return getter


def _name_of(attrs: dict) -> str:
    from unittest.mock import patch

    from bad_ass_mcp.backend.macos import _ax_name

    with patch("bad_ass_mcp.backend.macos._ax_get", side_effect=_fake_ax_get(attrs)):
        return _ax_name(attrs)


def test_ax_name_prefers_title():
    assert _name_of({"AXTitle": "Save", "AXDescription": "d", "AXHelp": "h"}) == "Save"


def test_ax_name_falls_back_to_description():
    assert _name_of({"AXRole": "AXButton", "AXDescription": "Close window"}) == "Close window"


def test_ax_name_uses_linked_title_element():
    # A field labelled by a separate static-text element beside it.
    label = {"AXTitle": "Email address"}
    field = {"AXRole": "AXTextField", "AXTitleUIElement": label}
    assert _name_of(field) == "Email address"


def test_ax_name_linked_title_element_value_fallback():
    label = {"AXValue": "Password"}  # label carries its text in AXValue
    field = {"AXRole": "AXTextField", "AXTitleUIElement": label}
    assert _name_of(field) == "Password"


def test_ax_name_falls_back_to_help():
    # Icon button with only a tooltip.
    el = {"AXRole": "AXButton", "AXHelp": "Zoom the window"}
    assert _name_of(el) == "Zoom the window"


def test_ax_name_uses_specific_role_description():
    # AXRoleDescription that adds information beyond the bare role.
    el = {"AXRole": "AXButton", "AXRoleDescription": "close button"}
    assert _name_of(el) == "close button"


def test_ax_name_skips_role_description_that_echoes_role():
    # "button" for a button adds nothing over the role field — drop it.
    el = {"AXRole": "AXButton", "AXRoleDescription": "button"}
    assert _name_of(el) == ""


def test_ax_name_empty_when_unlabelled_everywhere():
    el = {"AXRole": "AXButton"}
    assert _name_of(el) == ""


# ── error constant values ─────────────────────────────────────────────


def test_ax_error_constants_match_apple_header():
    # Regression: wrong constant (-25212 vs -25202) caused valid elements
    # to be marked stale when the AX API returned a transient error.
    assert _kAXErrorSuccess == 0
    assert _kAXErrorInvalidUIElement == -25202


# ── MacOSBackend.__init__ import guard ────────────────────────────────


def test_init_raises_without_pyobjc():
    from bad_ass_mcp.backend.macos import MacOSBackend

    with patch("bad_ass_mcp.backend.macos._HAS_PYOBJC", False):
        with pytest.raises(RuntimeError, match="[Pp]y[Oo]bj[Cc]"):
            MacOSBackend()


# ── _resolve staleness ────────────────────────────────────────────────

_darwin = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS + PyObjC")


@_darwin
def test_resolve_raises_stale_on_invalid_element():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    backend._handles["h1"] = MagicMock()

    with patch(
        "bad_ass_mcp.backend.macos.AXUIElementCopyAttributeValue",
        return_value=(-25202, None),  # kAXErrorInvalidUIElement
    ):
        with pytest.raises(StaleHandleError):
            backend._resolve("h1")

    assert "h1" not in backend._handles  # handle must be evicted


@_darwin
def test_resolve_does_not_evict_on_attribute_unsupported():
    # kAXErrorAttributeUnsupported (-25205): element is alive, just quirky
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fake_el = MagicMock()
    backend._handles["h2"] = fake_el

    with patch(
        "bad_ass_mcp.backend.macos.AXUIElementCopyAttributeValue",
        return_value=(-25205, None),
    ):
        result = backend._resolve("h2")

    assert result is fake_el
    assert "h2" in backend._handles


@_darwin
def test_resolve_does_not_evict_on_cannot_complete():
    # kAXErrorCannotComplete (-25204): transient, common during animations
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fake_el = MagicMock()
    backend._handles["h3"] = fake_el

    with patch(
        "bad_ass_mcp.backend.macos.AXUIElementCopyAttributeValue",
        return_value=(-25204, None),
    ):
        result = backend._resolve("h3")

    assert result is fake_el
    assert "h3" in backend._handles


@_darwin
def test_resolve_unknown_handle_raises():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    with pytest.raises(StaleHandleError, match="Unknown handle"):
        backend._resolve("no-such-handle")


# ── _window_geometry ──────────────────────────────────────────────────


@_darwin
def test_window_geometry_returns_int_tuple():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fake_frame = _FakeAXRect(10.5, 20.7, 800.0, 600.0)

    def fake_ax_get(el, attr):
        if attr == "AXWindows":
            return [MagicMock()]
        if attr == "AXFrame":
            return fake_frame
        return None

    with patch.object(backend, "_find_app_element", return_value=MagicMock()):
        with patch("bad_ass_mcp.backend.macos._ax_get", side_effect=fake_ax_get):
            result = backend._window_geometry("999")

    assert result == (10, 20, 800, 600)


@_darwin
def test_window_geometry_returns_none_for_missing_app():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    with patch.object(backend, "_find_app_element", return_value=None):
        assert backend._window_geometry("999") is None


@_darwin
def test_window_geometry_returns_none_when_no_windows():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    with patch.object(backend, "_find_app_element", return_value=MagicMock()):
        with patch("bad_ass_mcp.backend.macos._ax_get", return_value=None):
            assert backend._window_geometry("999") is None


# ── _find_app_element CG-fallback for lagged NSWorkspace ──────────────


@_darwin
def test_find_app_element_falls_back_to_cg_when_nsworkspace_lags():
    """NSWorkspace.runningApplications() can lag for several seconds after a
    bundled .app launches. _find_app_element used to return None in that
    window, which broke get_tree/find_elements with 'No window found' even
    though AXUIElementCreateApplication(pid) would have worked. This test
    pins the fallback so the regression doesn't return."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fresh_pid = 99999  # PID NSWorkspace doesn't yet know about
    fake_ax_app = MagicMock(name="ax_app")

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace") as ns,
        patch(
            "bad_ass_mcp.backend.macos._cg_onscreen_windows",
            return_value=[{"kCGWindowOwnerPID": fresh_pid}],
        ),
        patch(
            "bad_ass_mcp.backend.macos.AXUIElementCreateApplication",
            return_value=fake_ax_app,
        ),
        patch(
            "bad_ass_mcp.backend.macos._ax_get",
            return_value=[MagicMock()],  # AXWindows non-empty → real AX
        ),
    ):
        ns.sharedWorkspace.return_value.runningApplications.return_value = []
        result = backend._find_app_element(str(fresh_pid))

    assert result is fake_ax_app


@_darwin
def test_find_app_element_returns_none_when_pid_has_no_ax_windows():
    """If AXUIElementCreateApplication(pid) succeeds but the app has no
    AXWindows, the element is for a dead/AX-less PID — return None instead
    of handing back a useless element."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fresh_pid = 88888

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace") as ns,
        patch(
            "bad_ass_mcp.backend.macos._cg_onscreen_windows",
            return_value=[{"kCGWindowOwnerPID": fresh_pid}],
        ),
        patch("bad_ass_mcp.backend.macos.AXUIElementCreateApplication", return_value=MagicMock()),
        patch("bad_ass_mcp.backend.macos._ax_get", return_value=None),  # AXWindows empty
    ):
        ns.sharedWorkspace.return_value.runningApplications.return_value = []
        result = backend._find_app_element(str(fresh_pid))

    assert result is None


# ── stop_recording path validation ────────────────────────────────────


@_darwin
def test_stop_recording_rejects_unknown_extension():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    backend._recordings["h"] = (MagicMock(), "/tmp/bam-rec-h.mp4")
    with pytest.raises(ValueError, match=r"\.gif or \.mp4"):
        backend.stop_recording("h", "/tmp/output.webm")


@_darwin
def test_stop_recording_rejects_symlink_resolving_to_non_gif():
    import os
    import tempfile

    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    backend._recordings["h"] = (MagicMock(), "/tmp/bam-rec-h.mp4")

    # Create a .gif symlink pointing at a .txt file (simulates path-traversal via symlink)
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        real_path = f.name
    link_path = real_path[:-4] + ".gif"
    try:
        os.symlink(real_path, link_path)
        with pytest.raises(ValueError, match=r"\.gif or \.mp4"):
            backend.stop_recording("h", link_path)
    finally:
        try:
            os.unlink(link_path)
        except FileNotFoundError:
            pass
        os.unlink(real_path)


# ── _pid_for_window CGWindowList fallback ─────────────────────────────


@_darwin
def test_pid_for_window_uses_nsworkspace_when_present():
    # Sanity: existing happy path still works when NSWorkspace knows the app.
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    fake_app = MagicMock()
    fake_app.processIdentifier.return_value = 1234
    fake_app.localizedName.return_value = "Foo"
    fake_ws = MagicMock()
    fake_ws.sharedWorkspace.return_value.runningApplications.return_value = [fake_app]

    with patch("bad_ass_mcp.backend.macos.NSWorkspace", fake_ws):
        assert backend._pid_for_window("1234") == 1234


@_darwin
def test_pid_for_window_falls_back_to_cg_when_nsworkspace_misses():
    # The bug this guards against: in a long-running daemon, NSWorkspace's
    # runningApplications() snapshot can lag for apps that started after server
    # init (Tauri/Electron especially). _pid_for_window must still resolve the
    # PID via CGWindowList — same backfill list_windows() already trusts.
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    empty_ws = MagicMock()
    empty_ws.sharedWorkspace.return_value.runningApplications.return_value = []
    cg_windows = [{"kCGWindowOwnerPID": 27886, "kCGWindowNumber": 11600}]

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace", empty_ws),
        patch("bad_ass_mcp.backend.macos._cg_onscreen_windows", return_value=cg_windows),
    ):
        assert backend._pid_for_window("27886") == 27886


@_darwin
def test_pid_for_window_returns_none_for_non_numeric_unknown_name():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    empty_ws = MagicMock()
    empty_ws.sharedWorkspace.return_value.runningApplications.return_value = []

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace", empty_ws),
        patch("bad_ass_mcp.backend.macos._cg_onscreen_windows", return_value=[]),
    ):
        assert backend._pid_for_window("not-a-pid") is None


@_darwin
def test_pid_for_window_returns_none_for_numeric_with_no_window():
    # Numeric window_id but no CG window owned by it → still None, not silently
    # accepting a bogus PID.
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    empty_ws = MagicMock()
    empty_ws.sharedWorkspace.return_value.runningApplications.return_value = []

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace", empty_ws),
        patch("bad_ass_mcp.backend.macos._cg_onscreen_windows", return_value=[]),
    ):
        assert backend._pid_for_window("999999") is None


@_darwin
def test_stop_recording_accepts_valid_gif_path():
    import os
    import tempfile

    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    # Provide a real (empty) mp4 source so ffmpeg fails fast but validation passes
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as src:
        src_path = src.name
    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as dst:
        dst_path = dst.name

    proc_mock = MagicMock()
    proc_mock.stdin = MagicMock()
    proc_mock.wait = MagicMock(return_value=0)
    backend._recordings["h"] = (proc_mock, src_path)

    # ffmpeg will fail on an empty mp4, but we just want validation to pass
    try:
        backend.stop_recording("h", dst_path)
    except RuntimeError:
        pass  # expected — ffmpeg rejects empty file
    finally:
        for p in (src_path, dst_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ── webview AX wake-probe ─────────────────────────────────────────────


@_darwin
def test_wake_ax_windows_pokes_manual_accessibility_first():
    """AXManualAccessibility succeeding must short-circuit — the
    AXEnhancedUserInterface fallback has window-animation side effects."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fake_win = MagicMock()
    set_calls = []

    def fake_ax_set(el, attr, value):
        set_calls.append(attr)
        return attr == "AXManualAccessibility"

    with (
        patch("bad_ass_mcp.backend.macos.AXUIElementCreateApplication"),
        patch("bad_ass_mcp.backend.macos._ax_set", side_effect=fake_ax_set),
        patch("bad_ass_mcp.backend.macos._ax_get", return_value=[fake_win]),
        patch("time.sleep"),
    ):
        result = backend._wake_ax_windows(4242)

    assert result == [fake_win]
    assert set_calls == ["AXManualAccessibility"]


@_darwin
def test_wake_ax_windows_noop_when_attrs_unsupported():
    """Apps without a Chromium-style wake attribute (games, GL canvases)
    must return immediately — no poll loop, no sleep."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    with (
        patch("bad_ass_mcp.backend.macos.AXUIElementCreateApplication"),
        patch("bad_ass_mcp.backend.macos._ax_set", return_value=False),
        patch("bad_ass_mcp.backend.macos._ax_get") as ax_get,
        patch("time.sleep") as slept,
    ):
        result = backend._wake_ax_windows(4243)

    assert result == []
    ax_get.assert_not_called()
    slept.assert_not_called()


@_darwin
def test_wake_ax_windows_once_per_pid():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()

    with (
        patch("bad_ass_mcp.backend.macos.AXUIElementCreateApplication"),
        patch("bad_ass_mcp.backend.macos._ax_set", return_value=True) as ax_set,
        patch("bad_ass_mcp.backend.macos._ax_get", return_value=[MagicMock()]),
        patch("time.sleep"),
    ):
        first = backend._wake_ax_windows(4244)
        second = backend._wake_ax_windows(4244)

    assert first
    assert second == []
    ax_set.assert_called_once()


# ── _walk depth: Chromium content lives deep ──────────────────────────


@_darwin
def test_walk_descends_past_chromium_group_nesting():
    """Chromium (Vivaldi et al.) buries its web area ~13+ levels below the
    app root, deeper with GPU-composited pages. A shallow max_depth returned
    a hollow husk of empty AXGroups — get_tree looked broken while
    find_elements (uncapped) saw everything. Pin that the walk reaches a
    node deeper than the old 12-level cap so that regression can't return.
    """
    from bad_ass_mcp.backend.macos import _WALK_MAX_DEPTH, MacOSBackend

    backend = MacOSBackend()

    DEEP = 20  # within real Chromium range (Vivaldi content ran to depth ~22)
    assert DEEP > 12, "test must probe past the old cap"
    assert DEEP < _WALK_MAX_DEPTH, "cap must clear real Chromium nesting"

    # Build a synthetic chain DEEP wrapper groups long with a named leaf at
    # the bottom. Drive _walk via _ax_descendants (one child per level) and a
    # _to_handle that stamps the node's level as its name.
    from bad_ass_mcp.types import ElementHandle

    def fake_descendants(element, depth):
        return [depth + 1] if depth < DEEP else []

    def fake_to_handle(element, pid=None):
        level = 0 if not isinstance(element, int) else element
        name = "LEAF" if level == DEEP else ""
        return ElementHandle(id=str(level), role="group", name=name, value=None, states=[])

    with (
        patch.object(backend, "_ax_descendants", side_effect=fake_descendants),
        patch.object(backend, "_to_handle", side_effect=fake_to_handle),
    ):
        tree = backend._walk("root")

    # Descend the single chain to the bottom and confirm the deep leaf survived.
    node, levels = tree, 0
    while node.children:
        node = node.children[0]
        levels += 1
    assert levels == DEEP
    assert node.name == "LEAF"


@_darwin
def test_walk_respects_node_budget():
    """The node budget must bound a wide/pathological tree regardless of
    depth — a runaway or cyclic tree can't exhaust memory."""
    from bad_ass_mcp.backend.macos import MacOSBackend
    from bad_ass_mcp.types import ElementHandle

    backend = MacOSBackend()

    # Every node reports 10 children forever → unbounded without the budget.
    def fake_descendants(element, depth):
        return [object() for _ in range(10)]

    def fake_to_handle(element, pid=None):
        return ElementHandle(id="x", role="group", name="", value=None, states=[])

    def count(h):
        return 1 + sum(count(c) for c in h.children)

    with (
        patch("bad_ass_mcp.backend.macos._WALK_MAX_NODES", 500),
        patch.object(backend, "_ax_descendants", side_effect=fake_descendants),
        patch.object(backend, "_to_handle", side_effect=fake_to_handle),
    ):
        tree = backend._walk("root", budget=[500])

    assert count(tree) <= 500


@_darwin
def test_find_app_element_wakes_lazy_webview():
    """PID fallback: AXWindows empty on first probe, but a successful wake
    means the element is real — return it instead of None."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    fresh_pid = 77777
    fake_ax_app = MagicMock(name="ax_app")

    with (
        patch("bad_ass_mcp.backend.macos.NSWorkspace") as ns,
        patch(
            "bad_ass_mcp.backend.macos._cg_onscreen_windows",
            return_value=[{"kCGWindowOwnerPID": fresh_pid}],
        ),
        patch(
            "bad_ass_mcp.backend.macos.AXUIElementCreateApplication",
            return_value=fake_ax_app,
        ),
        patch("bad_ass_mcp.backend.macos._ax_get", return_value=None),  # AXWindows empty
        patch.object(backend, "_wake_ax_windows", return_value=[MagicMock()]),
    ):
        ns.sharedWorkspace.return_value.runningApplications.return_value = []
        result = backend._find_app_element(str(fresh_pid))

    assert result is fake_ax_app


# ── Quartz button mapping ────────────────────────────────────────────


@_darwin
def test_quartz_button_maps_names():
    from Quartz import (
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGMouseButtonCenter,
        kCGMouseButtonRight,
    )

    from bad_ass_mcp.backend.macos import _quartz_button

    down, up, _, btn = _quartz_button("right")
    assert (down, up, btn) == (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight)
    assert _quartz_button("middle")[3] == kCGMouseButtonCenter
    with pytest.raises(ValueError):
        _quartz_button("trackball")


@_darwin
def test_double_click_stamps_click_state():
    from bad_ass_mcp.backend import macos

    posted: list = []
    fields: list[tuple] = []
    with (
        patch.object(macos, "CGEventPost", side_effect=lambda tap, ev: posted.append(ev)),
        patch.object(macos, "CGEventCreateMouseEvent", side_effect=lambda *a: ("ev", a[1])),
        patch.object(
            macos,
            "CGEventSetIntegerValueField",
            side_effect=lambda ev, f, v: fields.append((ev, v)),
        ),
    ):
        macos._quartz_mouse_click(1, 2, clicks=2)
    assert len(posted) == 4
    # click state 1 on the first pair, 2 on the second
    assert [v for _, v in fields] == [1, 1, 2, 2]


# ── avfoundation screen device discovery ─────────────────────────────

_DEVICE_LISTING = b"""[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] MacBook Air Camera
[AVFoundation indev @ 0x1] [1] iPhone Camera
[AVFoundation indev @ 0x1] [2] MacBook Air Desk View Camera
[AVFoundation indev @ 0x1] [3] iPhone Desk View Camera
[AVFoundation indev @ 0x1] [4] Capture screen 0
[AVFoundation indev @ 0x1] [5] Capture screen 1
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Air Microphone
"""


@_darwin
def test_screen_device_skips_cameras():
    """With an iPhone nearby the screen is not device 1; take the first
    'Capture screen' entry whatever its index."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    MacOSBackend._screen_device = None
    fake = MagicMock(stderr=_DEVICE_LISTING, returncode=1)
    with patch("bad_ass_mcp.backend.macos.subprocess.run", return_value=fake) as run:
        assert MacOSBackend._avfoundation_screen_device() == "4"
        # cached: a second call doesn't re-run ffmpeg
        assert MacOSBackend._avfoundation_screen_device() == "4"
        assert run.call_count == 1
    MacOSBackend._screen_device = None


@_darwin
def test_screen_device_errors_when_none_listed():
    from bad_ass_mcp.backend.macos import MacOSBackend

    MacOSBackend._screen_device = None
    fake = MagicMock(stderr=b"[AVFoundation indev @ 0x1] [0] FaceTime HD Camera\n", returncode=1)
    with patch("bad_ass_mcp.backend.macos.subprocess.run", return_value=fake):
        with pytest.raises(RuntimeError, match="screen device"):
            MacOSBackend._avfoundation_screen_device()
    MacOSBackend._screen_device = None


@_darwin
def test_start_recording_scales_window_crop_for_retina():
    """AX geometry is in points, avfoundation captures pixels."""
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    MacOSBackend._screen_device = "4"
    seen: dict = {}

    def fake_spawn(cmd, video_path):
        seen["cmd"] = cmd
        return (MagicMock(), video_path, video_path + ".log")

    with (
        patch.object(backend, "_window_geometry", return_value=(10, 20, 301, 400)),
        patch.object(backend, "_main_display_scale", return_value=2.0),
        patch.object(backend, "_spawn_recorder", side_effect=fake_spawn),
    ):
        backend.start_recording("123")
    MacOSBackend._screen_device = None
    cmd = seen["cmd"]
    assert cmd[cmd.index("-i") + 1] == "4:none"
    # 301 pt → 602 px, 400 pt → 800 px, origin doubled
    assert cmd[cmd.index("-vf") + 1] == "crop=602:800:20:40"
