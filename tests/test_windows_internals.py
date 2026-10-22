"""
Regression tests for Windows backend internal helpers.

_bgra_to_png, VK code mapping, and control-type roles run on all
platforms (no Windows APIs required).  _resolve and stop_recording
tests are marked win32-only and use unittest.mock.
"""

from __future__ import annotations

import struct
import sys
import zlib
from unittest.mock import MagicMock, patch

import pytest

from bad_ass_mcp.backend.windows import (
    _CONTROL_TYPE_ROLES,
    _UIA_E_ELEMENTNOTAVAILABLE,
    _VK_CODES,
    _bgra_to_png,
)
from bad_ass_mcp.types import ActionResult, StaleHandleError

# ── _bgra_to_png ─────────────────────────────────────────────────────


def test_bgra_to_png_produces_valid_header():
    """Output must start with the 8-byte PNG signature."""
    bgra = bytes([0, 0, 255, 255] * 4)  # 2x2 red pixels (BGRA)
    png = _bgra_to_png(2, 2, bgra)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_bgra_to_png_contains_ihdr_idat_iend():
    bgra = bytes([0, 128, 0, 255] * 6)  # 3x2 green pixels
    png = _bgra_to_png(3, 2, bgra)
    assert b"IHDR" in png
    assert b"IDAT" in png
    assert b"IEND" in png


def test_bgra_to_png_1x1_pixel():
    """1x1 blue pixel (BGRA = 255,0,0,255) → RGB = 0,0,255."""
    bgra = bytes([255, 0, 0, 255])
    png = _bgra_to_png(1, 1, bgra)
    # Extract IDAT, decompress, check pixel data
    idat_start = png.index(b"IDAT") + 4
    # Find IDAT length: 4 bytes before the "IDAT" tag
    idat_len_offset = idat_start - 8
    import struct

    (idat_len,) = struct.unpack(">I", png[idat_len_offset : idat_len_offset + 4])
    compressed = png[idat_start : idat_start + idat_len]
    raw = zlib.decompress(compressed)
    # Row: filter-byte(0) + R(0) + G(0) + B(255)
    assert raw == bytes([0, 0, 0, 255])


def test_bgra_to_png_rejects_empty():
    """0-width or 0-height should produce minimal valid PNG or empty bytes."""
    # We don't require a specific error — just don't crash
    try:
        result = _bgra_to_png(0, 0, b"")
        assert isinstance(result, bytes)
    except (ValueError, struct.error):
        pass  # acceptable


# ── VK code coverage ─────────────────────────────────────────────────


def test_vk_codes_contain_essential_keys():
    """Named keys that the macOS/Linux backends support must also be mapped."""
    essential = {
        "Return",
        "Enter",
        "Escape",
        "Tab",
        "Space",
        "BackSpace",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Up",
        "Down",
        "Left",
        "Right",
    }
    assert essential.issubset(_VK_CODES.keys())


def test_vk_codes_values_are_valid():
    """All VK codes must be in the valid range 0x01–0xFF."""
    for key, vk in _VK_CODES.items():
        assert 0x01 <= vk <= 0xFF, f"{key} has invalid VK code {vk:#x}"


# ── control type role mapping ────────────────────────────────────────


def test_control_type_roles_contain_cross_platform_roles():
    """Roles shared with macOS/Linux backends must exist."""
    roles = set(_CONTROL_TYPE_ROLES.values())
    for expected in ("button", "checkbox", "combobox", "textfield", "menuitem", "list"):
        assert expected in roles, f"Missing cross-platform role: {expected}"


def test_control_type_ids_are_in_uia_range():
    """UIA ControlType IDs live in the 50000–50099 range."""
    for ct_id in _CONTROL_TYPE_ROLES:
        assert 50000 <= ct_id <= 50099, f"ControlType {ct_id} out of range"


# ── UIA_E_ELEMENTNOTAVAILABLE constant ───────────────────────────────


def test_element_not_available_hresult():
    # 0x80040201 as signed int32
    assert _UIA_E_ELEMENTNOTAVAILABLE == -2147220991


# ── WindowsBackend.__init__ import guard ─────────────────────────────


def test_init_raises_without_comtypes():
    from bad_ass_mcp.backend.windows import WindowsBackend

    with patch("bad_ass_mcp.backend.windows._HAS_UIA", False):
        with pytest.raises(RuntimeError, match="comtypes"):
            WindowsBackend()


# ── _resolve staleness ───────────────────────────────────────────────

_win32 = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows + comtypes")


@_win32
def test_resolve_raises_stale_on_element_not_available():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    fake_el = MagicMock()
    # Simulate UIA_E_ELEMENTNOTAVAILABLE when accessing CurrentControlType
    err = type("COMError", (Exception,), {"hresult": _UIA_E_ELEMENTNOTAVAILABLE})()
    type(fake_el).CurrentControlType = property(fget=MagicMock(side_effect=err))
    backend._handles["h1"] = fake_el

    with pytest.raises(StaleHandleError):
        backend._resolve("h1")

    assert "h1" not in backend._handles


@_win32
def test_resolve_does_not_evict_on_transient_com_error():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    fake_el = MagicMock()
    # Simulate a transient COM error (e.g., E_FAIL = 0x80004005 = -2147467259)
    err = type("COMError", (Exception,), {"hresult": -2147467259})()
    type(fake_el).CurrentControlType = property(fget=MagicMock(side_effect=err))
    backend._handles["h2"] = fake_el

    result = backend._resolve("h2")
    assert result is fake_el
    assert "h2" in backend._handles


