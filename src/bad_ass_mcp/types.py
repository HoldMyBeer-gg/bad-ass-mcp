from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WindowInfo:
    id: str
    name: str
    pid: int
    focused: bool
    minimized: bool = False
    # (x, y, width, height) in global screen coords, top-left origin.
    # None when the platform can't cheaply report it (e.g. Linux without
    # wmctrl). Callers compositing screenshot-relative click_at coords
    # need this to translate image pixels to screen pixels.
    bounds: tuple[int, int, int, int] | None = None
    # False when the window was discovered via an OS-level fallback path
    # because it never appeared in the platform a11y tree (CGWindowList on
    # macOS, _NET_CLIENT_LIST on Linux). find_elements / get_tree will
    # return empty for those windows — callers should go straight to
    # screenshot + click_at instead of round-tripping through AX.
    # Backends poke lazy Chromium-family trees awake (AXManualAccessibility
    # on macOS, the AT-SPI screen-reader flag on Linux) before stamping
    # this False, so Electron/CEF windows normally end up True; remaining
    # False cases are genuinely canvas-only: custom OpenGL/Vulkan surfaces,
    # raw immediate-mode toolkits without AccessKit. Note: egui *with* the
    # accesskit feature DOES surface here as accessible=True.
    accessible: bool = True
    # Titles of the app's top-level windows, where `name` is something
    # else: the process name on macOS, the AT-SPI application name on Linux
    # (empty for egui/accesskit). Empty when `name` already is the title.
    titles: tuple[str, ...] = ()

    def matches(self, pattern: str) -> bool:
        """Case-insensitive substring match against the name or any title,
        so one pattern finds the same app on every platform."""
        p = pattern.lower()
        return any(p in (s or "").lower() for s in (self.name, *self.titles))


@dataclass
class ElementHandle:
    id: str
    role: str
    name: str
    value: str | None = None
    states: set[str] = field(default_factory=set)
    children: list[ElementHandle] = field(default_factory=list)


@dataclass
class ActionResult:
    ok: bool
    error: str | None = None


class StaleHandleError(Exception):
    """Raised when an element handle is no longer valid (widget gone or UI changed)."""
