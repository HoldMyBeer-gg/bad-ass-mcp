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
    # screenshot + click_at instead of round-tripping through AX. Cases:
    # pre-handshake Tauri/Electron, custom OpenGL/Vulkan canvases, raw
    # immediate-mode toolkits without AccessKit. Note: egui *with* the
    # accesskit feature DOES surface here as accessible=True.
    accessible: bool = True


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
