"""
In-memory fake backend for contract tests.
Simulates a desktop with one window containing a small widget tree.
"""
from __future__ import annotations
import time
from bad_ass_mcp.backend.base import DesktopBackend
from bad_ass_mcp.types import WindowInfo, ElementHandle, ActionResult, StaleHandleError


FAKE_WINDOW_ID = "fake-window-1"

_TREE = ElementHandle(
    id="root",
    role="window",
    name="Fake App",
    children=[
        ElementHandle(id="btn-ok",     role="button",   name="OK",     states={"enabled"}),
        ElementHandle(id="btn-cancel", role="button",   name="Cancel", states={"enabled"}),
        ElementHandle(id="txt-name",   role="text",     name="Name",   value="", states={"enabled", "editable"}),
        ElementHandle(id="combo-size", role="combobox", name="Size",   value="Medium",
                      states={"enabled"},
                      children=[
                          ElementHandle(id="opt-small",  role="option", name="Small"),
                          ElementHandle(id="opt-medium", role="option", name="Medium"),
                          ElementHandle(id="opt-large",  role="option", name="Large"),
                      ]),
    ],
)


def _all_elements(node: ElementHandle) -> list[ElementHandle]:
    result = [node]
    for child in node.children:
        result.extend(_all_elements(child))
    return result


class FakeBackend(DesktopBackend):
    def __init__(self):
        self._stale: set[str] = set()
        self._values: dict[str, str] = {"txt-name": "", "combo-size": "Medium"}
        self._window_appears_at: float | None = None

    def _resolve(self, handle_id: str) -> ElementHandle:
        if handle_id in self._stale:
            raise StaleHandleError(f"{handle_id} is stale")
        for el in _all_elements(_TREE):
            if el.id == handle_id:
                return el
        raise KeyError(f"No element with id {handle_id!r}")

    def invalidate(self, handle_id: str):
        self._stale.add(handle_id)

    def schedule_window(self, delay: float = 0.05):
        self._window_appears_at = time.monotonic() + delay

    # ── DesktopBackend impl ──────────────────────────────────────────

    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(id=FAKE_WINDOW_ID, name="Fake App", pid=9999, focused=True)]

    def get_tree(self, window_id: str) -> ElementHandle:
        return _TREE

    def find_elements(self, window_id, *, role=None, name=None, index=0) -> list[ElementHandle]:
        results = [
            el for el in _all_elements(_TREE)
            if (role is None or el.role == role)
            and (name is None or el.name == name)
        ]
        return results

    def click(self, handle_id: str) -> ActionResult:
        self._resolve(handle_id)
        return ActionResult(ok=True)

    def type_text(self, handle_id: str, text: str) -> ActionResult:
        el = self._resolve(handle_id)
        if "editable" not in el.states:
            return ActionResult(ok=False, error=f"{handle_id} is not editable")
        self._values[handle_id] = text
        return ActionResult(ok=True)

    def select_option(self, handle_id: str, value: str) -> ActionResult:
        el = self._resolve(handle_id)
        options = [c.name for c in el.children]
        if value not in options:
            return ActionResult(ok=False, error=f"{value!r} not in {options}")
        self._values[handle_id] = value
        return ActionResult(ok=True)

    def get_value(self, handle_id: str) -> str | None:
        self._resolve(handle_id)
        return self._values.get(handle_id)

    def wait_for_window(self, title_pattern: str, timeout: float = 5.0) -> WindowInfo | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._window_appears_at and time.monotonic() >= self._window_appears_at:
                return WindowInfo(id="appeared-window", name=title_pattern, pid=1234, focused=False)
            time.sleep(0.01)
        return None

    def wait_for_element(self, window_id, *, role=None, name=None, state=None, timeout=5.0) -> ElementHandle | None:
        results = self.find_elements(window_id, role=role, name=name)
        if not results:
            return None
        el = results[0]
        if state is None or state in el.states:
            return el
        return None

    def screenshot(self, window_id=None) -> bytes:
        return b"\x89PNG\r\n\x1a\n"  # minimal PNG header stub
