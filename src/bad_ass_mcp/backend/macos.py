from __future__ import annotations

import re
import subprocess
import time
import uuid
from typing import Any

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo
from .base import DesktopBackend

try:
    from AppKit import NSWorkspace
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
    )
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        CGEventPostToPid,
        CGEventSetFlags,
        kCGEventFlagMaskCommand,
        kCGSessionEventTap,
    )

    _HAS_PYOBJC = True
except ImportError:
    _HAS_PYOBJC = False

# AX error codes (from AXError.h)
_kAXErrorSuccess = 0
_kAXErrorInvalidUIElement = -25202  # element has been destroyed
_kAXErrorNoValue = -25212  # attribute exists but currently has no value


def _ax_value_to_rect(ax_value: Any) -> tuple[float, float, float, float] | None:
    """Extract (x, y, w, h) from an AXValueRef (CGRect).

    PyObjC wraps AXValueRef as an opaque CF type. AXValueGetValue() takes a
    void* out-param that PyObjC cannot bridge, so we parse __str__ instead.
    The format is stable across macOS versions:
      {value = x:0.000000 y:34.000000 w:1710.000000 h:978.000000 type = kAXValueCGRectType}
    """
    m = re.search(
        r"x:(-?[\d.]+)\s+y:(-?[\d.]+)\s+w:(-?[\d.]+)\s+h:(-?[\d.]+)",
        str(ax_value),
    )
    if m:
        return tuple(float(g) for g in m.groups())  # type: ignore[return-value]
    return None


def _ax_get(element: Any, attr: str) -> Any:
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == _kAXErrorSuccess else None


def _ax_set(element: Any, attr: str, value: Any) -> bool:
    return AXUIElementSetAttributeValue(element, attr, value) == _kAXErrorSuccess


def _ax_do(element: Any, action: str) -> bool:
    return AXUIElementPerformAction(element, action) == _kAXErrorSuccess


def _quartz_key_press(keycode: int, flags: int = 0, pid: int | None = None) -> None:
    """Inject a key press/release pair via CoreGraphics.

    If pid is given, events are delivered directly to that process without
    stealing focus from the user's current window.
    """
    down = CGEventCreateKeyboardEvent(None, keycode, True)
    up = CGEventCreateKeyboardEvent(None, keycode, False)
    if flags:
        CGEventSetFlags(down, flags)
        CGEventSetFlags(up, flags)
    if pid:
        CGEventPostToPid(pid, down)
        CGEventPostToPid(pid, up)
    else:
        CGEventPost(kCGSessionEventTap, down)
        CGEventPost(kCGSessionEventTap, up)


def _quartz_type_char(char: str, pid: int | None = None) -> None:
    """Inject a single Unicode character via CoreGraphics keyboard event."""
    c = char[:1]
    if not c:
        return
    down = CGEventCreateKeyboardEvent(None, 0, True)
    up = CGEventCreateKeyboardEvent(None, 0, False)
    CGEventKeyboardSetUnicodeString(down, 1, c)
    CGEventKeyboardSetUnicodeString(up, 1, c)
    if pid:
        CGEventPostToPid(pid, down)
        CGEventPostToPid(pid, up)
    else:
        CGEventPost(kCGSessionEventTap, down)
        CGEventPost(kCGSessionEventTap, up)


def _role_name(element: Any) -> str:
    role = _ax_get(element, "AXRole") or "AXUnknown"
    return role[2:].lower() if role.startswith("AX") else role.lower()


# macOS virtual key codes for named keys
_KEY_CODES: dict[str, int] = {
    "Return": 0x24,
    "Enter": 0x24,
    "Escape": 0x35,
    "Tab": 0x30,
    "Space": 0x31,
    "BackSpace": 0x33,
    "Delete": 0x33,
    "ForwardDelete": 0x75,
    "Home": 0x73,
    "End": 0x77,
    "PageUp": 0x74,
    "PageDown": 0x79,
    "Up": 0x7E,
    "Down": 0x7D,
    "Left": 0x7B,
    "Right": 0x7C,
    "F1": 0x7A,
    "F2": 0x78,
    "F3": 0x63,
    "F4": 0x76,
    "F5": 0x60,
    "F6": 0x61,
    "F7": 0x62,
    "F8": 0x64,
    "F9": 0x65,
    "F10": 0x6D,
    "F11": 0x67,
    "F12": 0x6F,
}


