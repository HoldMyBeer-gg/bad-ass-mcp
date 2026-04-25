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
import subprocess
import time
import uuid
import zlib
from typing import Any

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo
from .base import DesktopBackend

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

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_PASTE = 0x0302
_CF_UNICODETEXT = 13

# Window style flags for list_windows filtering
_WS_CAPTION = 0x00C00000
_WS_EX_TOOLWINDOW = 0x00000080
_GWL_STYLE = -16
_GWL_EXSTYLE = -20

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

    # ── Internal helpers ──────────────────────────────────────────────

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
        self, element: Any, depth: int = 0, max_depth: int = 12, hwnd: int | None = None
    ) -> ElementHandle:
        handle = self._to_handle(element, hwnd)
        if depth < max_depth:
            try:
                cond = self._uia.CreateTrueCondition()
                children = element.FindAll(_TreeScope_Children, cond)
                if children:
                    for i in range(children.Length):
                        try:
                            child = children.GetElement(i)
                            handle.children.append(self._walk(child, depth + 1, max_depth, hwnd))
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

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_cb(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            # Skip tool windows (system tray, floating toolbars, etc.)
            exstyle = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if exstyle & _WS_EX_TOOLWINDOW:
                return True
            # Skip windows without a title bar (overlays, splash screens, etc.)
            style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
            if not (style & _WS_CAPTION):
                return True
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
                WindowInfo(id=str(hwnd), name=name, pid=pid.value, focused=(hwnd == foreground))
            )
            return True

        _user32.EnumWindows(_enum_cb, 0)
        return windows

    def get_tree(self, window_id: str) -> ElementHandle:
        hwnd = int(window_id)
        if not _user32.IsWindow(hwnd):
            raise ValueError(f"No window found for id {window_id!r}")
        root = self._uia.ElementFromHandle(hwnd)
        if root is None:
            raise ValueError(f"No window found for id {window_id!r}")
        return self._walk(root, hwnd=hwnd)

    def find_elements(
        self, window_id: str, *, role=None, name=None, index=0
    ) -> list[ElementHandle]:
        hwnd = int(window_id)
        if not _user32.IsWindow(hwnd):
            return []
        root = self._uia.ElementFromHandle(hwnd)
        if root is None:
            return []
        return [self._to_handle(n, hwnd) for n in self._search(root, role, name)]

    def click(self, handle_id: str) -> ActionResult:
        element = self._resolve(handle_id)
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
        return ActionResult(ok=False, error="No actionable UIA pattern found")

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
        element = self._resolve(handle_id)
        for role in ("listitem", "menuitem", "treeitem", "tabitem"):
            nodes = self._search(element, role=role, name=value)
            if nodes:
                try:
                    pat = nodes[0].GetCurrentPattern(_UIA_SelectionItemPatternId)
                    if pat:
                        pat.QueryInterface(IUIAutomationSelectionItemPattern).Select()
                        return ActionResult(ok=True)
                except Exception:
                    pass
                try:
                    pat = nodes[0].GetCurrentPattern(_UIA_InvokePatternId)
                    if pat:
                        pat.QueryInterface(IUIAutomationInvokePattern).Invoke()
                        return ActionResult(ok=True)
                except Exception as e:
                    return ActionResult(ok=False, error=str(e))
        return ActionResult(ok=False, error=f"Option {value!r} not found")

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

    def screenshot(self, window_id: str | None = None) -> bytes:
        if window_id:
            hwnd = int(window_id)
        else:
            hwnd = _user32.GetDesktopWindow()

        rect = ctypes.wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return b""

        hwnd_dc = _user32.GetWindowDC(hwnd)
        mem_dc = _gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old_bmp = _gdi32.SelectObject(mem_dc, bitmap)

        _user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT

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
        _user32.ReleaseDC(hwnd, hwnd_dc)

        try:
            return _bgra_to_png(width, height, buf.raw)
        except Exception:
            return b""

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

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._recordings[handle] = (proc, video_path)
        return handle

    def stop_recording(self, handle: str, output_path: str) -> str:
        canonical = os.path.realpath(os.path.expanduser(output_path))
        if not canonical.lower().endswith(".gif"):
            raise ValueError(f"output_path must end with .gif (got {output_path!r})")

        if handle not in self._recordings:
            raise ValueError(f"No active recording with handle {handle!r}")
        proc, video_path = self._recordings.pop(handle)
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-vf",
                "fps=12,scale=900:-1:flags=lanczos,split[s0][s1];"
                "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
                canonical,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace"))

        try:
            os.unlink(video_path)
        except Exception:
            pass
        return canonical

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
