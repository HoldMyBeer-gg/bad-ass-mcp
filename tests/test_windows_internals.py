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


# ── type_text: every strategy is verified by reading the value back ──
# egui (accesskit) exposes a writable ValuePattern and accepts SetValue
# without raising, then ignores it. Trusting "no exception" reported ok
# while the field stayed empty.


def _field():
    """A field that only takes text through the strategy named `works`."""
    state = {"value": ""}

    def attempt(name, works):
        def go():
            if name == works:
                state["value"] += "framexml"

        return (name, go)

    return state, attempt


def test_type_text_strategies_fall_through_an_ignored_setvalue():
    from bad_ass_mcp.backend.windows import _first_verified

    state, attempt = _field()
    r = _first_verified(
        "framexml",
        [attempt("setvalue", "keys"), attempt("wm_char", "keys"), attempt("keys", "keys")],
        lambda: state["value"],
        settle=0,
    )
    assert r.ok
    assert state["value"] == "framexml"


def test_type_text_stops_at_the_first_strategy_that_verifies():
    from bad_ass_mcp.backend.windows import _first_verified

    state, attempt = _field()
    r = _first_verified(
        "framexml",
        [attempt("setvalue", "setvalue"), attempt("keys", "setvalue")],
        lambda: state["value"],
        settle=0,
    )
    assert r.ok
    assert state["value"] == "framexml"  # not typed twice


def test_type_text_fails_loudly_when_nothing_sticks():
    from bad_ass_mcp.backend.windows import _first_verified

    state, attempt = _field()
    r = _first_verified(
        "framexml",
        [attempt("setvalue", None), attempt("keys", None)],
        lambda: state["value"],
        settle=0,
    )
    assert not r.ok
    assert "setvalue" in r.error and "keys" in r.error


def test_type_text_trusts_the_first_strategy_when_nothing_can_be_read_back():
    from bad_ass_mcp.backend.windows import _first_verified

    calls = []
    r = _first_verified(
        "x",
        [("setvalue", lambda: calls.append("setvalue")), ("keys", lambda: calls.append("keys"))],
        lambda: None,
        settle=0,
    )
    assert r.ok
    assert calls == ["setvalue"]


def test_type_text_skips_a_strategy_that_raises():
    from bad_ass_mcp.backend.windows import _first_verified

    def boom():
        raise RuntimeError("no pattern")

    state, attempt = _field()
    r = _first_verified(
        "framexml", [("setvalue", boom), attempt("keys", "keys")], lambda: state["value"], settle=0
    )
    assert r.ok


def test_type_text_rejects_a_doubled_partial_type():
    """A strategy that half-typed, then another that appended, must not
    pass because the text appears somewhere in the field."""
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": ""}

    def partial():
        state["value"] += "frame"

    def full():
        state["value"] += "framexml"

    r = _first_verified(
        "framexml", [("WM_CHAR", partial), ("keys", full)], lambda: state["value"], settle=0
    )
    assert not r.ok


def test_type_text_accepts_text_appended_to_existing_content():
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": "old "}

    def keys():
        state["value"] += "framexml"

    r = _first_verified("framexml", [("keys", keys)], lambda: state["value"], settle=0)
    assert r.ok


def test_type_text_says_which_strategy_worked():
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": ""}

    def keys():
        state["value"] = "x"

    r = _first_verified(
        "x", [("SetValue", lambda: None), ("keys", keys)], lambda: state["value"], settle=0
    )
    assert r.ok and r.detail == "keys"


def test_unicode_key_inputs_carry_case_and_need_no_shift():
    """KEYEVENTF_UNICODE sends the character itself, so 'FrameXML' keeps its
    capitals (VkKeyScan's shift byte was being masked off)."""
    from bad_ass_mcp.backend.windows import _unicode_key_inputs

    ins = _unicode_key_inputs("Fx")
    assert [(i.ki.wScan, i.ki.dwFlags) for i in ins] == [
        (ord("F"), 0x4),
        (ord("F"), 0x4 | 0x2),
        (ord("x"), 0x4),
        (ord("x"), 0x4 | 0x2),
    ]
    assert all(i.type == 1 and i.ki.wVk == 0 for i in ins)


def test_unicode_key_inputs_split_astral_characters_into_surrogates():
    from bad_ass_mcp.backend.windows import _unicode_key_inputs

    ins = _unicode_key_inputs("🦀")
    assert [i.ki.wScan for i in ins] == [0xD83E, 0xD83E, 0xDD80, 0xDD80]


def test_clipboard_calls_get_pointer_sized_types():
    """GlobalAlloc/GlobalLock default to a C int restype, which truncates a
    64-bit handle; memmove then wrote to null (access violation)."""
    import ctypes

    from bad_ass_mcp.backend.windows import _declare_clipboard_types

    k, u = MagicMock(), MagicMock()
    _declare_clipboard_types(k, u)
    assert k.GlobalAlloc.restype is ctypes.c_void_p
    assert k.GlobalLock.restype is ctypes.c_void_p
    assert k.GlobalLock.argtypes == (ctypes.c_void_p,)
    assert k.GlobalUnlock.argtypes == (ctypes.c_void_p,)
    assert u.SetClipboardData.argtypes == (ctypes.c_uint, ctypes.c_void_p)


# ── password fields read back masked (rusty-win-crab, WebView2) ──────