class MacOSBackend(DesktopBackend):
    def __init__(self) -> None:
        if not _HAS_PYOBJC:
            raise RuntimeError("macOS backend requires PyObjC: pip install 'bad-ass-mcp[macos]'")
        self._handles: dict[str, Any] = {}
        self._handle_pids: dict[str, int] = {}  # handle_id → owning process PID
        self._recordings: dict[str, tuple[Any, str]] = {}

    # ── Internal helpers ──────────────────────────────────────────────

    def _register(self, element: Any, pid: int | None = None) -> str:
        h = str(uuid.uuid4())
        self._handles[h] = element
        if pid is not None:
            self._handle_pids[h] = pid
        return h

    def _resolve(self, handle_id: str) -> Any:
        element = self._handles.get(handle_id)
        if element is None:
            raise StaleHandleError(f"Unknown handle: {handle_id!r}")
        err, _ = AXUIElementCopyAttributeValue(element, "AXRole", None)
        # Only kAXErrorInvalidUIElement (-25202) means the widget is gone.
        # Other non-zero codes (e.g. kAXErrorCannotComplete, kAXErrorAttributeUnsupported)
        # are transient or element-quirks and do not indicate staleness.
        if err == _kAXErrorInvalidUIElement:
            del self._handles[handle_id]
            raise StaleHandleError(f"Handle {handle_id!r} is stale (widget gone)")
        return element

    def _to_handle(self, element: Any, pid: int | None = None) -> ElementHandle:
        handle_id = self._register(element, pid)
        role = _role_name(element)

        name = _ax_get(element, "AXTitle") or _ax_get(element, "AXDescription") or ""

        value = None
        raw = _ax_get(element, "AXValue")
        if raw is not None:
            value = str(raw)

        states: set[str] = set()
        if _ax_get(element, "AXEnabled"):
            states.add("enabled")
        if _ax_get(element, "AXFocused"):
            states.add("focused")
        if _ax_get(element, "AXSelected"):
            states.add("selected")
        if _ax_get(element, "AXExpanded"):
            states.add("expanded")
        if not _ax_get(element, "AXHidden"):
            states.add("visible")
        if role in ("checkbox", "radiobutton") and raw:
            states.add("checked")
        if role in ("textfield", "textarea", "combobox", "searchfield"):
            states.add("editable")

        return ElementHandle(id=handle_id, role=role, name=name, value=value, states=states)

    def _walk(
        self, element: Any, depth: int = 0, max_depth: int = 12, pid: int | None = None
    ) -> ElementHandle:
        handle = self._to_handle(element, pid)
        if depth < max_depth:
            for child in _ax_get(element, "AXChildren") or []:
                try:
                    handle.children.append(self._walk(child, depth + 1, max_depth, pid))
                except Exception:
                    pass
        return handle

    def _find_app_element(self, window_id: str) -> Any | None:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if str(pid) == window_id or name == window_id:
                return AXUIElementCreateApplication(pid)
        return None

    def _pid_for_window(self, window_id: str) -> int | None:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if str(pid) == window_id or name == window_id:
                return pid
        return None

    def _search(self, element: Any, role: str | None, name: str | None) -> list[Any]:
        results: list[Any] = []
        try:
            node_role = _role_name(element)
            node_name = _ax_get(element, "AXTitle") or _ax_get(element, "AXDescription") or ""
            if (role is None or node_role == role) and (name is None or node_name == name):
                results.append(element)
            for child in _ax_get(element, "AXChildren") or []:
                results.extend(self._search(child, role, name))
        except Exception:
            pass
        return results

    def _window_geometry(self, window_id: str) -> tuple[int, int, int, int] | None:
        app = self._find_app_element(window_id)
        if not app:
            return None
        wins = _ax_get(app, "AXWindows")
        if not wins:
            return None
        frame = _ax_get(wins[0], "AXFrame")
        if frame:
            rect = _ax_value_to_rect(frame)
            if rect:
                x, y, w, h = rect
                return (int(x), int(y), int(w), int(h))
        return None

    # ── DesktopBackend impl ───────────────────────────────────────────

    def list_windows(self) -> list[WindowInfo]:
        ws = NSWorkspace.sharedWorkspace()
        active = ws.frontmostApplication()
        active_pid = active.processIdentifier() if active else -1
        windows: list[WindowInfo] = []
        for app in ws.runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if not name or pid <= 0:
                continue
            ax_app = AXUIElementCreateApplication(pid)
            if not _ax_get(ax_app, "AXWindows"):
                continue
            windows.append(WindowInfo(id=str(pid), name=name, pid=pid, focused=(pid == active_pid)))
        return windows

    def get_tree(self, window_id: str) -> ElementHandle:
        app = self._find_app_element(window_id)
        if app is None:
            raise ValueError(f"No window found for id {window_id!r}")
        return self._walk(app, pid=self._pid_for_window(window_id))

    def find_elements(
        self, window_id: str, *, role=None, name=None, index=0
    ) -> list[ElementHandle]:
        app = self._find_app_element(window_id)
        if app is None:
            return []
        pid = self._pid_for_window(window_id)
        return [self._to_handle(n, pid) for n in self._search(app, role, name)]

    def click(self, handle_id: str) -> ActionResult:
        element = self._resolve(handle_id)
        try:
            for action in ("AXPress", "AXPick", "AXConfirm"):
                if _ax_do(element, action):
                    time.sleep(0.15)
                    return ActionResult(ok=True)
            return ActionResult(ok=False, error="No actionable AX action found")
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

    def type_text(self, handle_id: str, text: str) -> ActionResult:
        element = self._resolve(handle_id)
        # Primary: direct AXValue set — foreground-independent, handles all Unicode
        if _ax_set(element, "AXValue", text):
            return ActionResult(ok=True)
        # Fallback: write to clipboard then Cmd+V — no AppleScript injection surface.
        # Use the stored PID so Cmd+V lands in the right process without stealing focus.
        try:
            _ax_set(element, "AXFocused", True)
            time.sleep(0.05)
            result = subprocess.run(
                ["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=5
            )
            if result.returncode == 0:
                pid = self._handle_pids.get(handle_id)
                _quartz_key_press(0x09, kCGEventFlagMaskCommand, pid=pid)  # Cmd+V
                return ActionResult(ok=True)
            return ActionResult(ok=False, error="pbcopy failed")
        except Exception as e:
            return ActionResult(ok=False, error=f"Cannot set text: {e}")

    def select_option(self, handle_id: str, value: str) -> ActionResult:
        element = self._resolve(handle_id)
        for role in ("menuitem", "option"):
            nodes = self._search(element, role=role, name=value)
            if nodes:
                try:
                    _ax_do(nodes[0], "AXPress")
                    return ActionResult(ok=True)
                except Exception as e:
                    return ActionResult(ok=False, error=str(e))
        return ActionResult(ok=False, error=f"Option {value!r} not found")

    def get_value(self, handle_id: str) -> str | None:
        element = self._resolve(handle_id)
        raw = _ax_get(element, "AXValue")
        if raw is not None:
            return str(raw)
        return _ax_get(element, "AXTitle") or _ax_get(element, "AXDescription")

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
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            cmd = ["screencapture", "-x", "-t", "png"]
            if window_id:
                app = self._find_app_element(window_id)
                if app:
                    wins = _ax_get(app, "AXWindows")
                    if wins:
                        win_num = _ax_get(wins[0], "AXWindowID")
                        if win_num is not None:
                            cmd += ["-l", str(int(win_num))]
            cmd.append(path)
            subprocess.run(cmd, check=True, capture_output=True)
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return b""
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def start_recording(self, window_id: str | None = None, fps: int = 15) -> str:
        import os
        import tempfile

        handle = str(uuid.uuid4())
        video_path = os.path.join(tempfile.gettempdir(), f"bam-rec-{handle}.mp4")

        # Capture full screen via avfoundation; crop to window if geometry known
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "avfoundation",
            "-framerate",
            str(fps),
            "-capture_cursor",
            "1",
            "-i",
            "1:none",
        ]
        if window_id:
            geom = self._window_geometry(window_id)
            if geom:
                x, y, w, h = geom
                w -= w % 2
                h -= h % 2
                cmd += ["-vf", f"crop={w}:{h}:{x}:{y}"]

        cmd += ["-t", "1800", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", video_path]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._recordings[handle] = (proc, video_path)
        return handle

    def stop_recording(self, handle: str, output_path: str) -> str:
        import os

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

        import os

        try:
            os.unlink(video_path)
        except Exception:
            pass
        return canonical

    def press_key(self, key: str, window_id: str | None = None) -> ActionResult:
        pid = self._pid_for_window(window_id) if window_id else None
        try:
            keycode = _KEY_CODES.get(key)
            if keycode is not None:
                _quartz_key_press(keycode, pid=pid)
            else:
                _quartz_type_char(key[:1], pid=pid)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))
