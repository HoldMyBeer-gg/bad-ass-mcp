from __future__ import annotations

import platform

from mcp.server.fastmcp import FastMCP

from .types import StaleHandleError

mcp = FastMCP("bad-ass-mcp")


def _backend():
    if not hasattr(_backend, "_instance"):
        os = platform.system()
        if os == "Linux":
            from .backend.linux import LinuxBackend

            _backend._instance = LinuxBackend()
        elif os == "Windows":
            from .backend.windows import WindowsBackend

            _backend._instance = WindowsBackend()
        elif os == "Darwin":
            from .backend.macos import MacOSBackend

            _backend._instance = MacOSBackend()
        else:
            raise RuntimeError(f"Unsupported platform: {os}")
    return _backend._instance


@mcp.tool()
def list_windows() -> list[dict]:
    """List all visible application windows on the desktop."""
    return [w.__dict__ for w in _backend().list_windows()]


@mcp.tool()
def get_tree(window_id: str) -> dict:
    """Return the full accessibility tree for a window as nested JSON.
    Use list_windows() first to get a window_id."""

    def serialise(el):
        return {
            "id": el.id,
            "role": el.role,
            "name": el.name,
            "value": el.value,
            "states": sorted(el.states),
            "children": [serialise(c) for c in el.children],
        }

    return serialise(_backend().get_tree(window_id))


@mcp.tool()
def find_elements(
    window_id: str,
    role: str | None = None,
    name: str | None = None,
) -> list[dict]:
    """Find interactive elements by role and/or name.
    Returns element handles — pass the 'id' field to click/type_text/etc.
    Common roles: button, combo box, text, entry, check box, menu item."""
    els = _backend().find_elements(window_id, role=role, name=name)
    return [
        {"id": e.id, "role": e.role, "name": e.name, "value": e.value, "states": sorted(e.states)}
        for e in els
    ]


@mcp.tool()
def click(handle_id: str) -> dict:
    """Click / invoke an element. Foreground-independent — does not steal focus.
    Get handle_id from find_elements() or get_tree()."""
    try:
        result = _backend().click(handle_id)
        return {"ok": result.ok, "error": result.error}
    except StaleHandleError as e:
        return {"ok": False, "error": f"Stale handle: {e}"}


@mcp.tool()
def type_text(handle_id: str, text: str) -> dict:
    """Type text into a field. Uses native SetValue — foreground-independent.
    Falls back to key injection if the element doesn't support SetValue."""
    try:
        result = _backend().type_text(handle_id, text)
        return {"ok": result.ok, "error": result.error}
    except StaleHandleError as e:
        return {"ok": False, "error": f"Stale handle: {e}"}


@mcp.tool()
def select_option(handle_id: str, value: str) -> dict:
    """Select an option in a combo box or list by its visible text."""
    try:
        result = _backend().select_option(handle_id, value)
        return {"ok": result.ok, "error": result.error}
    except StaleHandleError as e:
        return {"ok": False, "error": f"Stale handle: {e}"}


@mcp.tool()
def get_value(handle_id: str) -> dict:
    """Get the current text value or state of an element."""
    try:
        val = _backend().get_value(handle_id)
        return {"ok": True, "value": val}
    except StaleHandleError as e:
        return {"ok": False, "error": f"Stale handle: {e}"}


@mcp.tool()
def wait_for_window(title_pattern: str, timeout: float = 5.0) -> dict:
    """Wait until a window matching title_pattern appears. Returns null on timeout.
    Useful after triggering an action that opens a dialog."""
    w = _backend().wait_for_window(title_pattern, timeout)
    return w.__dict__ if w else {"error": f"Timeout: no window matching {title_pattern!r}"}


@mcp.tool()
def wait_for_element(
    window_id: str,
    role: str | None = None,
    name: str | None = None,
    state: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """Wait until a matching element exists and optionally has the given state.
    Useful after clicks that trigger async UI changes."""
    el = _backend().wait_for_element(window_id, role=role, name=name, state=state, timeout=timeout)
    if el is None:
        return {"error": "Timeout: element not found"}
    return {
        "id": el.id,
        "role": el.role,
        "name": el.name,
        "value": el.value,
        "states": sorted(el.states),
    }


@mcp.tool()
def screenshot(window_id: str | None = None) -> dict:
    """Capture a screenshot as base64 PNG. Last resort — prefer accessibility tools.
    Pass window_id to crop to a specific window, or omit for full screen."""
    import base64

    data = _backend().screenshot(window_id)
    if not data:
        return {"ok": False, "error": "Screenshot failed"}
    return {"ok": True, "data": base64.b64encode(data).decode()}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
