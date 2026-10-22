from __future__ import annotations

import subprocess
import time
import uuid
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo  # noqa: E402
from .base import DesktopBackend, prune_tree  # noqa: E402


class LinuxBackend(DesktopBackend):
    # How long to wait for a freshly-woken webview to register with AT-SPI.
    _WAKE_TIMEOUT = 2.0
    # Chromium nests real content deep (its web area can sit 13+ levels down
    # from the app root, deeper still with GPU-composited pages). A shallow
    # cap returns a hollow husk of empty groups; here it's usually reached via
    # the app frame so it bit less than on macOS, but the cap was still
    # fragile. 60 clears real trees; _WALK_MAX_NODES bounds a runaway/cyclic
    # walk regardless of depth.
    _WALK_MAX_DEPTH = 60
    _WALK_MAX_NODES = 20000
    # Process-name/cmdline markers for toolkits that build their a11y tree
    # lazily and watch the screen-reader flag: Chromium family (Electron,
    # CEF, browsers) and WebKitGTK (Tauri on Linux).
    _WEBVIEW_MARKERS = (
        "electron",
        "chromium",
        "chrome",
        "libcef",
        "cef",
        "msedge",
        "brave",
        "vivaldi",
        "opera",
        "webkit",
        "tauri",
    )

    def __init__(self):
        self._handles: dict[str, Any] = {}  # handle_id → live Atspi.Accessible
        self._woken_pids: set[int] = set()  # PIDs we've already tried to wake
        self._sr_flag_done = False  # screen-reader flag poke attempted
        self._recordings: dict[str, tuple[Any, str]] = {}
        # Waking is proactive on Linux: set the screen-reader flag at
        # server start so Chromium/Electron apps launched afterwards see
        # it from birth. Flipping it after launch did not populate an
        # already-running Vivaldi's tree in live testing (Vivaldi ships
        # accessibility off and ignored the flag entirely; it needs
        # --force-renderer-accessibility), so apps already running may
        # stay asleep; _atspi_list_windows detects those and stamps them
        # accessible=False.
        self._ensure_screen_reader_flag()

    # ── Internal helpers ──────────────────────────────────────────────

    def _register(self, node: Any) -> str:
        """Assign a stable handle ID to a live AT-SPI node."""
        h = str(uuid.uuid4())
        self._handles[h] = node
        return h

    def _resolve(self, handle_id: str) -> Any:
        node = self._handles.get(handle_id)
        if node is None:
            raise StaleHandleError(f"Unknown handle: {handle_id!r}")
        try:
            # Accessing the role will throw if the underlying object is gone
            node.get_role()
        except Exception:
            del self._handles[handle_id]
            raise StaleHandleError(f"Handle {handle_id!r} is stale (widget gone)")
        return node

    def _to_handle(self, node: Any) -> ElementHandle:
        handle_id = self._register(node)
        role = node.get_role_name() or "unknown"
        name = node.get_name() or ""
        value = None
        try:
            text = node.query_text()
            value = text.get_text(0, -1)
        except Exception:
            pass
        states = set()
        try:
            ss = node.get_state_set()
            for state_name in (
                "enabled",
                "focused",
                "visible",
                "checked",
                "editable",
                "selected",
                "expanded",
                "active",
            ):
                attr = getattr(Atspi.StateType, state_name.upper(), None)
                if attr is not None and ss.contains(attr):
                    states.add(state_name)
        except Exception:
            pass
        return ElementHandle(id=handle_id, role=role, name=name, value=value, states=states)

    def _walk(
        self,
        node: Any,
        depth: int = 0,
        max_depth: int = _WALK_MAX_DEPTH,
        budget: list[int] | None = None,
    ) -> ElementHandle:
        # budget is a shared 1-element list so the node cap spans the whole
        # recursion, not each branch.
        if budget is None:
            budget = [self._WALK_MAX_NODES]
        handle = self._to_handle(node)
        budget[0] -= 1
        if depth < max_depth and budget[0] > 0:
            try:
                for i in range(node.get_child_count()):
                    if budget[0] <= 0:
                        break
                    child = node.get_child_at_index(i)
                    handle.children.append(self._walk(child, depth + 1, max_depth, budget))
            except Exception:
                pass
        return handle

    def _find_app(self, window_id: str) -> Any | None:
        """window_id is either a PID str or an app name."""
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            try:
                if str(app.get_process_id()) == window_id:
                    return app
                if app.get_name() == window_id:
                    return app
            except Exception:
                continue
        return None

    def _pid_smells_webview(self, pid: int) -> bool:
        import os

        blob = ""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                blob = f.read().decode(errors="replace").lower()
        except Exception:
            pass
        try:
            blob += " " + os.path.realpath(f"/proc/{pid}/exe").lower()
        except Exception:
            pass
        return any(m in blob for m in self._WEBVIEW_MARKERS)

    def _ensure_screen_reader_flag(self) -> bool:
        """Turn on org.a11y.Status.ScreenReaderEnabled on the session bus.

        Chromium-family apps and WebKitGTK build their AT-SPI tree lazily and
        only materialise it when this flag says an assistive tech is
        listening. Returns True the first time the flag transitions to on —
        i.e. when a re-probe wait is worthwhile. Already-on (apps registered
        at launch, or will as soon as they see it) and D-Bus-unavailable both
        return False.
        """
        if self._sr_flag_done:
            return False
        self._sr_flag_done = True
        try:
            from gi.repository import Gio, GLib  # noqa: PLC0415

            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            current = bus.call_sync(
                "org.a11y.Bus",
                "/org/a11y/bus",
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.a11y.Status", "ScreenReaderEnabled")),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            if current and current.unpack()[0]:
                return False
            bus.call_sync(
                "org.a11y.Bus",
                "/org/a11y/bus",
                "org.freedesktop.DBus.Properties",
                "Set",
                GLib.Variant(
                    "(ssv)",
                    ("org.a11y.Status", "ScreenReaderEnabled", GLib.Variant("b", True)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return True
        except Exception:
            return False

    def _wake_webview_a11y(self, pid: int) -> bool:
        """Try to wake a lazy webview a11y tree. True → caller should re-probe."""
        if pid in self._woken_pids or not self._pid_smells_webview(pid):
            return False
        self._woken_pids.add(pid)
        return self._ensure_screen_reader_flag()

    def _toolkit_is_chromium(self, app: Any) -> bool:
        """Ask AT-SPI for the app's toolkit: flatpak-proof, unlike /proc.

        Sandboxed apps (flatpak Vivaldi et al.) report the sandbox helper's
        PID to AT-SPI, so _pid_smells_webview reads the wrong process's
        cmdline. The toolkit name comes from the app itself over the a11y
        bus and says "Chromium" regardless of packaging.
        """
        try:
            return (app.get_toolkit_name() or "").lower() in ("chromium", "chrome")
        except Exception:
            return False

    def _find_app_waking(self, window_id: str) -> Any | None:
        """_find_app, but pokes lazy webviews awake before giving up."""
        app = self._find_app(window_id)
        if app is not None:
            return app
        try:
            pid = int(window_id)
        except ValueError:
            return None
        if not self._wake_webview_a11y(pid):
            return None
        deadline = time.monotonic() + self._WAKE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.25)
            app = self._find_app(window_id)
            if app is not None:
                return app
        return None

    def _search(self, node: Any, role: str | None, name: str | None) -> list[Any]:
        results = []
        try:
            node_role = node.get_role_name()
            node_name = node.get_name() or ""
            if (role is None or node_role == role) and (name is None or node_name == name):
                results.append(node)
            for i in range(node.get_child_count()):
                results.extend(self._search(node.get_child_at_index(i), role, name))
        except Exception:
            pass
        return results

    # ── DesktopBackend impl ───────────────────────────────────────────

    def _atspi_list_windows(self) -> list[WindowInfo]:
        desktop = Atspi.get_desktop(0)
        windows = []
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            try:
                name = app.get_name() or ""
                pid = app.get_process_id()
                focused = False
                child_count = app.get_child_count()
                # All frames iconified → window is minimized; capture/click won't
                # work until something is restored.
                minimized = child_count > 0
                # No frames means AT-SPI knows the app exists but exposes no
                # tree to walk; same UX gap as the X11-fallback case.
                accessible = child_count > 0
                bounds: tuple[int, int, int, int] | None = None
                frames_have_content = False
                for j in range(child_count):
                    frame = app.get_child_at_index(j)
                    ss = frame.get_state_set()
                    if ss.contains(Atspi.StateType.ACTIVE):
                        focused = True
                    if not ss.contains(Atspi.StateType.ICONIFIED):
                        minimized = False
                    try:
                        # Asleep Chromium advertises a phantom child: the
                        # count says 1 but fetching it returns None. Only a
                        # fetchable child counts as content.
                        if frame.get_child_count() > 0 and frame.get_child_at_index(0) is not None:
                            frames_have_content = True
                    except Exception:
                        pass
                    if bounds is None:
                        try:
                            ext = frame.get_extents(Atspi.CoordType.SCREEN)
                            if ext is not None and ext.width > 0 and ext.height > 0:
                                bounds = (ext.x, ext.y, ext.width, ext.height)
                        except Exception:
                            pass
                # A Chromium window whose frame has no fetchable children
                # has not enabled accessibility. Either it launched before
                # the screen-reader flag went up, or the browser is holding
                # the tree behind a user consent prompt (Vivaldi does this).
                # Report it inaccessible so callers use screenshot+click_at
                # instead of trusting a hollow frame.
                if accessible and not frames_have_content and self._toolkit_is_chromium(app):
                    accessible = False
                windows.append(
                    WindowInfo(
                        id=str(pid),
                        name=name,
                        pid=pid,
                        focused=focused,
                        minimized=minimized,
                        bounds=bounds,
                        accessible=accessible,
                    )
                )
            except Exception:
                continue
        return windows

    def _x11_list_windows(self) -> list[WindowInfo]:
        """Discover top-level X11 windows for processes not in the AT-SPI tree.

        Uses _NET_CLIENT_LIST (window manager's client list — excludes panels,
        tooltips, and other unmanaged windows) so the result stays tidy.
        Covers Tauri / Electron / Qt apps that don't register with AT-SPI.
        """
        import re

        try:
            raw = subprocess.check_output(
                ["xprop", "-root", "_NET_CLIENT_LIST"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode()
            wids = re.findall(r"0x[0-9a-fA-F]+", raw)
        except Exception:
            return []

        seen_pids: set[int] = set()
        results = []
        for wid in wids:
            try:
                pid = int(
                    subprocess.check_output(
                        ["xdotool", "getwindowpid", wid],
                        stderr=subprocess.DEVNULL,
                        timeout=1,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                continue
            if pid in seen_pids:
                continue
            try:
                geom_out = subprocess.check_output(
                    ["xdotool", "getwindowgeometry", wid],
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                ).decode()
                w = h = 0
                px = py = 0
                for line in geom_out.splitlines():
                    if "Geometry:" in line:
                        dims = line.split(":")[1].strip()
                        w, h = map(int, dims.split("x"))
                    elif "Position:" in line:
                        # "Position: 100,200 (screen: 0)"
                        pos = line.split(":")[1].strip().split(" ")[0]
                        px, py = map(int, pos.split(","))
                if w < 50 or h < 50:
                    continue
                name = (
                    subprocess.check_output(
                        ["xdotool", "getwindowname", wid],
                        stderr=subprocess.DEVNULL,
                        timeout=1,
                    )
                    .decode()
                    .strip()
                )
                if not name:
                    continue
                seen_pids.add(pid)
                results.append(
                    WindowInfo(
                        id=str(pid),
                        name=name,
                        pid=pid,
                        focused=False,
                        bounds=(px, py, w, h),
                        accessible=False,
                    )
                )
            except Exception:
                continue
        return results

    def list_windows(self) -> list[WindowInfo]:
        atspi = self._atspi_list_windows()
        atspi_pids = {w.pid for w in atspi}
        # Supplement with top-level X11 windows whose process isn't in AT-SPI
        extra = [w for w in self._x11_list_windows() if w.pid not in atspi_pids]
        # Webview processes build their a11y tree lazily — poke the
        # screen-reader flag and give them a moment to register, then fold
        # any newcomers into the AT-SPI side instead of stamping them
        # accessible=false forever.
        fresh = [
            w for w in extra if w.pid not in self._woken_pids and self._pid_smells_webview(w.pid)
        ]
        if fresh:
            self._woken_pids.update(w.pid for w in fresh)
            if self._ensure_screen_reader_flag():
                waiting = {w.pid for w in fresh}
                deadline = time.monotonic() + self._WAKE_TIMEOUT
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    atspi = self._atspi_list_windows()
                    atspi_pids = {w.pid for w in atspi}
                    if waiting <= atspi_pids:
                        break
                extra = [w for w in extra if w.pid not in atspi_pids]
        return atspi + extra

    def get_tree(self, window_id: str, *, max_depth: int | None = None) -> ElementHandle:
        app = self._find_app_waking(window_id)
        if app is None:
            raise ValueError(f"No window found for id {window_id!r}")
        depth_cap = self._WALK_MAX_DEPTH if max_depth is None else max_depth
        return prune_tree(self._walk(app, max_depth=depth_cap))

    def find_elements(
        self, window_id: str, *, role=None, name=None, index=0
    ) -> list[ElementHandle]:
        app = self._find_app_waking(window_id)
        if app is None:
            return []
        nodes = self._search(app, role, name)
        return [self._to_handle(n) for n in nodes]

    _XDO_BUTTONS = {"left": "1", "middle": "2", "right": "3"}

    @staticmethod
    def _xdotool(*args: str) -> ActionResult:
        try:
            subprocess.run(["xdotool", *args], capture_output=True, check=True)
            return ActionResult(ok=True)
        except FileNotFoundError:
            return ActionResult(
                ok=False, error="xdotool not found; install it to use pointer tools"
            )
        except subprocess.CalledProcessError as e:
            return ActionResult(
                ok=False, error=e.stderr.decode(errors="replace") or f"xdotool {args[0]} failed"
            )

    def click_at(
        self,
        x: float,
        y: float,
        window_id: str | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> ActionResult:
        btn = self._XDO_BUTTONS.get(button)
        if btn is None:
            return ActionResult(ok=False, error=f"Unknown button {button!r}")
        if clicks < 1:
            return ActionResult(ok=False, error="clicks must be >= 1")
        ix, iy = int(round(x)), int(round(y))
        r = self._xdotool("mousemove", "--sync", str(ix), str(iy))
        if not r.ok:
            return r
        args = ["click", "--clearmodifiers"]
        if clicks > 1:
            args += ["--repeat", str(clicks), "--delay", "80"]
        r = self._xdotool(*args, btn)
        if r.ok:
            time.sleep(0.05)
        return r

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        button: str = "left",
        duration: float = 0.3,
    ) -> ActionResult:
        btn = self._XDO_BUTTONS.get(button)
        if btn is None:
            return ActionResult(ok=False, error=f"Unknown button {button!r}")
        steps = max(8, int(duration / 0.015))
        r = self._xdotool("mousemove", "--sync", str(int(round(from_x))), str(int(round(from_y))))
        if not r.ok:
            return r
        time.sleep(0.05)
        r = self._xdotool("mousedown", btn)
        if not r.ok:
            return r
        time.sleep(0.05)
        # Toolkits arm a drag only after the pointer travels while held, and
        # drop targets want a stream of motion events, so interpolate.
        for i in range(1, steps + 1):
            t = i / steps
            px = int(round(from_x + (to_x - from_x) * t))
            py = int(round(from_y + (to_y - from_y) * t))
            r = self._xdotool("mousemove", "--sync", str(px), str(py))
            if not r.ok:
                self._xdotool("mouseup", btn)
                return r
            time.sleep(max(duration, 0.1) / steps)
        time.sleep(0.05)
        r = self._xdotool("mouseup", btn)
        if r.ok:
            time.sleep(0.05)
        return r

    def element_bounds(self, handle_id: str) -> tuple[float, float, float, float] | None:
        node = self._resolve(handle_id)
        try:
            ext = node.query_component().get_extents(Atspi.CoordType.SCREEN)
        except Exception:
            return None
        if ext is None or ext.width <= 0 or ext.height <= 0:
            return None
        return (float(ext.x), float(ext.y), float(ext.width), float(ext.height))

    def click(self, handle_id: str, button: str = "left", clicks: int = 1) -> ActionResult:
        node = self._resolve(handle_id)
        if button != "left" or clicks != 1:
            # AT-SPI actions are single primary activations; go physical.
            return self._click_element_at(handle_id, button=button, clicks=clicks)
        try:
            n = node.get_n_actions()
        except Exception:
            n = 0
        if n <= 0:
            # No action interface (custom-drawn widgets): click the center.
            return self._click_element_at(handle_id)
        try:
            # Prefer "click" > "press" > action[0]
            names = [node.get_action_name(i) for i in range(n)]
            idx = next((i for i, a in enumerate(names) if a in ("click", "press")), 0)
            node.do_action(idx)
            time.sleep(0.15)  # let async UI insertions settle before next tool call
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=f"Cannot click: {e}")

    def type_text(self, handle_id: str, text: str) -> ActionResult:
        node = self._resolve(handle_id)
        try:
            # Try SetValue first (foreground-independent)
            val_iface = node.query_value()
            val_iface.set_current_value(float(text))
            return ActionResult(ok=True)
        except Exception:
            pass
        try:
            # EditableText interface (foreground-independent)
            et = node.query_editable_text()
            et.set_text_contents(text)
            return ActionResult(ok=True)
        except Exception:
            pass
        try:
            # Try xdotool type --window first: delivers text directly to the target
            # window without stealing focus (background-safe).
            pid = node.get_process_id()
            xwid = self._find_xwid(pid) if pid else None
            if xwid:
                subprocess.run(
                    [
                        "xdotool",
                        "type",
                        "--window",
                        xwid,
                        "--clearmodifiers",
                        "--delay",
                        "10",
                        text,
                    ],
                    capture_output=True,
                    check=True,
                )
                return ActionResult(ok=True)
        except FileNotFoundError:
            pass  # xdotool not available — fall through to AT-SPI
        except Exception:
            pass  # xwid lookup failed or xdotool error — fall through
        try:
            # AT-SPI keyboard injection as last resort. generate_keyboard_event
            # uses XTest under the hood so it goes to the X11-focused window.
            # Grab focus on the node so injection lands in the right place. After
            # each chunk drain the GLib queue to prevent D-Bus backpressure from
            # CodeMirror DOM mutations.
            from gi.repository import GLib  # noqa: PLC0415

            ctx = GLib.main_context_default()

            if getattr(self, "_focused_handle", None) != handle_id:
                node.grab_focus()
                time.sleep(0.05)
                self._focused_handle = handle_id

            for i, chunk in enumerate(text.split("\n")):
                if i > 0:
                    Atspi.generate_keyboard_event(0xFF0D, None, Atspi.KeySynthType.SYM)
                    while ctx.iteration(may_block=False):
                        pass
                if chunk:
                    Atspi.generate_keyboard_event(0, chunk, Atspi.KeySynthType.STRING)
                    while ctx.iteration(may_block=False):
                        pass
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=f"Cannot set text: {e}")

    def select_option(self, handle_id: str, value: str) -> ActionResult:
        node = self._resolve(handle_id)
        attempts: list[str] = []
        # Walk children looking for a matching option and select it
        nodes = self._search(node, role="menu item", name=value)
        if not nodes:
            nodes = self._search(node, role="option", name=value)
        if nodes:
            try:
                nodes[0].do_action(0)
                time.sleep(0.1)
                if self._option_selected(handle_id, value) is not False:
                    return ActionResult(ok=True)
                attempts.append("item activated but value did not change")
            except Exception as e:
                attempts.append(f"do_action: {e}")
        else:
            attempts.append("no matching item in AT-SPI subtree")
        # Last resort: focus with a real click, type the item, Return.
        r = self._select_option_by_keystrokes(handle_id, value)
        if r.ok:
            return r
        attempts.append(f"keystrokes: {r.error}")
        return ActionResult(ok=False, error=f"Option {value!r}: " + "; ".join(attempts))

    def get_value(self, handle_id: str) -> str | None:
        node = self._resolve(handle_id)
        try:
            return node.query_text().get_text(0, -1)
        except Exception:
            pass
        try:
            return str(node.query_value().get_current_value())
        except Exception:
            pass
        return node.get_name() or None

    def wait_for_window(self, title_pattern: str, timeout: float = 5.0) -> WindowInfo | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in self.list_windows():
                if title_pattern.lower() in w.name.lower():
                    return w
            time.sleep(0.1)
        return None

    def wait_for_element(
        self, window_id, *, role=None, name=None, state=None, timeout=5.0
    ) -> ElementHandle | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            results = self.find_elements(window_id, role=role, name=name)
            for el in results:
                if state is None or state in el.states:
                    return el
            time.sleep(0.1)
        return None

    def _display_backend(self) -> str:
        """Return 'x11', 'kwin', or 'other'.

        Wayland changed how screenshots work: apps can't grab pixels through the
        old X11 root-window path, so `import -window root` returns a black frame
        or fails outright. KWin (KDE Plasma / SteamOS gaming-mode's desktop)
        exposes ScreenShot2 over DBus and ships `spectacle` as its CLI front-end.
        Other Wayland compositors (GNOME, sway, Hyprland) need per-compositor
        paths that aren't implemented yet.
        """
        import os

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
        if session_type == "x11" or (not wayland_display and session_type != "wayland"):
            return "x11"
        # Wayland session — probe for KWin. qdbus is cheap and the KWin object
        # is only present when the KWin compositor is running.
        try:
            r = subprocess.run(
                ["qdbus", "org.kde.KWin", "/KWin"],
                capture_output=True,
                timeout=1.0,
            )
            if r.returncode == 0:
                return "kwin"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "other"

    def _capture_full_screen(self, target: str) -> None:
        """Write a full-screen PNG to `target`. Raises RuntimeError with stderr on failure."""
        import os

        backend = self._display_backend()
        if backend == "kwin":
            # Spectacle ships with KDE Plasma; -bnf = background, no notify, fullscreen.
            r = subprocess.run(
                ["spectacle", "-b", "-n", "-f", "-o", target],
                capture_output=True,
            )
            if r.returncode != 0 or not os.path.exists(target):
                raise RuntimeError(
                    "spectacle capture failed: "
                    + (r.stderr.decode(errors="replace").strip() or "no output written")
                )
            return
        if backend == "x11":
            r = subprocess.run(
                ["import", "-window", "root", target],
                capture_output=True,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    "ImageMagick `import` failed: "
                    + (r.stderr.decode(errors="replace").strip() or "unknown error")
                )
            return
        raise RuntimeError(
            "Screenshot is not supported on this Wayland compositor yet — "
            "only X11 and KWin (KDE Plasma) are implemented. "
            f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')!r}"
        )

    def screenshot(self, window_id: str | None = None, output_path: str | None = None) -> bytes:
        import os
        import tempfile

        if output_path:
            target = os.path.realpath(os.path.expanduser(output_path))
            cleanup = False
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                target = f.name
            cleanup = True
        try:
            geom = None
            if window_id:
                app = self._find_app(window_id)
                if app:
                    try:
                        child_count = app.get_child_count()
                        if child_count > 0 and all(
                            app.get_child_at_index(j)
                            .get_state_set()
                            .contains(Atspi.StateType.ICONIFIED)
                            for j in range(child_count)
                        ):
                            raise ValueError(
                                f"Window for {window_id!r} is minimized — "
                                "restore it before capturing"
                            )
                    except ValueError:
                        raise
                    except Exception:
                        pass
                # Resolve geometry — works for AT-SPI apps and non-AX apps
                # (Tauri/Electron) via the xdotool PID fallback in _window_geometry.
                geom = self._window_geometry(window_id)
                if geom is None:
                    raise ValueError(
                        f"Window {window_id!r} not found — pass None for a full-desktop capture"
                    )

            backend = self._display_backend()

            if not geom:
                # Full-screen capture path.
                self._capture_full_screen(target)
            elif backend == "x11":
                # X11 window-scoped capture: prefer capturing by xwid (handles
                # decorations); fall back to a geometry crop from root.
                xwid = None
                app = self._find_app(window_id) if window_id else None
                pid = app.get_process_id() if app else None
                if pid is None and window_id:
                    try:
                        pid = int(window_id)
                    except ValueError:
                        pass
                if pid:
                    xwid = self._find_xwid(pid)
                if xwid:
                    r = subprocess.run(
                        ["import", "-window", xwid, target],
                        capture_output=True,
                    )
                    if r.returncode != 0:
                        raise RuntimeError(
                            "ImageMagick `import` failed: "
                            + (r.stderr.decode(errors="replace").strip() or "unknown error")
                        )
                else:
                    x, y, w, h = geom
                    r = subprocess.run(
                        [
                            "import",
                            "-window",
                            "root",
                            "-crop",
                            f"{w}x{h}+{x}+{y}",
                            "+repage",
                            target,
                        ],
                        capture_output=True,
                    )
                    if r.returncode != 0:
                        raise RuntimeError(
                            "ImageMagick `import` failed: "
                            + (r.stderr.decode(errors="replace").strip() or "unknown error")
                        )
            else:
                # KWin (or fallback): spectacle can't scope by PID, so shoot
                # the full screen and crop to `geom` with Pillow. Pillow is
                # preferred over ImageMagick because it's a single Python dep
                # instead of a system binary that may not be installed
                # (SteamOS ships without ImageMagick, for instance).
                try:
                    from PIL import Image
                except ImportError as e:
                    raise RuntimeError(
                        "Pillow is required for per-window screenshots on KWin — "
                        "install with `pip install Pillow`"
                    ) from e
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fs:
                    full = fs.name
                try:
                    self._capture_full_screen(full)
                    x, y, w, h = geom
                    with Image.open(full) as img:
                        # Assumes the captured image and AT-SPI geometry share
                        # the same coord system. True for 1x displays (Steam
                        # Deck docked at 1080p). On HiDPI Wayland the captured
                        # PNG may be at physical resolution while AT-SPI
                        # reports logical — the crop will land on the wrong
                        # region. Fix when we have a HiDPI test rig.
                        img.crop((x, y, x + w, y + h)).save(target, "PNG")
                finally:
                    try:
                        os.unlink(full)
                    except OSError:
                        pass

            if output_path:
                return b""
            with open(target, "rb") as f:
                return f.read()
        finally:
            if cleanup:
                try:
                    os.unlink(target)
                except OSError:
                    pass

    def _find_xwid(self, pid: int) -> str | None:
        """Return the X11 window ID of the largest visible window for a PID."""
        try:
            xwids = (
                subprocess.check_output(
                    ["xdotool", "search", "--pid", str(pid)],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .split()
            )
            best: tuple[str, int] | None = None
            for wid in xwids:
                out = subprocess.check_output(
                    ["xdotool", "getwindowgeometry", wid],
                    stderr=subprocess.DEVNULL,
                ).decode()
                for line in out.splitlines():
                    if "Geometry:" in line:
                        dims = line.split(":")[1].strip()
                        w, h = map(int, dims.split("x"))
                        if w > 100 and h > 100 and (best is None or w * h > best[1]):
                            best = (wid, w * h)
            return best[0] if best else None
        except Exception:
            return None

    def _window_geometry(self, window_id: str) -> tuple[int, int, int, int] | None:
        """Return (x, y, width, height) using xdotool, falling back to AT-SPI.

        Handles non-AT-SPI apps (Tauri, Electron, Qt) by treating window_id as
        a raw PID when the AT-SPI lookup returns nothing.
        """
        # Try xdotool first — reliable across toolkits and HiDPI setups
        app = self._find_app(window_id)
        pid = app.get_process_id() if app else None
        # Non-AT-SPI fallback: interpret window_id directly as a PID
        if pid is None:
            try:
                pid = int(window_id)
            except ValueError:
                pass
        if pid:
            try:
                wids = (
                    subprocess.check_output(
                        ["xdotool", "search", "--pid", str(pid)],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .split()
                )
                best = None
                for wid in wids:
                    out = subprocess.check_output(
                        ["xdotool", "getwindowgeometry", wid],
                        stderr=subprocess.DEVNULL,
                    ).decode()
                    pos, size = None, None
                    for line in out.splitlines():
                        if "Position:" in line:
                            coords = line.split(":")[1].strip().split()[0]
                            x, y = map(int, coords.split(","))
                            pos = (x, y)
                        if "Geometry:" in line:
                            dims = line.split(":")[1].strip()
                            w, h = map(int, dims.split("x"))
                            size = (w, h)
                    if pos and size and size[0] > 100 and size[1] > 100:
                        if best is None or size[0] * size[1] > best[2] * best[3]:
                            best = (wid, pos[0], pos[1], size[0], size[1])
                if best:
                    wid, x, y, w, h = best
                    # Expand by window decoration extents (_NET_FRAME_EXTENTS: l,r,t,b)
                    try:
                        raw = subprocess.check_output(
                            ["xprop", "-id", wid, "_NET_FRAME_EXTENTS"],
                            stderr=subprocess.DEVNULL,
                        ).decode()
                        nums = [int(n) for n in raw.split("=")[1].split(",")]
                        fl, fr, ft, fb = nums
                        x -= fl
                        y -= ft
                        w += fl + fr
                        h += ft + fb
                    except Exception:
                        pass
                    return (x, y, w, h)
            except Exception:
                pass
        # Fall back to AT-SPI frame extents. Match _atspi_list_windows —
        # calling get_extents() directly on the frame is more reliable than
        # going through get_component(), which returns None on some
        # native-Wayland windows (Firefox on GTK-Wayland, notably) and takes
        # the whole fallback down with an AttributeError.
        if app:
            for i in range(app.get_child_count()):
                try:
                    frame = app.get_child_at_index(i)
                    ext = frame.get_extents(Atspi.CoordType.SCREEN)
                    if ext is not None and ext.width > 0 and ext.height > 0:
                        return (ext.x, ext.y, ext.width, ext.height)
                except Exception:
                    continue
        return None

    def start_recording(self, window_id: str | None = None, fps: int = 15) -> str:
        import os
        import tempfile

        handle = str(uuid.uuid4())
        video_path = os.path.join(tempfile.gettempdir(), f"bam-rec-{handle}.mp4")

        display = os.environ.get("DISPLAY", ":0")
        if window_id:
            geom = self._window_geometry(window_id)
        else:
            geom = None

        if geom:
            x, y, w, h = geom
            # Clamp to screen bounds so ffmpeg doesn't reject out-of-screen areas
            try:
                raw = subprocess.check_output(
                    ["xdpyinfo", "-display", display],
                    stderr=subprocess.DEVNULL,
                ).decode()
                for line in raw.splitlines():
                    if "dimensions:" in line:
                        dims = line.split(":")[1].strip().split()[0]
                        sw, sh = map(int, dims.split("x"))
                        x = max(0, min(x, sw - 1))
                        y = max(0, min(y, sh - 1))
                        w = min(w, sw - x)
                        h = min(h, sh - y)
                        break
            except Exception:
                pass
            # ffmpeg requires even dimensions
            w -= w % 2
            h -= h % 2
            grab = f"{w}x{h}"
            offset = f"{display}+{x},{y}"
        else:
            grab = "1920x1080"
            offset = f"{display}+0,0"

        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-video_size",
                grab,
                "-framerate",
                str(fps),
                "-f",
                "x11grab",
                "-i",
                offset,
                "-t",
                "1800",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                video_path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._recordings[handle] = (proc, video_path)
        return handle

    def stop_recording(self, handle: str, output_path: str) -> str:
        canonical, fmt = self._recording_target(output_path)

        if handle not in self._recordings:
            raise ValueError(f"No active recording with handle {handle!r}")

        proc, video_path = self._recordings.pop(handle)
        # Send 'q' to ffmpeg's stdin — graceful quit writes the moov atom.
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

        return self._export_recording(video_path, canonical, fmt)

    _KEY_SYMS = {
        "Return": 0xFF0D,
        "Enter": 0xFF0D,
        "Escape": 0xFF1B,
        "Tab": 0xFF09,
        "Up": 0xFF52,
        "Down": 0xFF54,
        "Left": 0xFF51,
        "Right": 0xFF53,
        "Home": 0xFF50,
        "End": 0xFF57,
        "PageUp": 0xFF55,
        "PageDown": 0xFF56,
        "Space": 0x0020,
        "BackSpace": 0xFF08,
        "Delete": 0xFFFF,
    }

    def press_key(self, key: str, window_id: str | None = None) -> ActionResult:
        if window_id:
            app = self._find_app(window_id)
            if app:
                try:
                    xwid = self._find_xwid(app.get_process_id())
                    if xwid:
                        # Pass the key name directly — xdotool speaks X11 keysym names
                        # natively (Return, F1, ctrl+a, etc.). The _KEY_SYMS filter is
                        # only for the AT-SPI fallback path below.
                        try:
                            subprocess.run(
                                [
                                    "xdotool",
                                    "key",
                                    "--window",
                                    xwid,
                                    "--clearmodifiers",
                                    key,
                                ],
                                capture_output=True,
                                check=True,
                            )
                            return ActionResult(ok=True)
                        except FileNotFoundError:
                            pass  # xdotool not available — fall through to AT-SPI
                except Exception:
                    pass
        try:
            from gi.repository import GLib  # noqa: PLC0415

            sym = self._KEY_SYMS.get(key)
            if sym:
                Atspi.generate_keyboard_event(sym, None, Atspi.KeySynthType.SYM)
            else:
                Atspi.generate_keyboard_event(0, key[:1], Atspi.KeySynthType.STRING)
            ctx = GLib.main_context_default()
            while ctx.iteration(may_block=False):
                pass
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))
