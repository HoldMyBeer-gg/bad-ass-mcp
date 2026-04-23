from .base import DesktopBackend
from ..types import WindowInfo, ElementHandle, ActionResult


class MacOSBackend(DesktopBackend):
    """macOS AXUIElement backend — not yet implemented."""

    def _nyi(self):
        raise NotImplementedError("macOS backend not yet implemented")

    def list_windows(self) -> list[WindowInfo]: self._nyi()
    def get_tree(self, window_id): self._nyi()
    def find_elements(self, window_id, *, role=None, name=None, index=0): self._nyi()
    def click(self, handle_id): self._nyi()
    def type_text(self, handle_id, text): self._nyi()
    def select_option(self, handle_id, value): self._nyi()
    def get_value(self, handle_id): self._nyi()
    def wait_for_window(self, title_pattern, timeout=5.0): self._nyi()
    def wait_for_element(self, window_id, *, role=None, name=None, state=None, timeout=5.0): self._nyi()
    def screenshot(self, window_id=None): self._nyi()
