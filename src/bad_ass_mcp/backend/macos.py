from __future__ import annotations

import re
import subprocess
import time
import uuid
from typing import Any

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo
from .base import DesktopBackend, prune_tree

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
        CGEventCreateMouseEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        CGEventPostToPid,
        CGEventSetFlags,
        CGWindowListCopyWindowInfo,
        kCGEventFlagMaskCommand,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
        kCGNullWindowID,
        kCGSessionEventTap,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
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


def _quartz_mouse_click(x: float, y: float) -> None:
    """Inject a left mouse click at the given screen coordinates via the HID
    event tap.

    Delivery via kCGHIDEventTap is the lowest-level path — the WindowServer
    performs hit-testing as it would for a real click, which is what makes
    this reach Tauri/Electron/CEF webview content. Higher-level paths
    (CGEventPostToPid, kCGSessionEventTap) bypass the hit-test pipeline and
    silently no-op for webviews while still returning success.

    Tradeoff: HID delivery is system-global, so this DOES affect the focused
    window — whatever is at (x, y) gets the click. Coordinates are in the
    global display space (top-left origin), same as AXFrame.
    """
    point = (float(x), float(y))
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def _cg_onscreen_windows() -> list[dict]:
    """Return on-screen window dicts from the WindowServer.

    Catches windows the AX API doesn't surface — Tauri/Electron/CEF apps
    often don't expose AXWindows until they get focus, but they're already
    drawing pixels so the WindowServer knows about them.
    """
    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    return [dict(w) for w in raw]


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


def _ax_name(element: Any) -> str:
    """Best available human-readable label for an element.

    AXTitle/AXDescription cover most controls, but icon buttons and other
    graphical controls often carry neither — they surface as name="" and a
    caller can't tell what they do. Fall back through the attributes screen
    readers use to voice such controls, most specific first:

      AXTitle           the visible label
      AXDescription     the accessibility description
      AXTitleUIElement  a separate element that labels this one (e.g. the
                        text beside a field); use its own title/value
      AXHelp            tooltip text
      AXRoleDescription a human phrase for the role ("close button") — the
                        weakest, only when nothing more specific labels it

    Returns "" when the element is genuinely unlabelled everywhere.
    """
    name = _ax_get(element, "AXTitle") or _ax_get(element, "AXDescription")
    if name:
        return name

    title_el = _ax_get(element, "AXTitleUIElement")
    if title_el is not None:
        linked = _ax_get(title_el, "AXTitle") or _ax_get(title_el, "AXValue")
        if linked:
            return str(linked)

    help_text = _ax_get(element, "AXHelp")
    if help_text:
        return str(help_text)

    # AXRoleDescription is the last resort. Skip it when it just echoes the
    # role ("button" for a button) — that adds nothing over the role field.
    # Keep it when it's more specific ("close button", "minimize button").
    role_desc = _ax_get(element, "AXRoleDescription")
    if role_desc and role_desc.lower() != _role_name(element):
        return str(role_desc)

    return ""


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


# Attributes that tell Chromium-family apps "an assistive tech is listening"
# so they build their (lazy) AX tree. AXManualAccessibility is Electron's
# documented switch; AXEnhancedUserInterface is what VoiceOver sets and what
# plain Chrome/CEF watch — but it also makes some apps animate window moves,
# so it's the fallback, not the first choice.
_AX_WAKE_ATTRS = ("AXManualAccessibility", "AXEnhancedUserInterface")
# How long to wait for a freshly-woken webview to publish AXWindows.
_AX_WAKE_TIMEOUT = 1.5
# Chromium (Electron/CEF and every Chromium browser) nests the real content
# deep: from the AXApplication root, Vivaldi's mail/web area doesn't begin
# until depth ~13 and runs to ~22, and GPU-composited pages add still more
# wrapper layers. A shallow cap returns a hollow husk of empty AXGroups —
# get_tree looked broken while find_elements (uncapped) saw everything. 60
# clears real trees with headroom; _WALK_MAX_NODES keeps the walk bounded
# regardless of depth so a pathological/cyclic tree can't run away.
_WALK_MAX_DEPTH = 60
_WALK_MAX_NODES = 20000


