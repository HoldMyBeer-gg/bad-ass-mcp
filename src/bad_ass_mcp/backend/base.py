from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo

_MAX_SEQUENCE_STEPS = 500
_MAX_SEQUENCE_RUNTIME = 300.0  # seconds

# Roles that carry no semantic meaning of their own: pure layout/grouping
# containers. Chromium buries real content under long chains of these, which
# is why an unpruned get_tree on a busy page runs to ~1.9 MB — mostly empty
# AXGroups. A wrapper is prunable only if it is one of these roles AND has no
# name, no value, and no interactive/meaningful state; its children graft up
# to its parent so nothing real is lost.
_STRUCTURAL_ROLES = frozenset(
    {
        "group",
        "genericcontainer",
        "generic",
        "pane",
        "panel",
        "section",
        "filler",
        "unknown",
        "",
    }
)
# States that make a node worth keeping even when it is otherwise an empty,
# nameless structural wrapper (it is focusable/checkable/etc., so a caller
# might target it).
_MEANINGFUL_STATES = frozenset(
    {"focused", "selected", "checked", "editable", "expanded", "collapsed"}
)


def _is_noise_wrapper(handle: "ElementHandle") -> bool:
    """True when this node is a pure layout wrapper safe to collapse away.

    Structural role, no name, no value, and no meaningful state. Such nodes
    exist only to nest; dropping them and lifting their children preserves
    every named or interactive descendant while cutting the empty-group
    chains Chromium generates.
    """
    if (handle.role or "").lower() not in _STRUCTURAL_ROLES:
        return False
    if (handle.name or "").strip():
        return False
    if handle.value not in (None, ""):
        return False
    if handle.states & _MEANINGFUL_STATES:
        return False
    return True


def prune_tree(root: "ElementHandle") -> "ElementHandle":
    """Collapse noise wrappers in-place, grafting their children upward.

    The root is never dropped (callers rely on it as the window anchor).
    Applied bottom-up so a chain of empty groups collapses fully in one pass.
    """

    def prune_children(node: "ElementHandle") -> None:
        kept: list[ElementHandle] = []
        for child in node.children:
            prune_children(child)
            if _is_noise_wrapper(child):
                kept.extend(child.children)  # graft grandchildren up
            else:
                kept.append(child)
        node.children = kept

    prune_children(root)
    return root


