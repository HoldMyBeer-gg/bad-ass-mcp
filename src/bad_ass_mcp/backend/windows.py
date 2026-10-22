"""Windows UI Automation backend.

Uses Microsoft UI Automation (UIA) via comtypes for accessibility tree
access and element interaction, with ctypes for Win32 window enumeration,
key injection, and GDI-based screenshots.

Window IDs are HWNDs (window handles), not PIDs — more precise on Windows
where a single process may own multiple windows.
"""

from __future__ import annotations

import os
import struct
import time
import uuid
import zlib
from typing import Any

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo
from .base import DesktopBackend, prune_tree

_HAS_UIA = False

try:
    import ctypes
    import ctypes.wintypes

    # Silence noisy comtypes logging during first-time type-library generation
    import logging

    import comtypes
    import comtypes.client

    _ctl = logging.getLogger("comtypes")
    _prev = _ctl.level
    _ctl.setLevel(logging.ERROR)
    try:
        comtypes.client.GetModule("UIAutomationCore.dll")
    finally:
        _ctl.setLevel(_prev)

    from comtypes.gen.UIAutomationClient import (  # type: ignore[import-untyped]
        CUIAutomation,
        IUIAutomation,
        IUIAutomationExpandCollapsePattern,
        IUIAutomationInvokePattern,
        IUIAutomationSelectionItemPattern,
        IUIAutomationTogglePattern,
        IUIAutomationValuePattern,
    )

    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _kernel32 = ctypes.windll.kernel32
    _HAS_UIA = True
except (ImportError, AttributeError, OSError):
    pass


# ── Win32 constants ──────────────────────────────────────────────────

_SPI_GETSCREENREADERRUNNING = 0x0046
_SPI_SETSCREENREADERRUNNING = 0x0047
_SPIF_SENDCHANGE = 0x0002

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_PASTE = 0x0302
_CF_UNICODETEXT = 13

# SendInput mouse flags (MOUSEEVENTF_*)
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSE_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_GA_ROOT = 2
_GA_ROOTOWNER = 3


# ── SendInput structures ──────────────────────────────────────────────
# Real ctypes definitions rather than hand-packed bytes. The INPUT union
# holds a ULONG_PTR, so on x64 it is 8-aligned and starts at offset 8, not
# 4: a hand-packed layout that assumed offset 4 shifted every field by one
# slot, so dwFlags landed in mouseData and SendInput happily delivered
# forty bytes of nothing while still returning "n events sent". Fixed
# widths (c_int32 for LONG, c_uint32 for DWORD) keep the layout identical
# on every host so the offsets can be unit-tested off Windows.
import ctypes as _ct  # noqa: E402


class _MOUSEINPUT(_ct.Structure):
    _fields_ = [
        ("dx", _ct.c_int32),
        ("dy", _ct.c_int32),
        ("mouseData", _ct.c_uint32),
        ("dwFlags", _ct.c_uint32),
        ("time", _ct.c_uint32),
        ("dwExtraInfo", _ct.c_size_t),
    ]


class _KEYBDINPUT(_ct.Structure):
    _fields_ = [
        ("wVk", _ct.c_uint16),
        ("wScan", _ct.c_uint16),
        ("dwFlags", _ct.c_uint32),
        ("time", _ct.c_uint32),
        ("dwExtraInfo", _ct.c_size_t),
    ]


class _HARDWAREINPUT(_ct.Structure):
    _fields_ = [
        ("uMsg", _ct.c_uint32),
        ("wParamL", _ct.c_uint16),
        ("wParamH", _ct.c_uint16),
    ]


