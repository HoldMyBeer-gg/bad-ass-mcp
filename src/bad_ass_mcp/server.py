from __future__ import annotations

# Trust model: this server runs over stdio and assumes the MCP client (e.g. Claude)
# is fully trusted. There is no authentication. Do not expose this server over a
# network socket or to untrusted processes — the AX/AT-SPI permissions it holds
# grant control over every application on the desktop.
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


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width/height from a PNG IHDR chunk without decoding the image.

    PNG layout: 8-byte signature, then IHDR chunk (length=13, type='IHDR',
    width u32 BE at offset 16, height u32 BE at offset 20). 24 bytes is
    enough to answer the question — cheaper than spawning sips.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


@mcp.tool()
def screenshot(window_id: str | None = None, output_path: str | None = None) -> dict:
    """Capture a screenshot as PNG. Last resort — prefer accessibility tools.

    Pass window_id to crop to a specific window, or omit for full screen.
    Pass output_path (e.g. '/tmp/shot.png') to write the PNG to disk and get
    back {ok, path, width, height} — strongly preferred for any real-window
    capture, since base64-inline screenshots routinely overflow the response
    token budget. Without output_path, returns {ok, data, width, height}
    with base64-encoded PNG bytes.

    width/height are pixel dimensions; on retina displays a window with
    720×450 logical points captures at 1440×900 px. Divide by the window's
    point size to recover the scale factor for coordinate math.

    Errors with {ok: False, error: ...} if window_id is given but the window
    cannot be located — does NOT silently fall back to a full-desktop capture.
    """
    import base64

    try:
        data = _backend().screenshot(window_id, output_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if output_path:
        import os

        target = os.path.realpath(os.path.expanduser(output_path))
        if not os.path.exists(target):
            return {"ok": False, "error": "Screenshot failed"}
        result: dict = {"ok": True, "path": target}
        with open(target, "rb") as f:
            head = f.read(24)
        dims = _png_dimensions(head)
        if dims:
            result["width"], result["height"] = dims
        return result
    if not data:
        return {"ok": False, "error": "Screenshot failed"}
    result = {"ok": True, "data": base64.b64encode(data).decode()}
    dims = _png_dimensions(data)
    if dims:
        result["width"], result["height"] = dims
    return result


@mcp.tool()
def click_at(x: float, y: float, window_id: str | None = None) -> dict:
    """Click at absolute screen coordinates (top-left origin, in points).

    Fallback for when accessibility-based clicking can't reach a target —
    webview content (Tauri/Electron/CEF), custom-drawn UI, OpenGL/canvas, etc.

    Delivery is system-global (kCGHIDEventTap on macOS) so the click actually
    reaches webviews — this was a regression in the first version, which used
    PID-targeted delivery that silently no-op'd on webview content while
    still returning ok. The cost is that the click affects whatever window
    is at (x, y), so make sure the target window is on top first.

    window_id is advisory only — the call cannot verify delivery, just that
    the events were posted."""
    try:
        result = _backend().click_at(x, y, window_id)
        return {"ok": result.ok, "error": result.error}
    except NotImplementedError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def start_recording(window_id: str | None = None, fps: int = 15) -> dict:
    """Start recording the screen (or a specific window) as video.
    Returns a handle to pass to stop_recording.
    Crops to the window if window_id is provided."""
    try:
        handle = _backend().start_recording(window_id, fps)
        return {"ok": True, "handle": handle}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def stop_recording(handle: str, output_path: str) -> dict:
    """Stop recording and export as a GIF.
    output_path must end in .gif — e.g. '/tmp/demo.gif'.
    Returns the path to the finished GIF."""
    try:
        path = _backend().stop_recording(handle, output_path)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def learn_layout(window_id: str, descriptors: dict) -> dict:
    """Resolve semantic names to live element handle IDs for the current session.

    Pass a map of {label: {role, name}} and get back {label: handle_id}.
    Store the result and use the handle IDs in run_sequence to skip repeated
    find_elements calls. Re-call after app restarts or window recreation.

    Example:
      learn_layout("1234", {
        "bold_button": {"role": "push button", "name": "Bold"},
        "editor":      {"role": "document",    "name": ""},
      })
    """
    return _backend().learn_layout(window_id, descriptors)


@mcp.tool()
def run_sequence(steps: list, stop_on_error: bool = True) -> list:
    """Execute a list of actions server-side in a single call — no round-trips.

    Each step is a dict with an "action" key plus action-specific fields:
      {"action": "click",            "handle": "..."}
      {"action": "click_at",         "x": 100.0, "y": 200.0, "window_id": null}
      {"action": "type",             "handle": "...", "text": "..."}
      {"action": "key",              "key": "Return"}
      {"action": "select",           "handle": "...", "value": "..."}
      {"action": "get_value",        "handle": "..."}
      {"action": "sleep",            "seconds": 0.15}
      {"action": "wait_for_element", "window_id": "...", "role": "...",
                                     "name": "...", "state": "...", "timeout": 5.0}
      {"action": "wait_for_window",  "pattern": "...", "timeout": 5.0}

    Returns a list of per-step results. Stops at first failure unless
    stop_on_error is False."""
    return _backend().run_sequence(steps, stop_on_error)


@mcp.tool()
def press_key(key: str, window_id: str | None = None) -> dict:
    """Inject a key press into the focused element or a specific window.
    key: 'Down', 'Up', 'Left', 'Right', 'Return', 'Escape', 'Tab',
         'Home', 'End', 'PageUp', 'PageDown', 'BackSpace', or any single character.
    Useful for navigating combo box dropdowns, dismissing dialogs, etc."""
    try:
        result = _backend().press_key(key, window_id)
        return {"ok": result.ok, "error": result.error}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