class DesktopBackend(ABC):
    @abstractmethod
    def list_windows(self) -> list[WindowInfo]:
        """Return all visible application windows."""

    @abstractmethod
    def get_tree(self, window_id: str, *, max_depth: int | None = None) -> ElementHandle:
        """Return the accessibility tree rooted at the given window.

        Noise wrappers (empty, nameless layout groups) are pruned so the
        result stays compact on content-heavy pages; every named or
        interactive node is preserved. max_depth caps recursion depth
        (None uses the backend default, which clears real Chromium nesting).
        """

    @abstractmethod
    def find_elements(
        self,
        window_id: str,
        *,
        role: str | None = None,
        name: str | None = None,
        index: int = 0,
    ) -> list[ElementHandle]:
        """Find elements by role and/or name. Returns all matches unless index is set."""

    @abstractmethod
    def click(self, handle_id: str, button: str = "left", clicks: int = 1) -> ActionResult:
        """Invoke the element's primary action. Foreground-independent where possible.

        For button != "left" or clicks > 1 there is no a11y action to invoke, so
        implementations go straight to element_bounds() + click_at(). A plain
        left click should also fall back to that path when the element exposes
        no actionable pattern (custom-drawn panels, tab strips that only
        highlight on Select, list rows that need a real double-click)."""

    def element_bounds(self, handle_id: str) -> tuple[float, float, float, float] | None:
        """Return (x, y, width, height) of an element in global screen coords.

        None when the platform cannot report it. Used for the coordinate
        fallbacks in click / select_option / drag."""
        return None

    def _element_center(self, handle_id: str) -> tuple[float, float] | None:
        b = self.element_bounds(handle_id)
        if not b or b[2] <= 0 or b[3] <= 0:
            return None
        return (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)

    def _click_element_at(
        self, handle_id: str, button: str = "left", clicks: int = 1
    ) -> ActionResult:
        """Coordinate fallback: resolve the element's center and send a real
        pointer click there. Shared by every backend's click()."""
        center = self._element_center(handle_id)
        if center is None:
            return ActionResult(
                ok=False,
                error="No actionable a11y pattern and element reports no screen bounds",
            )
        return self.click_at(center[0], center[1], None, button=button, clicks=clicks)

    @abstractmethod
    def type_text(self, handle_id: str, text: str) -> ActionResult:
        """Set text value. Uses SetValue on text controls, falls back to key events."""

    @abstractmethod
    def select_option(self, handle_id: str, value: str) -> ActionResult:
        """Select an option in a combo box / list by visible text or value.

        Implementations must verify the selection took effect when the
        control's value is readable, and report failure rather than returning
        ok after a silent no-op. _select_option_by_keystrokes() is the shared
        last-resort strategy."""

    def _option_selected(self, handle_id: str, value: str) -> bool | None:
        """True/False when the element's current value can be read and
        compared to `value` (case-insensitive, trimmed); None when the
        control exposes no readable value so the result cannot be checked."""
        try:
            current = self.get_value(handle_id)
        except Exception:
            return None
        if current is None or not str(current).strip():
            return None
        return str(current).strip().lower() == value.strip().lower()

    def _select_option_by_keystrokes(self, handle_id: str, value: str) -> ActionResult:
        """Last-resort combo-box strategy: real click on the control to focus
        it, type the item text (Win32/Qt/Cocoa combos do incremental search
        on typed characters), press Return to commit, then read the value
        back. Used when the a11y item search finds nothing and SetValue
        silently fails — both happen on stock Win32 droplists."""
        center = self._element_center(handle_id)
        if center is None:
            return ActionResult(ok=False, error="Element reports no screen bounds")
        r = self.click_at(center[0], center[1], None)
        if not r.ok:
            return r
        time.sleep(0.15)
        for ch in value:
            r = self.press_key(ch, None)
            if not r.ok:
                return r
            time.sleep(0.02)
        time.sleep(0.1)
        r = self.press_key("Return", None)
        if not r.ok:
            return r
        time.sleep(0.15)
        verified = self._option_selected(handle_id, value)
        if verified is False:
            return ActionResult(
                ok=False,
                error=(f"Typed {value!r} but the control now reads {self.get_value(handle_id)!r}"),
            )
        return ActionResult(ok=True)

    @abstractmethod
    def get_value(self, handle_id: str) -> str | None:
        """Return current text/value/state of an element."""

    @abstractmethod
    def wait_for_window(self, title_pattern: str, timeout: float = 5.0) -> WindowInfo | None:
        """Poll until a window matching title_pattern appears. Returns None on timeout."""

    @abstractmethod
    def wait_for_element(
        self,
        window_id: str,
        *,
        role: str | None = None,
        name: str | None = None,
        state: str | None = None,
        timeout: float = 5.0,
    ) -> ElementHandle | None:
        """Poll until a matching element exists and optionally has the given state."""

    @abstractmethod
    def screenshot(self, window_id: str | None = None, output_path: str | None = None) -> bytes:
        """Capture PNG bytes of a window, or the full screen if window_id is None.

        If output_path is given, the PNG is written to disk and the returned
        bytes may be empty — the server layer returns the path instead.
        Implementations should raise ValueError if window_id is given but the
        target window cannot be located (rather than falling back to a full
        desktop capture)."""

    def click_at(
        self,
        x: float,
        y: float,
        window_id: str | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> ActionResult:
        """Click at absolute screen coordinates.

        Foreground-independent fallback for when accessibility-based clicking
        isn't available — webview content, custom-drawn UI, etc. Backends that
        can scope events to a specific process should use window_id to do so.

        button: "left" | "right" | "middle". clicks: 1 or 2 (double-click)."""
        raise NotImplementedError("click_at is not implemented for this platform yet")

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        button: str = "left",
        duration: float = 0.3,
    ) -> ActionResult:
        """Press at (from_x, from_y), move to (to_x, to_y) over `duration`
        seconds, release. Real pointer events — for drag-and-drop targets
        that have no a11y path (editors that only accept drops from another
        panel, canvas reorder, sliders)."""
        raise NotImplementedError("drag is not implemented for this platform yet")

    @staticmethod
    def _recording_target(output_path: str) -> tuple[str, str]:
        """Validate a stop_recording output path. Returns (canonical, fmt)
        where fmt is "gif" or "mp4". The path is realpath'd first so a
        symlink can't smuggle a different extension through."""
        import os

        canonical = os.path.realpath(os.path.expanduser(output_path))
        lower = canonical.lower()
        if lower.endswith(".gif"):
            return canonical, "gif"
        if lower.endswith(".mp4"):
            return canonical, "mp4"
        raise ValueError(f"output_path must end with .gif or .mp4 (got {output_path!r})")

    @staticmethod
    def _spawn_recorder(cmd: list[str], video_path: str) -> tuple:
        """Start an ffmpeg capture and fail fast if it dies immediately.

        ffmpeg's stderr goes to a log beside the capture file instead of
        DEVNULL, so a bad input device ("Input/output error" on a camera
        index, a missing display) surfaces as a RuntimeError here rather
        than as a missing-file error at stop_recording. Returns
        (proc, video_path, log_path) for the _recordings table."""
        import subprocess
        import time as _time

        if cmd and cmd[0] == "ffmpeg" and "-hide_banner" not in cmd:
            cmd = [cmd[0], "-hide_banner", *cmd[1:]]
        log_path = video_path + ".log"
        log = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
            )
        except FileNotFoundError:
            log.close()
            raise RuntimeError("ffmpeg not found; install it to use start_recording")
        # Give ffmpeg long enough to open the device and reject it.
        deadline = _time.monotonic() + 1.5
        while _time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            _time.sleep(0.1)
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(
                f"ffmpeg exited with {proc.returncode} before recording started: "
                + DesktopBackend._log_tail(log_path)
            )
        return (proc, video_path, log_path)

    @staticmethod
    def _log_tail(log_path: str, lines: int = 6) -> str:
        import os

        try:
            with open(log_path, "rb") as f:
                text = f.read().decode(errors="replace")
        except OSError:
            return "(no ffmpeg output captured)"
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass
        kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
        return " | ".join(kept) or "(ffmpeg wrote nothing)"

    @staticmethod
    def _finish_recorder(entry: tuple) -> str:
        """Quit ffmpeg gracefully ('q' on stdin writes the moov atom), wait,
        and return the capture path. Raises with ffmpeg's last stderr lines
        if the capture file never appeared."""
        import os
        import subprocess

        proc, video_path = entry[0], entry[1]
        log_path = entry[2] if len(entry) > 2 else None
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
        if not os.path.exists(video_path):
            detail = DesktopBackend._log_tail(log_path) if log_path else "(no log)"
            raise RuntimeError(f"ffmpeg produced no capture file: {detail}")
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass
        return video_path

    @staticmethod
    def _export_recording(video_path: str, canonical: str, fmt: str) -> str:
        """Finish a stopped recording: transcode to GIF, or move the already
        H.264-encoded capture into place for mp4 (no re-encode, so a
        half-hour session finishes instantly and keeps full resolution)."""
        import os
        import shutil
        import subprocess

        if fmt == "mp4":
            shutil.move(video_path, canonical)
            return canonical
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

    @abstractmethod
    def start_recording(self, window_id: str | None = None, fps: int = 15) -> str:
        """Start screen recording. Returns a recording handle."""

    @abstractmethod
    def stop_recording(self, handle: str, output_path: str) -> str:
        """Stop recording and export. output_path ends in .gif (transcoded,
        downscaled) or .mp4 (the raw H.264 capture, moved into place).
        Returns the output path."""

    @abstractmethod
    def press_key(self, key: str, window_id: str | None = None) -> ActionResult:
        """Inject a key press. key is a name like 'Down', 'Up', 'Return', 'Escape',
        'Tab', 'Left', 'Right', or a single character. Grabs focus if window_id given."""

    # ── Composite helpers (implemented once, inherited by all backends) ──

    def learn_layout(
        self,
        window_id: str,
        descriptors: dict[str, dict],
    ) -> dict[str, str | None]:
        """Resolve semantic names to live handle IDs for the current session.

        descriptors: {"label": {"role": "button", "name": "OK"}, ...}
        Returns:     {"label": "uuid-...", ...}  (None when not found)

        Store the returned map and pass handle IDs directly to run_sequence
        to skip find_elements on every action.
        """
        result: dict[str, str | None] = {}
        for label, desc in descriptors.items():
            elements = self.find_elements(
                window_id,
                role=desc.get("role"),
                name=desc.get("name"),
            )
            result[label] = elements[0].id if elements else None
        return result

    def run_sequence(
        self,
        steps: list[dict],
        stop_on_error: bool = True,
    ) -> list[dict]:
        """Execute a list of actions server-side in one call.

        Supported actions:
          {"action": "click",            "handle": "...", "button": "left", "clicks": 1}
          {"action": "click_at",         "x": 100.0, "y": 200.0, "window_id": null,
                                         "button": "left", "clicks": 1}
          {"action": "drag",             "from_x": 0, "from_y": 0, "to_x": 0, "to_y": 0,
                                         "button": "left", "duration": 0.3}
          {"action": "type",             "handle": "...", "text": "..."}
          {"action": "key",              "key": "Return", "window_id": null}
          {"action": "select",           "handle": "...", "value": "..."}
          {"action": "get_value",        "handle": "..."}
          {"action": "sleep",            "seconds": 0.1}
          {"action": "wait_for_element", "window_id": "...", "role": null,
                                         "name": null, "state": null, "timeout": 5.0}
          {"action": "wait_for_window",  "pattern": "...", "timeout": 5.0}

        Returns a list of per-step result dicts with at least {step, action, ok}.
        Aborts on the first failure when stop_on_error is True.
        """
        if len(steps) > _MAX_SEQUENCE_STEPS:
            print(
                f"[bad-ass-mcp] run_sequence: received {len(steps)} steps; "
                f"truncating to {_MAX_SEQUENCE_STEPS}",
                file=sys.stderr,
            )
            steps = steps[:_MAX_SEQUENCE_STEPS]

        deadline = time.monotonic() + _MAX_SEQUENCE_RUNTIME
        results: list[dict] = []
        for i, step in enumerate(steps):
            action = step.get("action", "")
            entry: dict = {"step": i, "action": action, "ok": False}

            if time.monotonic() > deadline:
                print(
                    f"[bad-ass-mcp] run_sequence: wall-clock limit ({_MAX_SEQUENCE_RUNTIME}s) "
                    f"hit at step {i}; aborting",
                    file=sys.stderr,
                )
                entry["error"] = "sequence runtime limit exceeded"
                results.append(entry)
                break

            try:
                if action == "click":
                    r = self.click(
                        step["handle"],
                        button=step.get("button", "left"),
                        clicks=int(step.get("clicks", 1)),
                    )
                    entry.update(ok=r.ok, error=r.error)
                elif action == "click_at":
                    r = self.click_at(
                        float(step["x"]),
                        float(step["y"]),
                        step.get("window_id"),
                        button=step.get("button", "left"),
                        clicks=int(step.get("clicks", 1)),
                    )
                    entry.update(ok=r.ok, error=r.error)
                elif action == "drag":
                    r = self.drag(
                        float(step["from_x"]),
                        float(step["from_y"]),
                        float(step["to_x"]),
                        float(step["to_y"]),
                        button=step.get("button", "left"),
                        duration=float(step.get("duration", 0.3)),
                    )
                    entry.update(ok=r.ok, error=r.error)
                elif action == "type":
                    r = self.type_text(step["handle"], step["text"])
                    entry.update(ok=r.ok, error=r.error)
                elif action == "key":
                    r = self.press_key(step["key"], step.get("window_id"))
                    entry.update(ok=r.ok, error=r.error)
                elif action == "select":
                    r = self.select_option(step["handle"], step["value"])
                    entry.update(ok=r.ok, error=r.error)
                elif action == "get_value":
                    val = self.get_value(step["handle"])
                    entry.update(ok=True, value=val)
                elif action == "sleep":
                    time.sleep(float(step.get("seconds", 0.1)))
                    entry["ok"] = True
                elif action == "wait_for_element":
                    el = self.wait_for_element(
                        step["window_id"],
                        role=step.get("role"),
                        name=step.get("name"),
                        state=step.get("state"),
                        timeout=float(step.get("timeout", 5.0)),
                    )
                    entry.update(ok=el is not None, handle=el.id if el else None)
                elif action == "wait_for_window":
                    w = self.wait_for_window(step["pattern"], float(step.get("timeout", 5.0)))
                    entry.update(ok=w is not None, window_id=w.id if w else None)
                else:
                    entry.update(ok=False, error=f"Unknown action: {action!r}")
            except StaleHandleError as e:
                entry.update(ok=False, error=f"Stale handle: {e}")
            except Exception as e:
                entry.update(ok=False, error=str(e))
            results.append(entry)
            if not entry["ok"] and stop_on_error:
                break
        return results
