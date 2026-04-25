from __future__ import annotations

import subprocess
import time
import uuid
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo  # noqa: E402
from .base import DesktopBackend  # noqa: E402


class LinuxBackend(DesktopBackend):
    def __init__(self):
        self._handles: dict[str, Any] = {}  # handle_id → live Atspi.Accessible

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

    def _walk(self, node: Any, depth: int = 0, max_depth: int = 12) -> ElementHandle:
        handle = self._to_handle(node)
        if depth < max_depth:
            try:
                for i in range(node.get_child_count()):
                    child = node.get_child_at_index(i)
                    handle.children.append(self._walk(child, depth + 1, max_depth))
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

    def list_windows(self) -> list[WindowInfo]:
        desktop = Atspi.get_desktop(0)
        windows = []
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            try:
                name = app.get_name() or ""
                pid = app.get_process_id()
                # Check if any child frame has focus
                focused = False
                for j in range(app.get_child_count()):
                    frame = app.get_child_at_index(j)
                    ss = frame.get_state_set()
                    if ss.contains(Atspi.StateType.ACTIVE):
                        focused = True
                        break
                windows.append(WindowInfo(id=str(pid), name=name, pid=pid, focused=focused))
            except Exception:
                continue
        return windows

    def get_tree(self, window_id: str) -> ElementHandle:
        app = self._find_app(window_id)
        if app is None:
            raise ValueError(f"No window found for id {window_id!r}")
        return self._walk(app)

    def find_elements(
        self, window_id: str, *, role=None, name=None, index=0
    ) -> list[ElementHandle]:
        app = self._find_app(window_id)
        if app is None:
            return []
        nodes = self._search(app, role, name)
        return [self._to_handle(n) for n in nodes]

    def click(self, handle_id: str) -> ActionResult:
        node = self._resolve(handle_id)
        try:
            n = node.get_n_actions()
            # Prefer "click" > "press" > action[0]
            names = [node.get_action_name(i) for i in range(n)]
            idx = next((i for i, a in enumerate(names) if a in ("click", "press")), 0)
            node.do_action(idx)
            time.sleep(0.15)  # let async UI insertions settle before next tool call
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

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
            # AT-SPI keyboard injection. generate_keyboard_event uses XTest under
            # the hood so it goes to the X11-focused window. We activate the target
            # window with xdotool (focus management only, not typing) so injection
            # lands in the right place. After each chunk, drain the GLib queue to
            # prevent AT-SPI D-Bus backpressure from CodeMirror DOM mutations.
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
        # Walk children looking for a matching option and select it
        nodes = self._search(node, role="menu item", name=value)
        if not nodes:
            nodes = self._search(node, role="option", name=value)
        if not nodes:
            return ActionResult(ok=False, error=f"Option {value!r} not found")
        try:
            nodes[0].do_action(0)
            return ActionResult(ok=True)
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

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

    def screenshot(self, window_id: str | None = None) -> bytes:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            if window_id:
                app = self._find_app(window_id)
                if app:
                    try:
                        for _i in range(app.get_child_count()):
                            break
                    except Exception:
                        pass
            subprocess.run(["scrot", path], check=True, capture_output=True)
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return b""
        finally:
            try:
                os.unlink(path)
            except Exception:
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
        """Return (x, y, width, height) using xdotool, falling back to AT-SPI."""
        # Try xdotool first — reliable across toolkits and HiDPI setups
        app = self._find_app(window_id)
        pid = app.get_process_id() if app else None
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
        # Fall back to AT-SPI component interface
        if app:
            try:
                for i in range(app.get_child_count()):
                    frame = app.get_child_at_index(i)
                    comp = frame.get_component()
                    ext = comp.get_extents(Atspi.CoordType.SCREEN)
                    if ext.width > 0 and ext.height > 0:
                        return (ext.x, ext.y, ext.width, ext.height)
            except Exception:
                pass
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
        self._recordings: dict  # ensure attr exists
        if not hasattr(self, "_recordings"):
            self._recordings = {}
        self._recordings[handle] = (proc, video_path)
        return handle

    def stop_recording(self, handle: str, output_path: str) -> str:
        import os

        canonical = os.path.realpath(os.path.expanduser(output_path))
        if not canonical.lower().endswith(".gif"):
            raise ValueError(f"output_path must end with .gif (got {output_path!r})")

        if not hasattr(self, "_recordings") or handle not in self._recordings:
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

        # Convert to GIF using ffmpeg palette trick for quality
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
                        subprocess.run(
                            ["xdotool", "windowactivate", "--sync", xwid],
                            capture_output=True,
                        )
                        time.sleep(0.1)
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
