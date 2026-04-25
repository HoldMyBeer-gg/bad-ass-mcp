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
from bad_ass_mcp.types import StaleHandleError

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
def test_stop_recording_rejects_non_gif_extension():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    backend._recordings["h"] = (MagicMock(), "C:\\tmp\\bam-rec-h.mp4")
    with pytest.raises(ValueError, match=r"\.gif"):
        backend.stop_recording("h", "C:\\tmp\\output.mp4")


@_win32
def test_stop_recording_rejects_unknown_handle():
    from bad_ass_mcp.backend.windows import WindowsBackend

    backend = WindowsBackend()
    with pytest.raises(ValueError, match="No active recording"):
        backend.stop_recording("no-such-handle", "C:\\tmp\\out.gif")
