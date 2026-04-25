from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod

from ..types import ActionResult, ElementHandle, StaleHandleError, WindowInfo

_MAX_SEQUENCE_STEPS = 500
_MAX_SEQUENCE_RUNTIME = 300.0  # seconds


class DesktopBackend(ABC):
    @abstractmethod
    def list_windows(self) -> list[WindowInfo]:
        """Return all visible application windows."""

    @abstractmethod
    def get_tree(self, window_id: str) -> ElementHandle:
        """Return full accessibility tree rooted at the given window."""

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
    def click(self, handle_id: str) -> ActionResult:
        """Invoke the element's primary action. Foreground-independent where possible."""

    @abstractmethod
    def type_text(self, handle_id: str, text: str) -> ActionResult:
        """Set text value. Uses SetValue on text controls, falls back to key events."""

    @abstractmethod
    def select_option(self, handle_id: str, value: str) -> ActionResult:
        """Select an option in a combo box / list by visible text or value."""

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
    def screenshot(self, window_id: str | None = None) -> bytes:
        """Capture PNG bytes of a window, or the full screen if window_id is None."""

    @abstractmethod
    def start_recording(self, window_id: str | None = None, fps: int = 15) -> str:
        """Start screen recording. Returns a recording handle."""

    @abstractmethod
    def stop_recording(self, handle: str, output_path: str) -> str:
        """Stop recording and export as GIF. Returns the output path."""

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
          {"action": "click",            "handle": "..."}
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
                    r = self.click(step["handle"])
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
