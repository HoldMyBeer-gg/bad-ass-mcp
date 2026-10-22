from __future__ import annotations

# Trust model: this server runs over stdio and assumes the MCP client (e.g. Claude)
# is fully trusted. There is no authentication. Do not expose this server over a
# network socket or to untrusted processes — the AX/AT-SPI permissions it holds
# grant control over every application on the desktop.
import json
import platform
from collections import deque

from mcp.server.fastmcp import FastMCP

from .types import StaleHandleError

mcp = FastMCP("bad-ass-mcp")

# get_tree's JSON must fit the MCP client's per-tool-result cap (~25K tokens
# in Claude Code). Even after pruning empty wrappers, a content-heavy page can
# blow past that. Cap the *serialised* size and stop breadth-first, so the
# shallow/structural nodes (the useful ones) survive and deep leaves are cut
# first. The result then carries truncated=true and the dropped-node count so
# the caller knows to narrow with max_depth or find_elements.
#
# get_tree returns a COMPACT JSON string, not a dict. FastMCP serialises a
# returned dict with pydantic_core.to_json(..., indent=2) — that hardcoded
# indent inflates a compact tree ~2.4x, so a dict measured at 40 KB landed at
# 96 KB on the wire and overflowed anyway. Returning a pre-serialised compact
# string sidesteps the framework's indenting entirely, so the byte budget
# below is the size the client actually receives. ~40 KB is roughly 11K
# tokens: under the ~25K cap with headroom.
_TREE_BYTE_BUDGET = 40_000


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
    """List all visible application windows on the desktop.

    Fields per entry:
      id, name, pid, focused, minimized
      bounds: [x, y, width, height] in global screen coords (top-left
        origin), or null when the platform can't report it. Use this to
        translate screenshot pixels to click_at coordinates.
      accessible: false signals the platform a11y tree has nothing for
        this window — find_elements / get_tree will return empty. Skip
        those tools and use screenshot + click_at directly. Chromium-family
        windows (Electron/CEF) get an automatic wake poke first — their
        lazy a11y trees are enabled and re-probed before this is stamped
        false — so a false here means genuinely canvas-only: custom
        OpenGL/Vulkan surfaces, raw immediate-mode toolkits without an
        AccessKit adapter. (egui+accesskit surfaces as accessible=true.)"""
    return [w.__dict__ for w in _backend().list_windows()]


def _serialise_tree(root, byte_budget: int = _TREE_BYTE_BUDGET) -> dict:
    """Serialise a tree to nested dicts, breadth-first, within a byte budget.

    Every node's own fields are always emitted; children are attached
    breadth-first until the running JSON size would exceed byte_budget, then
    the walk stops. Shallow, structural nodes (the useful ones) survive; deep
    leaves are dropped first. When anything is cut, the root gains
    truncated=true and dropped_nodes so the caller can narrow the query.

    The root itself is always emitted whole, even if it alone exceeds the
    budget: a non-empty result beats an empty one.
    """

    def node_dict(el) -> dict:
        return {
            "id": el.id,
            "role": el.role,
            "name": el.name,
            "value": el.value,
            "states": sorted(el.states),
            "children": [],
        }

    # Per-node byte cost: the node's own JSON with no children, serialised the
    # SAME way get_tree emits the finished tree (compact, no spaces), so the
    # budget reflects the exact size the client receives. Cheap upper bound on
    # what attaching the node adds, so we never overshoot.
    def node_cost(d: dict) -> int:
        return len(json.dumps({**d, "children": []}, separators=(",", ":")))

    root_dict = node_dict(root)
    used = node_cost(root_dict)
    dropped = 0
    # Queue of (source_element, its_dict) whose children we still owe.
    queue: deque = deque([(root, root_dict)])

    while queue:
        src, dst = queue.popleft()
        for child in src.children:
            child_dict = node_dict(child)
            cost = node_cost(child_dict)
            if used + cost > byte_budget:
                # No room: this child and everything under it is dropped.
                dropped += 1 + _count_descendants(child)
                continue
            used += cost
            dst["children"].append(child_dict)
            queue.append((child, child_dict))

    if dropped:
        root_dict["truncated"] = True
        root_dict["dropped_nodes"] = dropped
    return root_dict


def _count_descendants(el) -> int:
    return sum(1 + _count_descendants(c) for c in el.children)


