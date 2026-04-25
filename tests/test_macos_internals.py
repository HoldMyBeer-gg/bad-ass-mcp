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


# ── stop_recording path validation ────────────────────────────────────


@_darwin
def test_stop_recording_rejects_non_gif_extension():
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    backend._recordings["h"] = (MagicMock(), "/tmp/bam-rec-h.mp4")
    with pytest.raises(ValueError, match=r"\.gif"):
        backend.stop_recording("h", "/tmp/output.mp4")


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
        with pytest.raises(ValueError, match=r"\.gif"):
            backend.stop_recording("h", link_path)
    finally:
        try:
            os.unlink(link_path)
        except FileNotFoundError:
            pass
        os.unlink(real_path)


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