class _INPUT_UNION(_ct.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(_ct.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", _ct.c_uint32), ("u", _INPUT_UNION)]


_INPUT_MOUSE = 0


class _POINT(_ct.Structure):
    _fields_ = [("x", _ct.c_int32), ("y", _ct.c_int32)]


# Window style flags for list_windows filtering
_WS_CAPTION = 0x00C00000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_GWL_STYLE = -16
_GWL_EXSTYLE = -20

# DWM cloaked attribute — filters Store apps, virtual-desktop hidden windows
_DWMWA_CLOAKED = 14

# UIA pattern IDs
_UIA_InvokePatternId = 10000
_UIA_ValuePatternId = 10002
_UIA_ExpandCollapsePatternId = 10005
_UIA_SelectionItemPatternId = 10010
_UIA_TogglePatternId = 10015

# UIA tree scope & states
_TreeScope_Children = 0x2
_TreeScope_Subtree = 0x7
_ToggleState_On = 1
_ExpandCollapseState_Expanded = 1

# UIA HRESULT for stale elements
_UIA_E_ELEMENTNOTAVAILABLE = -2147220991  # 0x80040201

# ── Virtual key codes ────────────────────────────────────────────────

_VK_CODES: dict[str, int] = {
    "Return": 0x0D,
    "Enter": 0x0D,
    "Escape": 0x1B,
    "Tab": 0x09,
    "Space": 0x20,
    "BackSpace": 0x08,
    "Delete": 0x2E,
    "Home": 0x24,
    "End": 0x23,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "Up": 0x26,
    "Down": 0x28,
    "Left": 0x25,
    "Right": 0x27,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
}

# ── UIA ControlType → normalised role name ───────────────────────────

_CONTROL_TYPE_ROLES: dict[int, str] = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "textfield",
    50005: "hyperlink",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "treeitem",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "datagrid",
    50029: "dataitem",
    50030: "document",
    50031: "splitbutton",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "headeritem",
    50036: "table",
    50037: "titlebar",
    50038: "separator",
}


# ── GDI helpers ──────────────────────────────────────────────────────


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _bgra_to_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode raw BGRA pixel data as a PNG (RGB, no alpha).

    Minimal PNG encoder using only stdlib (struct + zlib).
    """

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    stride = width * 4
    raw = bytearray(height * (1 + width * 3))
    dst = 0
    for y in range(height):
        raw[dst] = 0  # PNG row filter: None
        dst += 1
        row = bgra[y * stride : y * stride + stride]
        # Reorder BGRA → RGB by slicing channel offsets across the whole row at once
        raw[dst : dst + width * 3 : 3] = row[2::4]  # R
        raw[dst + 1 : dst + width * 3 : 3] = row[1::4]  # G
        raw[dst + 2 : dst + width * 3 : 3] = row[0::4]  # B
        dst += width * 3

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


def _make_lparam(vk: int, is_up: bool = False) -> int:
    """Construct lParam for WM_KEYDOWN / WM_KEYUP with correct scan code."""
    scan = _user32.MapVirtualKeyW(vk, 0) & 0xFF  # MAPVK_VK_TO_VSC
    lparam = 1  # repeat count
    lparam |= scan << 16
    if is_up:
        lparam |= 0xC0000000  # previous-key-state + transition-state
    return lparam


# ── Backend ──────────────────────────────────────────────────────────


class WindowsBackend(DesktopBackend):
    """Windows backend using UI Automation + ctypes Win32 API."""

    # Chromium nests real content deep. get_tree() reroutes to the
    # Chrome_RenderWidgetHostHWND renderer child, which starts the DOM
    # shallower than the top-level HWND, so a shallow cap bit less here than
    # on macOS — but it was still fragile for deep pages. 60 clears real
    # trees; _WALK_MAX_NODES bounds a runaway/cyclic walk regardless of depth.
    _WALK_MAX_DEPTH = 60
    _WALK_MAX_NODES = 20000

    def __init__(self) -> None:
        if not _HAS_UIA:
            raise RuntimeError(
                "Windows backend requires comtypes: pip install 'bad-ass-mcp[windows]'"
            )
        self._handles: dict[str, Any] = {}  # handle_id → live IUIAutomationElement
        self._handle_hwnds: dict[str, int] = {}  # handle_id → owning HWND
        self._recordings: dict[str, tuple[Any, str]] = {}

        # Per-monitor DPI awareness for accurate window coordinates
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                _user32.SetProcessDPIAware()
            except Exception:
                pass

        self._uia: Any = comtypes.CoCreateInstance(
            CUIAutomation._reg_clsid_,
            interface=IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        self._wake_results: dict[int, bool] = {}  # pid → did the wake produce a tree
        self._screen_reader_flag_set = False

    # ── Internal helpers ──────────────────────────────────────────────

    def _is_chromium_window(self, hwnd: int) -> bool:
        """Check if a window belongs to a Chromium-family app (Electron, CEF, Chrome)."""
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value == "Chrome_WidgetWin_1"

    def _find_renderer_hwnd(self, hwnd: int) -> int | None:
        """Find the Chrome_RenderWidgetHostHWND child — the actual DOM UIA root."""
        renderer = None

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def cb(child, _):
            nonlocal renderer
            buf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(child, buf, 256)
            if buf.value == "Chrome_RenderWidgetHostHWND":
                renderer = child
                return False  # stop enumeration
            return True

        _user32.EnumChildWindows(hwnd, cb, 0)
        return renderer

    def _chromium_root(self, hwnd: int) -> int:
        """Return the best HWND for UIA tree access on a Chromium window.

        For Chromium-family windows the DOM content lives under the
        Chrome_RenderWidgetHostHWND child, not the top-level HWND.
        Falls back to the original HWND if no renderer child is found.
        """
        if self._is_chromium_window(hwnd):
            renderer = self._find_renderer_hwnd(hwnd)
            if renderer is not None:
                return renderer
        return hwnd

    def _wake_chromium(self, hwnd: int, pid: int) -> bool:
        """Set the screen-reader-running flag so Chromium populates its UIA tree.

        Returns True if the tree has content after waiting, False otherwise.
        Pokes once per PID per server lifetime to avoid spam; the outcome is
        cached so a failed wake keeps reporting accessible=False instead of
        silently flipping to True on the next list_windows call.
        """
        cached = self._wake_results.get(pid)
        if cached is not None:
            return cached
        self._wake_results[pid] = False

        # Set the system-wide screen reader flag (idempotent — only done once)
        if not self._screen_reader_flag_set:
            try:
                _user32.SystemParametersInfoW(
                    _SPI_SETSCREENREADERRUNNING, 1, None, _SPIF_SENDCHANGE
                )
                self._screen_reader_flag_set = True
            except Exception:
                pass

        # Find the renderer child and query it to trigger tree construction
        renderer = self._find_renderer_hwnd(hwnd)
        target = renderer if renderer else hwnd
        try:
            root = self._uia.ElementFromHandle(target)
            if root is None:
                return False
        except Exception:
            return False

        # Wait up to 2s for Chromium to build the a11y tree.
        # Check subtree, not just direct children — some browsers (Vivaldi)
        # wrap the entire DOM under a single unnamed group element.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                cond = self._uia.CreateTrueCondition()
                subtree = root.FindAll(_TreeScope_Subtree, cond)
                if subtree and subtree.Length > 5:
                    self._wake_results[pid] = True
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _register(self, element: Any, hwnd: int | None = None) -> str:
        h = str(uuid.uuid4())
        self._handles[h] = element
        if hwnd is not None:
            self._handle_hwnds[h] = hwnd
        return h

    def _resolve(self, handle_id: str) -> Any:
        element = self._handles.get(handle_id)
        if element is None:
            raise StaleHandleError(f"Unknown handle: {handle_id!r}")
        try:
            element.CurrentControlType  # live probe — throws if element is gone
        except Exception as exc:
            # Only evict on UIA_E_ELEMENTNOTAVAILABLE or non-COM exceptions
            hresult = getattr(exc, "hresult", None)
            if hresult == _UIA_E_ELEMENTNOTAVAILABLE or hresult is None:
                del self._handles[handle_id]
                self._handle_hwnds.pop(handle_id, None)
                raise StaleHandleError(f"Handle {handle_id!r} is stale (widget gone)")
            # Other COM errors are transient — keep the handle
        return element

    def _to_handle(self, element: Any, hwnd: int | None = None) -> ElementHandle:
        handle_id = self._register(element, hwnd)

        try:
            ct = element.CurrentControlType
        except Exception:
            ct = 0
        role = _CONTROL_TYPE_ROLES.get(ct, "unknown")

        try:
            name = element.CurrentName or ""
        except Exception:
            name = ""

        value = None
        try:
            pat = element.GetCurrentPattern(_UIA_ValuePatternId)
            if pat:
                value = pat.QueryInterface(IUIAutomationValuePattern).CurrentValue
        except Exception:
            pass

        states: set[str] = set()
        try:
            if element.CurrentIsEnabled:
                states.add("enabled")
        except Exception:
            pass
        try:
            if element.CurrentHasKeyboardFocus:
                states.add("focused")
        except Exception:
            pass
        try:
            if not element.CurrentIsOffscreen:
                states.add("visible")
        except Exception:
            pass
        try:
            pat = element.GetCurrentPattern(_UIA_TogglePatternId)
            if pat:
                tp = pat.QueryInterface(IUIAutomationTogglePattern)
                if tp.CurrentToggleState == _ToggleState_On:
                    states.add("checked")
        except Exception:
            pass
        try:
            pat = element.GetCurrentPattern(_UIA_SelectionItemPatternId)
            if pat:
                si = pat.QueryInterface(IUIAutomationSelectionItemPattern)
                if si.CurrentIsSelected:
                    states.add("selected")
        except Exception:
            pass
        try:
            pat = element.GetCurrentPattern(_UIA_ExpandCollapsePatternId)
            if pat:
                ec = pat.QueryInterface(IUIAutomationExpandCollapsePattern)
                if ec.CurrentExpandCollapseState == _ExpandCollapseState_Expanded:
                    states.add("expanded")
        except Exception:
            pass
        if role in ("textfield", "combobox", "document"):
            states.add("editable")

        return ElementHandle(id=handle_id, role=role, name=name, value=value, states=states)

    def _walk(
        self,
        element: Any,
        depth: int = 0,
        max_depth: int = _WALK_MAX_DEPTH,
        hwnd: int | None = None,
        budget: list[int] | None = None,
    ) -> ElementHandle:
        # budget is a shared 1-element list so the node cap spans the whole
        # recursion, not each branch.
        if budget is None:
            budget = [self._WALK_MAX_NODES]
        handle = self._to_handle(element, hwnd)
        budget[0] -= 1
        if depth < max_depth and budget[0] > 0:
            try:
                cond = self._uia.CreateTrueCondition()
                children = element.FindAll(_TreeScope_Children, cond)
                if children:
                    for i in range(children.Length):
                        if budget[0] <= 0:
                            break
                        try:
                            child = children.GetElement(i)
                            handle.children.append(
                                self._walk(child, depth + 1, max_depth, hwnd, budget)
                            )
                        except Exception:
                            pass
            except Exception:
                pass
        return handle

    def _search(self, element: Any, role: str | None, name: str | None) -> list[Any]:
        results: list[Any] = []
        try:
            ct = element.CurrentControlType
            node_role = _CONTROL_TYPE_ROLES.get(ct, "unknown")
            node_name = element.CurrentName or ""
            if (role is None or node_role == role) and (name is None or node_name == name):
                results.append(element)
            cond = self._uia.CreateTrueCondition()
            children = element.FindAll(_TreeScope_Children, cond)
            if children:
                for i in range(children.Length):
                    try:
                        results.extend(self._search(children.GetElement(i), role, name))
                    except Exception:
                        pass
        except Exception:
            pass
        return results

    def _hwnd_for_element(self, handle_id: str) -> int | None:
        """Return the HWND associated with a handle."""
        hwnd = self._handle_hwnds.get(handle_id)
        if hwnd:
            return hwnd
        element = self._handles.get(handle_id)
        if element:
            try:
                wh = element.CurrentNativeWindowHandle
                if wh:
                    return wh
            except Exception:
                pass
        return None

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        """Return (x, y, width, height) for a window handle."""
        rect = ctypes.wintypes.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        return None

    def _clipboard_set(self, text: str) -> None:
        """Copy text to the Windows clipboard via Win32 API."""
        data = (text + "\0").encode("utf-16-le")
        _user32.OpenClipboard(0)
        try:
            _user32.EmptyClipboard()
            h = _kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            p = _kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            _kernel32.GlobalUnlock(h)
            _user32.SetClipboardData(_CF_UNICODETEXT, h)
        finally:
            _user32.CloseClipboard()

    # ── DesktopBackend impl ───────────────────────────────────────────

    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        foreground = _user32.GetForegroundWindow()
        # An owned popup (effect rack, modal dialog) can hold the foreground
        # while its owner is the app the caller thinks of as focused. Mark
        # both, so focused:true survives even when the popup itself is
        # filtered out below.
        try:
            fg_owner = _user32.GetAncestor(foreground, _GA_ROOTOWNER) if foreground else 0
        except Exception:
            fg_owner = 0

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_cb(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            exstyle = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
            has_caption = bool(style & _WS_CAPTION)
            # Tool windows are usually tray icons and floating toolbars, but
            # some apps (Adobe's DroverLord frames) open real dialogs with
            # WS_EX_TOOLWINDOW set. Keep one when it is the foreground window
            # or the foreground window's owner; drop the rest as before.
            if exstyle & _WS_EX_TOOLWINDOW and hwnd not in (foreground, fg_owner):
                return True
            if not has_caption:
                # Frameless windows (Tauri, Electron, CEF) lack WS_CAPTION.
                # Accept them if they have a title + nonzero area + aren't
                # cloaked (virtual-desktop hidden) or non-activatable overlays.
                if exstyle & _WS_EX_NOACTIVATE:
                    return True
                rect = ctypes.wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.right - rect.left) < 50 or (rect.bottom - rect.top) < 50:
                    return True
                # DWM cloaked check — filters Store app ghosts & virtual-desktop windows
                try:
                    cloaked = ctypes.c_int(0)
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                    )
                    if cloaked.value:
                        return True
                except Exception:
                    pass
            length = _user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            name = buf.value
            if not name:
                return True
            pid = ctypes.wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append(
                WindowInfo(
                    id=str(hwnd),
                    name=name,
                    pid=pid.value,
                    focused=(hwnd == foreground or hwnd == fg_owner),
                    minimized=bool(_user32.IsIconic(hwnd)),
                    bounds=self._window_rect(hwnd),
                    accessible=True,  # updated below for Chromium windows
                )
            )
            return True

        _user32.EnumWindows(_enum_cb, 0)

        # Wake Chromium/Electron windows so their UIA trees are populated
        for w in windows:
            hwnd = int(w.id)
            if self._is_chromium_window(hwnd):
                w.accessible = self._wake_chromium(hwnd, w.pid)

        return windows

    def get_tree(self, window_id: str, *, max_depth: int | None = None) -> ElementHandle:
        hwnd = int(window_id)
        if not _user32.IsWindow(hwnd):
            raise ValueError(f"No window found for id {window_id!r}")
        target = self._chromium_root(hwnd)
        root = self._uia.ElementFromHandle(target)
        if root is None:
            raise ValueError(f"No window found for id {window_id!r}")
        depth_cap = self._WALK_MAX_DEPTH if max_depth is None else max_depth
        return prune_tree(self._walk(root, max_depth=depth_cap, hwnd=hwnd))

    def find_elements(
        self, window_id: str, *, role=None, name=None, index=0
    ) -> list[ElementHandle]:
        hwnd = int(window_id)
        if not _user32.IsWindow(hwnd):
            return []
        target = self._chromium_root(hwnd)
        root = self._uia.ElementFromHandle(target)
        if root is None:
            return []
        return [self._to_handle(n, hwnd) for n in self._search(root, role, name)]

    def element_bounds(self, handle_id: str) -> tuple[float, float, float, float] | None:
        element = self._resolve(handle_id)
        try:
            r = element.CurrentBoundingRectangle  # tagRECT: left, top, right, bottom
            left, top, right, bottom = r.left, r.top, r.right, r.bottom
        except Exception:
            return None
        if right <= left or bottom <= top:
            return None
        return (float(left), float(top), float(right - left), float(bottom - top))

    def click(self, handle_id: str, button: str = "left", clicks: int = 1) -> ActionResult:
        element = self._resolve(handle_id)
        if button != "left" or clicks != 1:
            # No UIA pattern expresses a right- or double-click; go physical.
            return self._click_element_at(handle_id, button=button, clicks=clicks)
        # InvokePattern — buttons, links, menu items
        try:
            pat = element.GetCurrentPattern(_UIA_InvokePatternId)
            if pat:
                pat.QueryInterface(IUIAutomationInvokePattern).Invoke()
                time.sleep(0.15)
                return ActionResult(ok=True)
        except Exception:
            pass
        # TogglePattern — checkboxes, toggle buttons
        try:
            pat = element.GetCurrentPattern(_UIA_TogglePatternId)
            if pat:
                pat.QueryInterface(IUIAutomationTogglePattern).Toggle()
                time.sleep(0.15)
                return ActionResult(ok=True)
        except Exception:
            pass
        # ExpandCollapsePattern — tree nodes, combo boxes
        try:
            pat = element.GetCurrentPattern(_UIA_ExpandCollapsePatternId)
            if pat:
                pat.QueryInterface(IUIAutomationExpandCollapsePattern).Expand()
                time.sleep(0.15)
                return ActionResult(ok=True)
        except Exception:
            pass
        # SelectionItemPattern — list items, tab items
        try:
            pat = element.GetCurrentPattern(_UIA_SelectionItemPatternId)
            if pat:
                pat.QueryInterface(IUIAutomationSelectionItemPattern).Select()
                time.sleep(0.15)
                return ActionResult(ok=True)
        except Exception:
            pass
        # Nothing actionable (custom-drawn panels, owner-drawn controls):
        # send a real click at the element's center instead.
        return self._click_element_at(handle_id)

    def type_text(self, handle_id: str, text: str) -> ActionResult:
        element = self._resolve(handle_id)
        # Primary: ValuePattern.SetValue — fully foreground-independent
        try:
            pat = element.GetCurrentPattern(_UIA_ValuePatternId)
            if pat:
                vp = pat.QueryInterface(IUIAutomationValuePattern)
                if not vp.CurrentIsReadOnly:
                    vp.SetValue(text)
                    return ActionResult(ok=True)
        except Exception:
            pass
        # Fallback: post WM_CHAR messages to the target HWND — foreground-independent
        # but limited to apps that handle WM_CHAR (most Win32 controls do)
        hwnd = self._hwnd_for_element(handle_id)
        if hwnd:
            try:
                for ch in text:
                    _user32.PostMessageW(hwnd, _WM_CHAR, ord(ch), 0)
                return ActionResult(ok=True)
            except Exception:
                pass
        # Final fallback: clipboard + WM_PASTE — works for all standard Win32 edit
        # controls without needing to simulate modifier keys via PostMessage (which
        # doesn't update GetKeyState and would deliver a bare 'V' instead of Ctrl+V).
        try:
            element.SetFocus()
            time.sleep(0.05)
            self._clipboard_set(text)
            target = hwnd or _user32.GetForegroundWindow()
            _user32.PostMessageW(target, _WM_PASTE, 0, 0)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=f"Cannot set text: {e}")

    def select_option(self, handle_id: str, value: str) -> ActionResult:
        """Three strategies, each verified by reading the value back:

        1. UIA item search under the control + SelectionItem.Select / Invoke.
           Fails on stock Win32 droplists whose items only exist while the
           list is dropped down.
        2. ValuePattern.SetValue on the combo itself. Works on some combos,
           silently does nothing on others (observed on the same dialog).
        3. Real click to focus + type the item text + Return — the only
           strategy that worked every time on Win32 COMBOBOX controls.

        A strategy that returns without error but leaves the control reading
        something else is treated as a failure, not a success."""
        element = self._resolve(handle_id)
        attempts: list[str] = []

        # 1. a11y item search
        for role in ("listitem", "menuitem", "treeitem", "tabitem"):
            nodes = self._search(element, role=role, name=value)
            if not nodes:
                continue
            acted = False
            try:
                pat = nodes[0].GetCurrentPattern(_UIA_SelectionItemPatternId)
                if pat:
                    pat.QueryInterface(IUIAutomationSelectionItemPattern).Select()
                    acted = True
            except Exception:
                pass
            if not acted:
                try:
                    pat = nodes[0].GetCurrentPattern(_UIA_InvokePatternId)
                    if pat:
                        pat.QueryInterface(IUIAutomationInvokePattern).Invoke()
                        acted = True
                except Exception as e:
                    attempts.append(f"invoke {role}: {e}")
            if acted:
                time.sleep(0.1)
                if self._option_selected(handle_id, value) is not False:
                    return ActionResult(ok=True)
                attempts.append(f"{role} selected but value did not change")
            break
        else:
            attempts.append("no matching item in UIA subtree")

        # 2. ValuePattern.SetValue
        try:
            pat = element.GetCurrentPattern(_UIA_ValuePatternId)
            if pat:
                vp = pat.QueryInterface(IUIAutomationValuePattern)
                if not vp.CurrentIsReadOnly:
                    vp.SetValue(value)
                    time.sleep(0.1)
                    if self._option_selected(handle_id, value) is True:
                        return ActionResult(ok=True)
                    attempts.append("SetValue accepted but value did not change")
        except Exception as e:
            attempts.append(f"SetValue: {e}")

        # 3. keystrokes
        r = self._select_option_by_keystrokes(handle_id, value)
        if r.ok:
            return r
        attempts.append(f"keystrokes: {r.error}")
        return ActionResult(ok=False, error=f"Option {value!r}: " + "; ".join(attempts))

    def get_value(self, handle_id: str) -> str | None:
        element = self._resolve(handle_id)
        try:
            pat = element.GetCurrentPattern(_UIA_ValuePatternId)
            if pat:
                return pat.QueryInterface(IUIAutomationValuePattern).CurrentValue
        except Exception:
            pass
        try:
            name = element.CurrentName
            if name:
                return name
        except Exception:
            pass
        return None

    def wait_for_window(self, title_pattern: str, timeout: float = 5.0) -> WindowInfo | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in self.list_windows():
                if title_pattern.lower() in w.name.lower():
                    return w
            time.sleep(0.1)
        return None

    def wait_for_element(
        self, window_id: str, *, role=None, name=None, state=None, timeout=5.0
    ) -> ElementHandle | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for el in self.find_elements(window_id, role=role, name=name):
                if state is None or state in el.states:
                    return el
            time.sleep(0.1)
        return None

    def screenshot(self, window_id: str | None = None, output_path: str | None = None) -> bytes:
        if window_id:
            hwnd = int(window_id)
            if not _user32.IsWindow(hwnd):
                raise ValueError(f"No on-screen window found for window_id {window_id!r}")
            if _user32.IsIconic(hwnd):
                raise ValueError(f"Window {window_id!r} is minimized — restore it before capturing")
            rect = ctypes.wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            left, top = rect.left, rect.top
            width = rect.right - rect.left
            height = rect.bottom - rect.top
        else:
            # Full screen. PrintWindow on GetDesktopWindow() renders nothing
            # (solid black) under DWM, so the desktop path BitBlts from the
            # screen DC over the virtual-screen rectangle instead, which also
            # covers every monitor rather than just the primary.
            hwnd = None
            left = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            top = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            width = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
            height = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            raise ValueError(f"Window {window_id!r} has zero area ({width}x{height})")

        hwnd_dc = _user32.GetWindowDC(hwnd) if hwnd else _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old_bmp = _gdi32.SelectObject(mem_dc, bitmap)

        if hwnd:
            _user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
        else:
            _gdi32.BitBlt(mem_dc, 0, 0, width, height, hwnd_dc, left, top, _SRCCOPY | _CAPTUREBLT)

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # negative → top-down scanlines
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(width * height * 4)
        _gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)

        _gdi32.SelectObject(mem_dc, old_bmp)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(hwnd or 0, hwnd_dc)

        try:
            png = _bgra_to_png(width, height, buf.raw)
        except Exception:
            return b""

        if output_path:
            target = os.path.realpath(os.path.expanduser(output_path))
            with open(target, "wb") as f:
                f.write(png)
            return b""
        return png

    def start_recording(self, window_id: str | None = None, fps: int = 15) -> str:
        import tempfile

        handle = str(uuid.uuid4())
        video_path = os.path.join(tempfile.gettempdir(), f"bam-rec-{handle}.mp4")

        cmd = ["ffmpeg", "-y", "-f", "gdigrab", "-framerate", str(fps)]

        if window_id:
            geom = self._window_rect(int(window_id))
            if geom:
                x, y, w, h = geom
                w -= w % 2
                h -= h % 2
                cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]

        cmd += [
            "-i",
            "desktop",
            "-t",
            "1800",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            video_path,
        ]

        self._recordings[handle] = self._spawn_recorder(cmd, video_path)
        return handle

    def stop_recording(self, handle: str, output_path: str) -> str:
        canonical, fmt = self._recording_target(output_path)

        if handle not in self._recordings:
            raise ValueError(f"No active recording with handle {handle!r}")
        video_path = self._finish_recorder(self._recordings.pop(handle))
        return self._export_recording(video_path, canonical, fmt)

    def _send_mouse(self, events: list[tuple[int, float, float]]) -> ActionResult:
        """Send a batch of absolute mouse events via SendInput.

        events: (flags, x, y) in screen pixels; MOVE|ABSOLUTE is OR'd in so
        every event carries a position. Coordinates are mapped onto the
        virtual screen so secondary monitors (and negative origins) work."""
        vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        vw = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        vh = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            return ActionResult(ok=False, error="Could not determine screen size")
        # MOUSEEVENTF_VIRTUALDESK (0x4000) makes ABSOLUTE coords span the
        # whole virtual desktop instead of the primary monitor.
        base = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | 0x4000
        n = len(events)
        inputs = (_INPUT * n)()
        for i, (flags, x, y) in enumerate(events):
            inputs[i].type = _INPUT_MOUSE
            inputs[i].mi.dx = int((round(x) - vx) * 65535 / vw)
            inputs[i].mi.dy = int((round(y) - vy) * 65535 / vh)
            inputs[i].mi.mouseData = 0
            inputs[i].mi.dwFlags = base | flags
            inputs[i].mi.time = 0
            inputs[i].mi.dwExtraInfo = 0
        sent = _user32.SendInput(n, inputs, ctypes.sizeof(_INPUT))
        if sent != n:
            return ActionResult(ok=False, error=f"SendInput returned {sent}, expected {n}")
        return ActionResult(ok=True)

    def _activate_window_at(self, x: float, y: float) -> None:
        """Bring the top-level window under (x, y) to the foreground if it
        isn't already. MDI-style child windows (the Creation Kit's form
        dialogs) swallow the first click of an inactive window as
        activation only, so a double-click on them opened nothing until the
        second try. Best effort: SetForegroundWindow may refuse, and the
        click that follows will still activate the window the old way."""
        try:
            pt = _POINT(int(round(x)), int(round(y)))
            hwnd = _user32.WindowFromPoint(pt)
            if not hwnd:
                return
            root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
            fg = _user32.GetForegroundWindow()
            if root == fg or root == (_user32.GetAncestor(fg, _GA_ROOT) if fg else 0):
                return
            if _user32.SetForegroundWindow(root):
                time.sleep(0.1)
        except Exception:
            pass

    def _move_pointer(self, x: float, y: float) -> ActionResult:
        """Move the pointer and verify it arrived. SendInput reports success
        for events that did nothing (wrong struct layout, UIPI blocking a
        higher-integrity target, a remote session with no input desktop),
        so the only trustworthy signal is GetCursorPos afterwards."""
        r = self._send_mouse([(0, x, y)])
        if not r.ok:
            return r
        time.sleep(0.03)
        pt = _POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            if abs(pt.x - round(x)) > 2 or abs(pt.y - round(y)) > 2:
                return ActionResult(
                    ok=False,
                    error=(
                        f"Pointer did not reach ({round(x)}, {round(y)}); "
                        f"it is at ({pt.x}, {pt.y}). SendInput was accepted but "
                        "had no effect (blocked by a higher-integrity window?)"
                    ),
                )
        return ActionResult(ok=True)

    def click_at(
        self,
        x: float,
        y: float,
        window_id: str | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> ActionResult:
        # window_id accepted for API parity but ignored — SendInput is global,
        # same as Quartz CGEvent on macOS and xdotool on Linux.
        if button not in _MOUSE_BUTTON_FLAGS:
            return ActionResult(ok=False, error=f"Unknown button {button!r}")
        if clicks < 1:
            return ActionResult(ok=False, error="clicks must be >= 1")
        down, up = _MOUSE_BUTTON_FLAGS[button]
        try:
            # Move first on its own (verified) so hover state settles before
            # the press; then every press/release pair in one SendInput call
            # so the inter-click gap stays well inside GetDoubleClickTime().
            r = self._move_pointer(x, y)
            if not r.ok:
                return r
            self._activate_window_at(x, y)
            events = [(down, x, y), (up, x, y)] * clicks
            r = self._send_mouse(events)
            if not r.ok:
                return r
            time.sleep(0.05)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        button: str = "left",
        duration: float = 0.3,
    ) -> ActionResult:
        if button not in _MOUSE_BUTTON_FLAGS:
            return ActionResult(ok=False, error=f"Unknown button {button!r}")
        down, up = _MOUSE_BUTTON_FLAGS[button]
        steps = max(8, int(duration / 0.015))
        try:
            r = self._move_pointer(from_x, from_y)
            if not r.ok:
                return r
            time.sleep(0.05)
            r = self._send_mouse([(down, from_x, from_y)])
            if not r.ok:
                return r
            # Win32 drag detection needs the pointer to leave the
            # SM_CXDRAG/SM_CYDRAG rectangle while the button is held, and
            # most drop targets only arm after several WM_MOUSEMOVEs, so the
            # move is interpolated rather than jumped.
            time.sleep(0.05)
            for i in range(1, steps + 1):
                t = i / steps
                r = self._send_mouse(
                    [(0, from_x + (to_x - from_x) * t, from_y + (to_y - from_y) * t)]
                )
                if not r.ok:
                    return r
                time.sleep(max(duration, 0.1) / steps)
            time.sleep(0.05)
            r = self._send_mouse([(up, to_x, to_y)])
            if not r.ok:
                return r
            time.sleep(0.05)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

    def press_key(self, key: str, window_id: str | None = None) -> ActionResult:
        vk = _VK_CODES.get(key)
        hwnd = int(window_id) if window_id else None
        try:
            if hwnd:
                # PostMessage to target HWND — foreground-independent
                if vk is not None:
                    _user32.PostMessageW(hwnd, _WM_KEYDOWN, vk, _make_lparam(vk))
                    _user32.PostMessageW(hwnd, _WM_KEYUP, vk, _make_lparam(vk, True))
                else:
                    _user32.PostMessageW(hwnd, _WM_CHAR, ord(key[:1]), 0)
            else:
                # No target — use keybd_event for the foreground window
                if vk is None:
                    vk = _user32.VkKeyScanW(ord(key[:1])) & 0xFF
                scan = _user32.MapVirtualKeyW(vk, 0) & 0xFF
                _user32.keybd_event(vk, scan, 0, 0)  # down
                _user32.keybd_event(vk, scan, 0x0002, 0)  # up (KEYEVENTF_KEYUP)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))
