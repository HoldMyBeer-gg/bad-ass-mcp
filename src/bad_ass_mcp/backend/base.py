from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import ActionResult, ElementHandle, WindowInfo


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