class MacOSBackend(DesktopBackend):
    def __init__(self) -> None:
        if not _HAS_PYOBJC:
            raise RuntimeError("macOS backend requires PyObjC: pip install 'bad-ass-mcp[macos]'")
        self._handles: dict[str, Any] = {}
        self._handle_pids: dict[str, int] = {}  # handle_id → owning process PID
        self._recordings: dict[str, tuple[Any, str]] = {}
        self._woken_pids: set[int] = set()  # PIDs we've already tried to wake

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

        name = _ax_name(element)

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
        self,
        element: Any,
        depth: int = 0,
        max_depth: int = _WALK_MAX_DEPTH,
        pid: int | None = None,
        budget: list[int] | None = None,
    ) -> ElementHandle:
        # budget is a shared 1-element list so the node cap spans the whole
        # recursion, not each branch. Chromium trees are deep (see
        # _WALK_MAX_DEPTH); the cap keeps a runaway/cyclic tree bounded.
        if budget is None:
            budget = [_WALK_MAX_NODES]
        handle = self._to_handle(element, pid)
        budget[0] -= 1
        if depth < max_depth and budget[0] > 0:
            for child in self._ax_descendants(element, depth):
                if budget[0] <= 0:
                    break
                try:
                    handle.children.append(
                        self._walk(child, depth + 1, max_depth, pid, budget)
                    )
                except Exception:
                    pass
        return handle

    def _ax_descendants(self, element: Any, depth: int) -> list[Any]:
        """Children to recurse into, with the AXApplication-root quirk handled.

        On an AXApplication element, AXChildren and AXWindows are parallel
        attributes. Some toolkits (accesskit-macos as embedded by eframe,
        custom NSApplication subclasses) populate AXWindows but leave
        AXChildren empty on the app root, which would otherwise dead-end
        the walk at depth 0 even though System Events / VoiceOver see the
        full tree via 'windows of process X'. Prefer AXWindows there;
        fall back to AXChildren so menu-bar-only apps still work.
        """
        if depth == 0:
            wins = _ax_get(element, "AXWindows") or []
            if wins:
                return list(wins)
        return list(_ax_get(element, "AXChildren") or [])

    def _wake_ax_windows(self, pid: int) -> list[Any]:
        """Poke a lazy webview AX tree awake and wait briefly for AXWindows.

        Chromium (and therefore Electron/CEF) skips building its AX tree
        until an assistive tech announces itself; until then AXWindows is
        empty and the app looks canvas-only. Setting a wake attribute is the
        announcement. The set call only succeeds on apps that implement one
        of the attributes, so this is a cheap no-op for genuinely AX-less
        windows — no process-name sniffing needed. One attempt per PID per
        server lifetime.
        """
        if pid in self._woken_pids:
            return []
        self._woken_pids.add(pid)
        ax_app = AXUIElementCreateApplication(pid)
        if not any(_ax_set(ax_app, attr, True) for attr in _AX_WAKE_ATTRS):
            return []
        deadline = time.monotonic() + _AX_WAKE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.25)
            wins = _ax_get(ax_app, "AXWindows") or []
            if wins:
                return list(wins)
        return []

    def _find_app_element(self, window_id: str) -> Any | None:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if str(pid) == window_id or name == window_id:
                return AXUIElementCreateApplication(pid)
        # NSWorkspace.runningApplications() can lag in a long-running daemon —
        # fall back to direct AX-by-PID. AXUIElementCreateApplication accepts
        # any PID; we only return the element if it actually has AXWindows
        # (otherwise we've created an element for a dead/AX-less PID and
        # downstream queries would silently return nothing).
        try:
            pid_int = int(window_id)
        except (TypeError, ValueError):
            return None
        for win in _cg_onscreen_windows():
            try:
                if int(win.get("kCGWindowOwnerPID", 0)) == pid_int:
                    ax_app = AXUIElementCreateApplication(pid_int)
                    if _ax_get(ax_app, "AXWindows"):
                        return ax_app
                    if self._wake_ax_windows(pid_int):
                        return ax_app
                    return None
            except (TypeError, ValueError):
                continue
        return None

    def _pid_for_window(self, window_id: str) -> int | None:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if str(pid) == window_id or name == window_id:
                return pid
        # NSWorkspace.runningApplications() can lag in a long-running daemon —
        # apps that started after server init may be missing for a while.
        # CGWindowList sees them via the WindowServer regardless, which is
        # the same backfill list_windows() already does for Tauri/Electron/CEF.
        try:
            pid_int = int(window_id)
        except (TypeError, ValueError):
            return None
        for win in _cg_onscreen_windows():
            try:
                if int(win.get("kCGWindowOwnerPID", 0)) == pid_int:
                    return pid_int
            except (TypeError, ValueError):
                continue
        return None

    def _search(
        self, element: Any, role: str | None, name: str | None, depth: int = 0
    ) -> list[Any]:
        results: list[Any] = []
        try:
            node_role = _role_name(element)
            node_name = _ax_name(element)
            if (role is None or node_role == role) and (name is None or node_name == name):
                results.append(element)
            for child in self._ax_descendants(element, depth):
                results.extend(self._search(child, role, name, depth + 1))
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
        seen_pids: set[int] = set()
        for app in ws.runningApplications():
            pid = app.processIdentifier()
            name = app.localizedName() or ""
            if not name or pid <= 0:
                continue
            ax_app = AXUIElementCreateApplication(pid)
            ax_wins = _ax_get(ax_app, "AXWindows") or []
            if not ax_wins:
                continue
            # All AX windows minimized → app is docked; capture/click won't work
            # until something is restored. Surfacing this lets callers either
            # un-minimize first or skip the entry instead of guessing.
            minimized = all(bool(_ax_get(w, "AXMinimized")) for w in ax_wins)
            windows.append(
                WindowInfo(
                    id=str(pid),
                    name=name,
                    pid=pid,
                    focused=(pid == active_pid),
                    minimized=minimized,
                    bounds=self._cg_primary_bounds_for_pid(pid),
                )
            )
            seen_pids.add(pid)

        # Augment with WindowServer-visible apps that haven't exposed AXWindows.
        # This catches Tauri/Electron/CEF apps before they've finished their
        # AX handshake. We pick the first normal-layer window per PID and use
        # the app's localized name (or the window owner name) for display.
        running_by_pid = {
            int(app.processIdentifier()): app
            for app in ws.runningApplications()
            if int(app.processIdentifier()) > 0
        }
        for win in _cg_onscreen_windows():
            try:
                pid = int(win.get("kCGWindowOwnerPID", 0))
                layer = int(win.get("kCGWindowLayer", 0))
                bounds = win.get("kCGWindowBounds") or {}
                rect: tuple[int, int, int, int] | None = (
                    int(bounds.get("X", 0)),
                    int(bounds.get("Y", 0)),
                    int(bounds.get("Width", 0)),
                    int(bounds.get("Height", 0)),
                )
                if rect[2] * rect[3] <= 0:
                    rect = None
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen_pids or layer != 0:
                continue
            app = running_by_pid.get(pid)
            owner = (app.localizedName() if app else None) or win.get("kCGWindowOwnerName") or ""
            if not owner:
                continue
            # NSWorkspace.runningApplications() lags in a long-running daemon,
            # so apps that launched after server init can land here even when
            # they DO have a working AX adapter (egui+accesskit, freshly
            # bundled .apps, etc.). Probe AX directly via the PID rather than
            # blindly stamping accessible=False — the marker is meant for
            # genuinely-canvas-only apps (Tauri/Electron pre-handshake), not
            # for any app NSWorkspace happened to miss this tick.
            ax_app = AXUIElementCreateApplication(pid)
            ax_wins = _ax_get(ax_app, "AXWindows") or []
            if not ax_wins:
                # Chromium-family trees are lazy, not absent — announce
                # ourselves as an assistive tech and re-probe before
                # stamping accessible=False.
                ax_wins = self._wake_ax_windows(pid)
            ax_accessible = bool(ax_wins)
            windows.append(
                WindowInfo(
                    id=str(pid),
                    name=owner,
                    pid=pid,
                    focused=(pid == active_pid),
                    bounds=rect,
                    accessible=ax_accessible,
                )
            )
            seen_pids.add(pid)
        return windows

    def get_tree(self, window_id: str, *, max_depth: int | None = None) -> ElementHandle:
        app = self._find_app_element(window_id)
        if app is None:
            raise ValueError(f"No window found for id {window_id!r}")
        depth_cap = _WALK_MAX_DEPTH if max_depth is None else max_depth
        tree = self._walk(app, max_depth=depth_cap, pid=self._pid_for_window(window_id))
        return prune_tree(tree)

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

    def _cg_window_bounds(self, win_num: int) -> tuple[int, int] | None:
        """Return (width, height) in points for an on-screen CGWindow number.

        Used as a sanity check before handing the number to screencapture: a
        zero-area window produces an empty PNG that's harder to debug than
        a clean error up front.
        """
        for win in _cg_onscreen_windows():
            try:
                if int(win.get("kCGWindowNumber", 0)) != win_num:
                    continue
                bounds = win.get("kCGWindowBounds") or {}
                w = int(bounds.get("Width", 0))
                h = int(bounds.get("Height", 0))
                return (w, h)
            except (TypeError, ValueError):
                continue
        return None

    def _cg_primary_bounds_for_pid(self, pid: int) -> tuple[int, int, int, int] | None:
        """Largest layer-0 window bounds (x, y, w, h) for a PID, in global coords.

        Mirrors the selection logic in _cg_window_number_for_pid so the
        bounds reported by list_windows correspond to the window screenshot
        would capture. Falls back to other layers when there's no layer-0
        match (Tauri/Electron settings panels show up that way).
        """
        primary: tuple[int, tuple[int, int, int, int]] | None = None
        secondary: tuple[int, tuple[int, int, int, int]] | None = None
        for win in _cg_onscreen_windows():
            try:
                if int(win.get("kCGWindowOwnerPID", 0)) != pid:
                    continue
                layer = int(win.get("kCGWindowLayer", 0))
                bounds = win.get("kCGWindowBounds") or {}
                x = int(bounds.get("X", 0))
                y = int(bounds.get("Y", 0))
                w = int(bounds.get("Width", 0))
                h = int(bounds.get("Height", 0))
            except (TypeError, ValueError):
                continue
            area = w * h
            if area <= 0:
                continue
            rect = (x, y, w, h)
            if layer == 0:
                if primary is None or area > primary[0]:
                    primary = (area, rect)
            else:
                if secondary is None or area > secondary[0]:
                    secondary = (area, rect)
        if primary is not None:
            return primary[1]
        if secondary is not None:
            return secondary[1]
        return None

    def _cg_window_number_for_pid(self, pid: int) -> int | None:
        """Find a CGWindow number for the given PID via the WindowServer.

        Returns the largest window owned by the PID, preferring normal-layer
        (layer == 0) but falling back to any layer if the app's only on-screen
        windows are sheets / floating panels / settings dialogs (Tauri likes
        to do this). Zero-area / off-screen entries are skipped.
        """
        primary: tuple[int, int] | None = None  # (area, window_number)  layer 0
        secondary: tuple[int, int] | None = None  # any other layer
        for win in _cg_onscreen_windows():
            try:
                if int(win.get("kCGWindowOwnerPID", 0)) != pid:
                    continue
                layer = int(win.get("kCGWindowLayer", 0))
                num = int(win.get("kCGWindowNumber", 0))
                bounds = win.get("kCGWindowBounds") or {}
                area = int(bounds.get("Width", 0)) * int(bounds.get("Height", 0))
            except (TypeError, ValueError):
                continue
            if num <= 0 or area <= 0:
                continue
            if layer == 0:
                if primary is None or area > primary[0]:
                    primary = (area, num)
            else:
                if secondary is None or area > secondary[0]:
                    secondary = (area, num)
        if primary is not None:
            return primary[1]
        if secondary is not None:
            return secondary[1]
        return None

    def screenshot(self, window_id: str | None = None, output_path: str | None = None) -> bytes:
        import os
        import tempfile

        win_num: int | None = None
        if window_id:
            app = self._find_app_element(window_id)
            if app:
                # Iterate every AXWindow — the first one isn't always the
                # visible/foreground one. Hidden/minimized windows may
                # not expose AXWindowID, so skip them and keep looking.
                for w in _ax_get(app, "AXWindows") or []:
                    raw = _ax_get(w, "AXWindowID")
                    if raw is not None:
                        win_num = int(raw)
                        break
            if win_num is None:
                pid = self._pid_for_window(window_id)
                if pid is not None:
                    win_num = self._cg_window_number_for_pid(pid)
            if win_num is None:
                # Don't silently fall back to a full-desktop capture — that produced
                # huge useless screenshots when the window couldn't be located.
                # Common cause: app is minimized to the dock, so its windows are
                # off-screen and CGWindowListOptionOnScreenOnly skips them.
                pid = self._pid_for_window(window_id)
                if pid is not None:
                    ax_app = AXUIElementCreateApplication(pid)
                    wins = _ax_get(ax_app, "AXWindows") or []
                    if wins and all(bool(_ax_get(w, "AXMinimized")) for w in wins):
                        raise ValueError(
                            f"Window for {window_id!r} is minimized to the dock — "
                            "restore it before capturing"
                        )
                raise ValueError(f"No on-screen window found for window_id {window_id!r}")

            dims = self._cg_window_bounds(win_num)
            if dims is not None:
                w, h = dims
                if w <= 0 or h <= 0:
                    raise ValueError(
                        f"Window {window_id!r} has zero area ({w}x{h}) — nothing to capture"
                    )

        if output_path:
            target = os.path.realpath(os.path.expanduser(output_path))
            cleanup = False
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                target = f.name
            cleanup = True

        try:
            cmd = ["screencapture", "-x", "-t", "png"]
            if win_num is not None:
                cmd += ["-l", str(win_num)]
            cmd.append(target)
            subprocess.run(cmd, check=True, capture_output=True)
            if output_path:
                # Caller asked for a path — don't read the bytes back. The MCP
                # tool layer returns just the path so we don't blow the token
                # budget on multi-megabyte base64.
                return b""
            with open(target, "rb") as f:
                return f.read()
        except Exception:
            return b""
        finally:
            if cleanup:
                try:
                    os.unlink(target)
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

    def click_at(self, x: float, y: float, window_id: str | None = None) -> ActionResult:
        # window_id is accepted for API parity but ignored on macOS: HID-tap
        # delivery is global, and PID-targeted delivery (CGEventPostToPid)
        # was tried first but silently no-ops on webview content because it
        # bypasses WindowServer hit-testing. Caller is responsible for
        # ensuring (x, y) lands on the intended window.
        try:
            _quartz_mouse_click(x, y)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

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