@_win32
def test_resolve_unknown_handle_raises():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    with pytest.raises(StaleHandleError, match="Unknown handle"):
        backend._resolve("no-such-handle")


# ── stop_recording path validation ───────────────────────────────────


@_win32
def test_stop_recording_rejects_unknown_extension():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    backend._recordings["h"] = (MagicMock(), "C:\\tmp\\bam-rec-h.mp4")
    with pytest.raises(ValueError, match=r"\.gif or \.mp4"):
        backend.stop_recording("h", "C:\\tmp\\output.webm")


@_win32
def test_stop_recording_rejects_unknown_handle():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    with pytest.raises(ValueError, match="No active recording"):
        backend.stop_recording("no-such-handle", "C:\\tmp\\out.gif")


# ── _wake_chromium result caching ─────────────────────────────────────


@_win32
def test_wake_chromium_failed_wake_stays_false():
    """A failed wake must keep reporting False on later calls — returning
    True for merely-attempted PIDs flips accessible=true on the second
    list_windows even though the tree never populated."""
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    backend._screen_reader_flag_set = True  # skip the system-wide poke

    with (
        patch.object(backend, "_find_renderer_hwnd", return_value=None),
        patch.object(backend._uia, "ElementFromHandle", return_value=None),
    ):
        assert backend._wake_chromium(1234, 42) is False
        assert backend._wake_chromium(1234, 42) is False  # cached, not True


@_win32
def test_wake_chromium_successful_wake_cached_true():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    backend._screen_reader_flag_set = True

    fake_root = MagicMock()
    fake_root.FindAll.return_value = MagicMock(Length=10)

    with (
        patch.object(backend, "_find_renderer_hwnd", return_value=None),
        patch.object(backend._uia, "ElementFromHandle", return_value=fake_root) as efh,
    ):
        assert backend._wake_chromium(1234, 43) is True
        assert backend._wake_chromium(1234, 43) is True

    efh.assert_called_once()  # second call served from cache


# ── mouse button flags ───────────────────────────────────────────────


def test_mouse_button_flags_cover_three_buttons():
    from bad_ass_mcp.backend.windows import _MOUSE_BUTTON_FLAGS

    assert set(_MOUSE_BUTTON_FLAGS) == {"left", "right", "middle"}
    # MOUSEEVENTF_LEFTDOWN/UP, RIGHTDOWN/UP, MIDDLEDOWN/UP from winuser.h
    assert _MOUSE_BUTTON_FLAGS["left"] == (0x0002, 0x0004)
    assert _MOUSE_BUTTON_FLAGS["right"] == (0x0008, 0x0010)
    assert _MOUSE_BUTTON_FLAGS["middle"] == (0x0020, 0x0040)


@_win32
def test_click_at_rejects_unknown_button():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    r = backend.click_at(1, 1, button="trackball")
    assert not r.ok


@_win32
def test_click_at_double_sends_two_press_release_pairs():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    sent: list[list[tuple[int, float, float]]] = []

    def fake_send(events):
        sent.append(list(events))
        return ActionResult(ok=True)

    with (
        patch.object(backend, "_send_mouse", side_effect=fake_send),
        patch.object(backend, "_move_pointer", return_value=ActionResult(ok=True)),
    ):
        assert backend.click_at(5, 6, button="right", clicks=2).ok
    # DOWN/UP ×2 with right-button flags in one batch (the move is separate)
    assert [f for f, _, _ in sent[0]] == [0x0008, 0x0010, 0x0008, 0x0010]


# ── SendInput struct layout ──────────────────────────────────────────


def test_input_struct_matches_win64_layout():
    """INPUT is 40 bytes on x64 with the union at offset 8 (ULONG_PTR
    alignment). The previous hand-packed layout put dx at 4, which shifted
    dwFlags into mouseData and made every event a no-op."""
    import ctypes

    from bad_ass_mcp.backend.windows import _INPUT, _KEYBDINPUT, _MOUSEINPUT

    assert ctypes.sizeof(_INPUT) == 40
    assert _INPUT.u.offset == 8
    assert ctypes.sizeof(_MOUSEINPUT) == 32
    assert _MOUSEINPUT.dx.offset == 0
    assert _MOUSEINPUT.dy.offset == 4
    assert _MOUSEINPUT.mouseData.offset == 8
    assert _MOUSEINPUT.dwFlags.offset == 12
    assert _MOUSEINPUT.time.offset == 16
    assert _MOUSEINPUT.dwExtraInfo.offset == 24
    assert _KEYBDINPUT.dwFlags.offset == 4


def test_input_struct_dwflags_lands_at_offset_20():
    """Writing through the union must put dwFlags at byte 20 of INPUT."""

    from bad_ass_mcp.backend.windows import _INPUT

    inp = _INPUT()
    inp.type = 0
    inp.mi.dx = 1
    inp.mi.dy = 2
    inp.mi.dwFlags = 0x8001
    raw = bytes(inp)
    assert int.from_bytes(raw[8:12], "little") == 1
    assert int.from_bytes(raw[12:16], "little") == 2
    assert int.from_bytes(raw[20:24], "little") == 0x8001