@mcp.tool()
def get_tree(window_id: str, max_depth: int | None = None) -> str:
    """Return the accessibility tree for a window as a compact JSON string.
    Use list_windows() first to get a window_id.

    Empty, nameless layout wrappers are pruned automatically, so the tree
    stays compact even on content-heavy pages while every named or
    interactive node is preserved. Very large pages are additionally capped
    to a byte budget so the result always fits the client: when that happens
    the root object carries "truncated": true and "dropped_nodes" (a count).
    To see the cut content, pass max_depth (e.g. 8) to cap recursion depth, or
    use find_elements() to target specific controls.

    Returned as a compact JSON string (not a nested object) so the client's
    result-size cap counts real content, not indentation whitespace; parse it
    with a JSON loader."""

    tree = _serialise_tree(_backend().get_tree(window_id, max_depth=max_depth))
    return json.dumps(tree, separators=(",", ":"))


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
def click(handle_id: str, button: str = "left", clicks: int = 1) -> dict:
    """Click / invoke an element. Get handle_id from find_elements() or get_tree().

    A plain left single click invokes the element's accessibility action
    (foreground-independent, does not steal focus). If the element exposes
    no actionable pattern, it falls back to a real pointer click at the
    element's center, so custom-drawn panels and tab strips that only
    highlight on a11y Select still get activated.

    button="right" opens context menus; clicks=2 is a real double-click
    (opens list rows / tree items whose a11y invoke only selects them).
    Both always use real pointer events at the element's center, so make
    sure the window is on top for those."""
    try:
        result = _backend().click(handle_id, button=button, clicks=clicks)
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
    """Select an option in a combo box or list by its visible text.

    Tries the a11y item list, then SetValue on the control, then a
    keystroke fallback (real click to focus, type the item text, Return).
    Each strategy is verified by reading the value back; a strategy that
    leaves the control showing something else counts as a failure, so
    ok=false means it really did not change. The keystroke fallback needs
    the window on top."""
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

    Caveat on Linux/KWin: per-window capture crops a full-screen shot to the
    target window's rectangle, because the KDE compositor won't let
    unprivileged clients capture individual windows. Any other window on top
    of the target will appear in the crop. Raise or close overlapping
    windows before capturing if isolation matters. Windows and macOS
    capture per-window at the compositor level and are unaffected.
    """
    import base64

    try:
        data = _backend().screenshot(window_id, output_path)
    except (ValueError, RuntimeError) as e:
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
def click_at(
    x: float,
    y: float,
    window_id: str | None = None,
    button: str = "left",
    clicks: int = 1,
) -> dict:
    """Click at absolute screen coordinates (top-left origin, in points).

    button: "left" (default), "right" (context menus) or "middle".
    clicks: 1 (default) or 2 for a real double-click.

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
        result = _backend().click_at(x, y, window_id, button=button, clicks=clicks)
        return {"ok": result.ok, "error": result.error}
    except NotImplementedError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def drag(
    from_x: float | None = None,
    from_y: float | None = None,
    to_x: float | None = None,
    to_y: float | None = None,
    from_handle: str | None = None,
    to_handle: str | None = None,
    button: str = "left",
    duration: float = 0.3,
) -> dict:
    """Drag-and-drop with real pointer events: press at the source, move to
    the target over `duration` seconds, release.

    Give either screen coordinates (from_x/from_y, to_x/to_y, top-left
    origin in points) or element handles (from_handle/to_handle, from
    find_elements or get_tree); a handle is resolved to its on-screen
    center. You can mix: a handle on one end and coordinates on the other.

    For drop targets that have no accessibility path — editors that only
    accept drops from another panel, list reordering, sliders. Delivery is
    system-global, so both ends must be visible and the window on top."""
    be = _backend()
    try:
        if from_handle:
            c = be._element_center(from_handle)
            if c is None:
                return {"ok": False, "error": "from_handle reports no screen bounds"}
            from_x, from_y = c
        if to_handle:
            c = be._element_center(to_handle)
            if c is None:
                return {"ok": False, "error": "to_handle reports no screen bounds"}
            to_x, to_y = c
    except StaleHandleError as e:
        return {"ok": False, "error": f"Stale handle: {e}"}
    if None in (from_x, from_y, to_x, to_y):
        return {
            "ok": False,
            "error": "Need from_x/from_y and to_x/to_y, or from_handle/to_handle",
        }
    try:
        result = be.drag(from_x, from_y, to_x, to_y, button=button, duration=duration)
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
    """Stop recording and export.

    output_path must end in .gif or .mp4. GIF is transcoded and downscaled
    to 900px wide at 12 fps, fine for a short clip in a README. MP4 keeps
    the full-resolution H.264 capture with no re-encode, so use it for
    anything longer than a minute or two (a 20-minute GIF is both huge and
    slow to build). Returns the path to the finished file."""
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
