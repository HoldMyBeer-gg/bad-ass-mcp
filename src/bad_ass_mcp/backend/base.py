from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from typing import Any

from ..types import ActionResult, BlockedByDialog, ElementHandle, StaleHandleError, WindowInfo

_MAX_SEQUENCE_STEPS = 500
_MAX_SEQUENCE_RUNTIME = 300.0  # seconds

# Roles that carry no semantic meaning of their own: pure layout/grouping
# containers. Chromium buries real content under long chains of these, which
# is why an unpruned get_tree on a busy page runs to ~1.9 MB, mostly empty
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
                if child.truncated:
                    # The cap cut inside a wrapper that's about to vanish:
                    # the mark moves up so the cut stays visible.
                    node.truncated = True
            else:
                kept.append(child)
        node.children = kept

    prune_children(root)
    return root


# When this process last injected real keyboard/mouse input (monotonic
# seconds). Backends call mark_injected() right where they send it (SendInput,
# keybd_event, CGEventPost, XTEST), so the BAM runner can tell its own input
# from a person's: anything newer than this didn't come from us.
_LAST_INJECTED = [0.0]


# Runs ffmpeg for _spawn_recorder on POSIX and ends it when the server is
# gone: a recorder that outlived a dead server kept capturing the whole
# desktop until its 30 minute bound. Arguments: the server pid, then the
# ffmpeg command. stdin is handed on explicitly, since a background job in
# a non-interactive sh gets /dev/null otherwise and the 'q' from
# stop_recording would never arrive. SIGTERM makes ffmpeg finish the file;
# a background job in sh ignores SIGINT.
_RECORDER_GUARD = """
parent=$1; shift
exec 3<&0
"$@" <&3 &
exec 3<&-
child=$!
while kill -0 "$parent" 2>/dev/null && kill -0 "$child" 2>/dev/null; do
  sleep 0.5
done
kill -0 "$child" 2>/dev/null && kill -TERM "$child" 2>/dev/null
wait "$child"
"""

_JOBS: list = []  # Windows job handles; a job ends its processes when its last handle closes


def _bind_to_job(proc) -> None:
    """Windows: put the recorder in a job object that kills its processes
    when the handle closes, which the OS does when this process dies. Best
    effort: an older Windows that refuses nested jobs leaves the recorder
    as before."""
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.CreateJobObjectW.restype = wintypes.HANDLE  # 64-bit; the default int truncates
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return
    info = EXTENDED_LIMITS()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        wintypes.HANDLE(job),
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ) and k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(proc._handle))
    if ok:
        _JOBS.append(job)
    else:
        k32.CloseHandle(wintypes.HANDLE(job))


def drive(steps: Generator[None, None, Any], pause: Callable[[], None]) -> Any:
    """Run a wait generator to its return value, calling pause() between
    its steps. See the waits section of DesktopBackend."""
    try:
        while True:
            next(steps)
            pause()
    except StopIteration as done:
        return done.value


def mark_injected() -> None:
    _LAST_INJECTED[0] = time.monotonic()


def last_injected_at() -> float:
    return _LAST_INJECTED[0]


