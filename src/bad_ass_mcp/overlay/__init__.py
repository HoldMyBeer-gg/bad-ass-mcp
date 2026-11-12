"""On-screen annotations for BAM runs (`annotate`).

The overlay is its own process: a click-through, never-focused, top-most
window over every screen that reads JSON lines on stdin and draws each
item for a moment before it fades:

    {"box": [x, y, w, h], "label": "1  click ok"}
    {"arrow": [fx, fy, tx, ty], "label": "2  drag split"}
    {"text": [x, y], "label": "3  key ctrl+s"}

An item may add "hold" (seconds on screen) and "kind": "fail" (drawn red).
Coordinates are the backend's screen coordinates: physical pixels on
Windows, points on macOS. The overlay exits when stdin closes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path


class Overlay:
    """Feeds one overlay process. Sending never raises: a dead overlay
    costs the run its annotations, never the run itself."""

    def __init__(self, cmd: list[str], *, ready_timeout: float | None = None):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if ready_timeout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if ready_timeout else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=flags,
        )
        if ready_timeout:
            self._wait_ready(ready_timeout)

    def _wait_ready(self, timeout: float) -> None:
        line: list[str] = []
        t = threading.Thread(target=lambda: line.append(self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        if line and line[0].strip() == "ready":
            return
        self.proc.kill()
        try:
            _, err = self.proc.communicate(timeout=2)
        except Exception:
            err = ""
        tail = (err or "").strip().splitlines()[-1:] or ["no window within %gs" % timeout]
        raise RuntimeError(f"the annotation overlay didn't start: {tail[0]}")

    def send(self, item: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(item) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def start_overlay(platform: str) -> Overlay:
    """Start the overlay for this desktop, or raise RuntimeError saying why
    there can't be one."""
    if platform == "linux":
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError(
                "annotations aren't supported on a Wayland session: "
                "an app can't place a click-through window over others there"
            )
        raise RuntimeError("annotations aren't supported on Linux yet")
    if platform not in ("windows", "macos"):
        raise RuntimeError(f"annotations aren't supported on {platform}")
    # By file, not -m: the script is self-contained, and this way the child
    # runs the same copy as the runner whatever the install looks like.
    cmd = [sys.executable, str(Path(__file__).with_name(f"{platform}.py"))]
    return Overlay(cmd, ready_timeout=10)