def test_password_field_stops_at_setvalue_when_the_mask_length_matches():
    """UIA returns '••••' for a password input. Comparing to the plain text
    fell through to the keys strategy, which typed it a second time."""
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": ""}

    def set_value():
        state["value"] = "•" * 40

    def keys():
        state["value"] += "•" * 40

    r = _first_verified(
        "t" * 40, [("SetValue", set_value), ("keys", keys)], lambda: state["value"], settle=0
    )
    assert r.ok and r.detail == "SetValue"
    assert state["value"] == "•" * 40


def test_password_field_with_the_wrong_mask_length_is_not_a_pass():
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": ""}

    def doubled():
        state["value"] = "•" * 80

    r = _first_verified("t" * 40, [("keys", doubled)], lambda: state["value"], settle=0)
    assert not r.ok


def test_masked_append_counts_from_the_starting_mask():
    from bad_ass_mcp.backend.windows import _first_verified

    state = {"value": "•" * 3}

    def keys():
        state["value"] += "•" * 4

    r = _first_verified("abcd", [("keys", keys)], lambda: state["value"], settle=0)
    assert r.ok


# ── get_tree on a WebView2 window that hasn't built its tree yet ─────


def test_until_populated_retries_an_empty_tree():
    from bad_ass_mcp.backend.windows import _until_populated
    from bad_ass_mcp.types import ElementHandle

    empty = ElementHandle(id="r", role="pane", name="Hold My Beer - Web content")
    full = ElementHandle(
        id="r", role="pane", name="x", children=[ElementHandle(id="c", role="button", name="Go")]
    )
    walks = iter([empty, empty, full])
    with patch("time.sleep"):
        tree = _until_populated(lambda: next(walks), timeout=2.0)
    assert tree is full


def test_until_populated_gives_up_and_returns_the_empty_tree():
    from bad_ass_mcp.backend.windows import _until_populated
    from bad_ass_mcp.types import ElementHandle

    empty = ElementHandle(id="r", role="pane", name="x")
    tree = _until_populated(lambda: empty, timeout=0.05)
    assert tree is empty


# ── clipboard hygiene for the paste strategy ─────────────────────────


class _FakeClipboard:
    """Dict-backed stand-in for the Win32 clipboard calls."""

    def __init__(self, contents):
        self.data = dict(contents)
        self.names: dict[str, int] = {}
        self.opened = 0
        self.history: list[dict] = []

    def open(self):
        self.opened += 1
        return True

    def close(self):
        self.opened -= 1
        self.history.append(dict(self.data))

    def formats(self):
        return list(self.data)

    def get(self, fmt):
        return self.data.get(fmt)

    def empty(self):
        self.data.clear()

    def put(self, fmt, raw):
        self.data[fmt] = raw

    def register(self, name):
        return self.names.setdefault(name, 0xC000 + len(self.names))


def test_paste_puts_the_user_clipboard_back_with_every_format():
    from bad_ass_mcp.backend.windows import _CF_UNICODETEXT, _borrowed_clipboard

    users = {_CF_UNICODETEXT: "hunter2\0".encode("utf-16-le"), 0xC123: b"\x89PNG..."}
    cb = _FakeClipboard(users)
    with _borrowed_clipboard(cb, "*Sylvanas*.m2"):
        assert cb.data[_CF_UNICODETEXT] == "*Sylvanas*.m2\0".encode("utf-16-le")
    assert cb.data == users
    assert cb.opened == 0


def test_paste_restores_even_when_the_paste_raises():
    from bad_ass_mcp.backend.windows import _CF_UNICODETEXT, _borrowed_clipboard

    users = {_CF_UNICODETEXT: "mine\0".encode("utf-16-le")}
    cb = _FakeClipboard(users)
    with pytest.raises(RuntimeError):
        with _borrowed_clipboard(cb, "x"):
            raise RuntimeError("boom")
    assert cb.data == users


def test_borrowed_text_is_kept_out_of_clipboard_history_and_cloud():
    from bad_ass_mcp.backend.windows import _borrowed_clipboard

    cb = _FakeClipboard({})
    with _borrowed_clipboard(cb, "x"):
        by_name = {n: cb.data.get(f) for n, f in cb.names.items()}
    assert "ExcludeClipboardContentFromMonitorProcessing" in by_name
    assert by_name["CanIncludeInClipboardHistory"] == struct.pack("<I", 0)
    assert by_name["CanUploadToCloudClipboard"] == struct.pack("<I", 0)


def test_an_empty_user_clipboard_is_left_empty():
    from bad_ass_mcp.backend.windows import _borrowed_clipboard

    cb = _FakeClipboard({})
    with _borrowed_clipboard(cb, "x"):
        pass
    assert cb.data == {}


def test_password_fields_never_use_the_clipboard():
    from bad_ass_mcp.backend.windows import _typing_attempts

    names = lambda xs: [n for n, _ in xs]  # noqa: E731
    attempts = [("SetValue", 1), ("WM_CHAR", 2), ("paste", 3), ("keys", 4)]
    assert names(_typing_attempts(attempts, password=True)) == ["SetValue", "WM_CHAR", "keys"]
    assert names(_typing_attempts(attempts, password=False)) == [
        "SetValue",
        "WM_CHAR",
        "paste",
        "keys",
    ]