# How long select_option waits for a changed value to show up in the
# accessibility tree before reporting a mismatch.
_SELECT_SETTLE_S = 1.0


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

    def _drag_ends(self, step: dict) -> tuple[float, float, float, float] | str:
        """Resolve a run_sequence drag step's endpoints: either end may be an
        element (`from_handle` / `to_handle`, its center) or coordinates; the
        end point may instead be `dx` / `dy` from the start, which is how a
        splitter is dragged a known distance. Returns an error string if the
        step can't be resolved."""
        if step.get("from_handle"):
            c = self._element_center(step["from_handle"])
            if c is None:
                return "from_handle reports no screen bounds"
            fx, fy = c
        elif step.get("from_x") is not None and step.get("from_y") is not None:
            fx, fy = float(step["from_x"]), float(step["from_y"])
        else:
            return "Need from_handle or from_x/from_y"
        if step.get("to_handle"):
            c = self._element_center(step["to_handle"])
            if c is None:
                return "to_handle reports no screen bounds"
            tx, ty = c
        elif step.get("to_x") is not None and step.get("to_y") is not None:
            tx, ty = float(step["to_x"]), float(step["to_y"])
        elif step.get("dx") is not None or step.get("dy") is not None:
            tx, ty = fx, fy
        else:
            return "Need to_handle, to_x/to_y, or dx/dy"
        tx += float(step.get("dx") or 0)
        ty += float(step.get("dy") or 0)
        return fx, fy, tx, ty

    def _click_element_at(
        self, handle_id: str, button: str = "left", clicks: int = 1
    ) -> ActionResult:
        """Coordinate fallback: resolve the element's center and send a real
        pointer click there. Shared by every backend's click()."""
        center = self._element_center(handle_id)
        if center is None:
            return ActionResult(
                ok=False,
                error=(
                    "Element reports no screen bounds"
                    if button != "left" or clicks != 1
                    else "No actionable a11y pattern and element reports no screen bounds"
                ),
            )
        return self.click_at(center[0], center[1], None, button=button, clicks=clicks)

    @abstractmethod
    def type_text(self, handle_id: str, text: str, paste: bool = False) -> ActionResult:
        """Set text value. Uses SetValue on text controls, falls back to key
        events. `paste` also allows typing through the clipboard: the text is
        lent to the clipboard (marked out of history) and cleared after if
        nothing replaced it. The user's clipboard is never read or restored,
        and password fields never use it."""

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
        silently fails; both happen on stock Win32 droplists."""
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
        # Web views (WKWebView, WebView2) update the control's a11y value a
        # beat after the change lands, so poll briefly before calling it a
        # mismatch.
        deadline = time.monotonic() + _SELECT_SETTLE_S
        while True:
            time.sleep(0.15)
            verified = self._option_selected(handle_id, value)
            if verified is not False or time.monotonic() >= deadline:
                break
        if verified is False:
            return ActionResult(
                ok=False,
                error=(f"Typed {value!r} but the control now reads {self.get_value(handle_id)!r}"),
            )
        return ActionResult(ok=True)

    def is_password(self, handle_id: str) -> bool | None:
        """Whether the element is a password field: True/False when the
        backend can tell, None when it can't. Backends override this with
        the platform's own flag; the fallback recognises a masked value."""
        try:
            value = self.get_value(handle_id)
        except Exception:
            return None
        if isinstance(value, str) and value and set(value) <= {"•", "●", "*"}:
            return True
        return None

    @abstractmethod
    def get_value(self, handle_id: str) -> str | None:
        """Return current text/value/state of an element."""

    # ── waits ───────────────────────────────────────────────────────────
    # Each wait is a generator that probes once per step and returns the hit,
    # or None once its deadline has passed. The sync wait_for_* methods run
    # one with time.sleep between steps; the MCP server runs the same
    # generator with an async sleep, so a long wait leaves the event loop
    # free for other calls and a client cancel ends it. Override the *_poll
    # generator, not the wait, to change how a platform probes.

    POLL_S = 0.1
    # How long a modal dialog must keep blocking a waited-on window before
    # the wait gives up and names it. A progress box that comes and goes
    # inside this doesn't end the wait.
    BLOCKED_GRACE_S = 3.0

    def _pause(self) -> None:
        time.sleep(self.POLL_S)

    def blocking_dialog(self, window_id: str) -> WindowInfo | None:
        """The modal dialog keeping window_id from taking input, or None.
        Backends that can't tell return None, and waits run to their
        timeout as before."""
        return None

    def window_poll(self, title_pattern: str, timeout: float) -> Generator[None, None, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            for w in self.list_windows():
                if w.matches(title_pattern):
                    return w
            if time.monotonic() >= deadline:
                return None
            yield

    def wait_for_window(self, title_pattern: str, timeout: float = 5.0) -> WindowInfo | None:
        """Poll until a window matching title_pattern appears. Returns None on timeout."""
        return drive(self.window_poll(title_pattern, timeout), self._pause)

    def element_poll(
        self,
        window_id: str,
        *,
        role: str | None = None,
        name: str | None = None,
        state: str | None = None,
        timeout: float = 5.0,
    ) -> Generator[None, None, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        blocked_since = None
        while True:
            for el in self.find_elements(window_id, role=role, name=name):
                if state is None or state in el.states:
                    return el
            dialog = self.blocking_dialog(window_id)
            if dialog is None:
                blocked_since = None
            else:
                now = time.monotonic()
                blocked_since = now if blocked_since is None else blocked_since
                if now - blocked_since >= self.BLOCKED_GRACE_S:
                    raise BlockedByDialog(dialog)
            if time.monotonic() >= deadline:
                return None
            yield

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
        return drive(
            self.element_poll(window_id, role=role, name=name, state=state, timeout=timeout),
            self._pause,
        )

    def text_poll(
        self,
        handle_id: str | None = None,
        text: str | None = None,
        *,
        pattern: str | None = None,
        contains: bool = False,
        timeout: float = 5.0,
        window_id: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> Generator[None, None, Any]:
        import re

        want = _nfc(text) if text is not None else None
        rx = re.compile(pattern) if pattern else None

        def matches(value: str) -> bool:
            got = _nfc(value)
            if rx is not None:
                return rx.search(got) is not None
            if want is None:
                return False
            return (want in got) if contains else (got == want)

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            for value in self._texts_of(handle_id, window_id, role, name):
                if matches(value):
                    return value
            if time.monotonic() >= deadline:
                return None
            yield

    def wait_for_text(
        self,
        handle_id: str | None = None,
        text: str | None = None,
        *,
        pattern: str | None = None,
        contains: bool = False,
        timeout: float = 5.0,
        window_id: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> str | None:
        """Poll until an element reads `text` (whitespace aside, Unicode
        compared in NFC), contains it, or matches the regex `pattern`
        (re.search, so "^Opened" means "starts with Opened"). Returns the
        text that matched, or None on timeout.

        Give a handle for a field whose value changes in place (a search
        box after type_text; read straight away a busy app still shows a
        prefix). Give window_id plus role/name for a label or status bar:
        egui replaces such a node when its text changes, and the old
        handle keeps the old text for good, so the element is re-found
        each poll. An element without a value is read by its name."""
        return drive(
            self.text_poll(
                handle_id,
                text,
                pattern=pattern,
                contains=contains,
                timeout=timeout,
                window_id=window_id,
                role=role,
                name=name,
            ),
            self._pause,
        )

    def _wait_for_text_error(self, step: dict, timeout: float) -> str:
        now = self._texts_of(
            step.get("handle"), step.get("window_id"), step.get("role"), step.get("name")
        )
        return _wait_for_text_error_text(step, timeout, now)

    def _texts_of(
        self, handle_id: str | None, window_id: str | None, role: str | None, name: str | None
    ) -> list[str]:
        """What wait_for_text compares: the handle's value, or the value (else
        name) of every element the locator finds right now."""
        if handle_id is not None:
            try:
                value = self.get_value(handle_id)
            except Exception:
                return []
            return [str(value)] if value is not None else []
        if window_id is None:
            return []
        out: list[str] = []
        try:
            found = self.find_elements(window_id, role=role, name=name)
        except Exception:
            return []
        for el in found:
            try:
                value = self.get_value(el.id)
            except Exception:
                value = None
            if value is None:
                value = el.name
            if value is not None:
                out.append(str(value))
        return out

    @abstractmethod
    def screenshot(self, window_id: str | None = None, output_path: str | None = None) -> bytes:
        """Capture PNG bytes of a window, or the full screen if window_id is None.

        If output_path is given, the PNG is written to disk and the returned
        bytes may be empty; the server layer returns the path instead.
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
        isn't available: webview content, custom-drawn UI, etc. Backends that
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
        seconds, release. Real pointer events, for drag-and-drop targets
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
    def _h264_output(video_path: str) -> list[str]:
        """The encode every recording ends with: H.264 in 4:2:0, which
        browsers and QuickTime play. Left alone, libx264 keeps the capture's
        chroma (4:4:4 from gdigrab, 4:2:2 from avfoundation) and the file
        shows only a poster in a browser. 4:2:0 needs even dimensions; every
        backend evens its crop.

        The file is written fragmented, with an empty moov up front and a
        fragment per keyframe, flushed to disk as written, and a keyframe is
        forced every two seconds: if the server dies mid-take, what is on
        disk still plays, and at most two seconds are lost. A plain MP4 gets its moov atom only when
        ffmpeg finishes, so a kill left nothing playable. _export_recording
        remuxes a clean stop into the ordinary seekable file."""
        return [
            "-t",
            "1800",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-flush_packets",
            "1",
            "-movflags",
            "+frag_keyframe+empty_moov",
            video_path,
        ]

    @staticmethod
    def _spawn_recorder(cmd: list[str], video_path: str) -> tuple:
        """Start an ffmpeg capture and fail fast if it dies immediately.

        ffmpeg's stderr goes to a log beside the capture file instead of
        DEVNULL, so a bad input device ("Input/output error" on a camera
        index, a missing display) surfaces as a RuntimeError here rather
        than as a missing-file error at stop_recording. Returns
        (proc, video_path, log_path) for the _recordings table."""
        import os
        import subprocess
        import time as _time

        if cmd and cmd[0] == "ffmpeg" and "-hide_banner" not in cmd:
            cmd = [cmd[0], "-hide_banner", *cmd[1:]]
        log_path = video_path + ".log"
        log = open(log_path, "wb")
        popen_kw: dict = {}
        if sys.platform != "win32":
            # The guard ends the recorder when this process is gone; its own
            # session makes the pair one process group for the kill path.
            cmd = ["sh", "-c", _RECORDER_GUARD, "bam-recorder", str(os.getpid()), *cmd]
            popen_kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, **popen_kw
            )
        except FileNotFoundError:
            log.close()
            raise RuntimeError("ffmpeg not found; install it to use start_recording")
        if sys.platform == "win32":
            _bind_to_job(proc)
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
    def _finish_recorder(entry: tuple, grace: float = 10.0) -> str:
        """Quit ffmpeg gracefully ('q' on stdin finishes the file), wait up
        to `grace` seconds, and return the capture path. Raises with
        ffmpeg's last stderr lines if the capture file never appeared."""
        import os
        import signal
        import subprocess

        proc, video_path = entry[0], entry[1]
        log_path = entry[2] if len(entry) > 2 else None
        pump = entry[3] if len(entry) > 3 else None
        try:
            if pump is not None:
                # Frames arrive on stdin, where a 'q' would land mid-frame;
                # the end of input finishes a piped capture.
                pump.stop()
                proc.stdin.close()
            else:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                # The guard and ffmpeg share a process group (their own
                # session), so this reaches ffmpeg, not only the guard.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
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
        """Finish a stopped recording: transcode to GIF, or for mp4 copy the
        H.264 stream into an ordinary file with the moov atom up front (no
        re-encode, so a half-hour session finishes in seconds and keeps full
        resolution). The capture itself is fragmented so a crash leaves it
        playable; without ffmpeg at hand it is moved into place as is."""
        import os
        import shutil
        import subprocess

        if fmt == "mp4":
            try:
                remux = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        video_path,
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        canonical,
                    ],
                    capture_output=True,
                )
                ok = remux.returncode == 0 and os.path.exists(canonical)
            except FileNotFoundError:
                ok = False
            if ok:
                try:
                    os.unlink(video_path)
                except OSError:
                    pass
            else:
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

    # ── foreground: who would receive real input ────────────────────
    # Real keys (no window_id), click_at and drag go to whatever is in front
    # or under the pointer, not to an element. The BAM runner checks these
    # before each such step. None means "can't tell on this platform", and
    # the runner then proceeds as before.

    def foreground_pid(self) -> int | None:
        """Process owning the foreground window (its top-level owner)."""
        return None

    def pid_at(self, x: float, y: float) -> int | None:
        """Process owning the top-level window under screen point (x, y)."""
        return None

    def app_hung(self, pid: int) -> bool | None:
        """Whether `pid`'s windows have stopped processing input (None: can't
        tell). Called from a watchdog thread while another call may be
        blocked on that app, so it must not go through the a11y API."""
        return None

    def idle_seconds(self) -> float | None:
        """Seconds since the last keyboard/mouse input of any kind (ours
        included). None: can't tell. Used to notice a person at the machine."""
        return None

    def bring_to_front(self, pid: int, *, gentle: bool = False) -> bool:
        """Raise one of `pid`'s windows and give it focus; True if it landed.
        `gentle`: no synthetic input (used to hand focus back to the user).
        Implementations record what they tried in `self.last_raise` as
        [{"method", "ok", "error"}] (no window names) for the run timeline."""
        self.last_raise = []
        return False

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
                                         or "from_handle"/"to_handle" (element
                                         centers), or "dx"/"dy" from the start
          {"action": "get_bounds",       "handle": "..."}   → value [x, y, w, h] or null
          {"action": "type",             "handle": "...", "text": "..."}
          {"action": "key",              "key": "Return", "window_id": null}
          {"action": "select",           "handle": "...", "value": "..."}
          {"action": "get_value",        "handle": "..."}
          {"action": "sleep",            "seconds": 0.1}
          {"action": "wait_for_element", "window_id": "...", "role": null,
                                         "name": null, "state": null, "timeout": 5.0}
          {"action": "wait_for_text",    "handle": "...", "text": "...", "timeout": 5.0,
                                         "contains": false, "pattern": null}  → value once
                                         it reads so; or "window_id" + "role"/"name" in
                                         place of "handle", re-found each poll (labels)
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
                    ends = self._drag_ends(step)
                    if isinstance(ends, str):
                        entry.update(ok=False, error=ends)
                    else:
                        r = self.drag(
                            *ends,
                            button=step.get("button", "left"),
                            duration=float(step.get("duration", 0.3)),
                        )
                        entry.update(ok=r.ok, error=r.error)
                elif action == "get_bounds":
                    b = self.element_bounds(step["handle"])
                    entry.update(ok=True, value=list(b) if b else None)
                elif action == "type":
                    r = self.type_text(
                        step["handle"], step["text"], paste=bool(step.get("paste", False))
                    )
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
                    timeout = float(step.get("timeout", 5.0))
                    el = self.wait_for_element(
                        step["window_id"],
                        role=step.get("role"),
                        name=step.get("name"),
                        state=step.get("state"),
                        timeout=timeout,
                    )
                    entry.update(ok=el is not None, handle=el.id if el else None)
                    if el is None:
                        wanted = ", ".join(
                            f"{k}={step[k]!r}" for k in ("role", "name", "state") if step.get(k)
                        )
                        entry["error"] = (
                            f"timed out after {timeout:g}s waiting for {wanted or 'an element'}"
                        )
                elif action == "wait_for_text":
                    timeout = float(step.get("timeout", 5.0))
                    contains = bool(step.get("contains", False))
                    value = self.wait_for_text(
                        step.get("handle"),
                        step.get("text"),
                        pattern=step.get("pattern"),
                        contains=contains,
                        timeout=timeout,
                        window_id=step.get("window_id"),
                        role=step.get("role"),
                        name=step.get("name"),
                    )
                    entry.update(ok=value is not None, value=value)
                    if value is None:
                        entry["error"] = self._wait_for_text_error(step, timeout)
                elif action == "wait_for_window":
                    timeout = float(step.get("timeout", 5.0))
                    w = self.wait_for_window(step["pattern"], timeout)
                    entry.update(ok=w is not None, window_id=w.id if w else None)
                    if w is None:
                        entry["error"] = (
                            f"timed out after {timeout:g}s waiting for a window "
                            f"matching {step['pattern']!r}"
                        )
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


# ── key combinations ("Cmd+A", "Ctrl+Shift+G") ───────────────────────

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "cmd": "cmd",
    "command": "cmd",
    "super": "super",
    "win": "super",
    "meta": "super",
}
_MODIFIER_ORDER = ("ctrl", "alt", "cmd", "shift", "super")


def parse_combo(key: str) -> tuple[tuple[str, ...], str] | None:
    """Split "Ctrl+Shift+G" into (("ctrl", "shift"), "G"); None for a plain
    key. "Ctrl++" targets the plus key. An unknown modifier raises
    ValueError instead of being typed as text."""
    if "+" not in key or key == "+":
        return None
    if key.endswith("++"):
        mods, main = key[:-2].split("+"), "+"
    else:
        *mods, main = key.split("+")
    if not main:
        raise ValueError(f"key combination {key!r} has no key after the modifiers")
    out: list[str] = []
    for m in mods:
        canon = _MODIFIER_ALIASES.get(m.strip().lower())
        if canon is None:
            raise ValueError(
                f"unknown modifier {m!r} in {key!r}; use Ctrl, Shift, Alt/Option, "
                "Cmd (Command on macOS, Ctrl elsewhere) or Super/Win"
            )
        if canon not in out:
            out.append(canon)
    return tuple(out), main


def platform_modifiers(mods: tuple[str, ...], platform: str) -> tuple[str, ...]:
    """Map portable modifiers to the platform's own: "cmd" is the primary
    shortcut modifier (Command on macOS, Ctrl elsewhere), so "Cmd+A" means
    select-all everywhere; "super" is the Windows/Super key (Command on
    macOS). Order is canonical and duplicates collapse."""
    if platform == "macos":
        mapped = {"cmd" if m == "super" else m for m in mods}
    else:
        mapped = {"ctrl" if m == "cmd" else m for m in mods}
    return tuple(m for m in _MODIFIER_ORDER if m in mapped)


def _wait_for_text_error_text(step: dict, timeout: float, now: list[str]) -> str:
    want = step.get("pattern") or step.get("text")
    verb = "match" if step.get("pattern") else ("contain" if step.get("contains") else "read")
    if step.get("handle") is not None:
        reads = f"it reads {now[0] if now else ''!r}"
    elif now:
        reads = "found " + ", ".join(repr(v[:60]) for v in now[:3])
    else:
        reads = "no element matched the locator"
    return f"timed out after {timeout:g}s waiting for the field to {verb} {want!r}; {reads}"


def _nfc(value: str) -> str:
    """Trimmed, NFC-normalised text, so "é" typed as e + combining acute
    matches "é" stored as one code point."""
    import unicodedata

    return unicodedata.normalize("NFC", value).strip()
