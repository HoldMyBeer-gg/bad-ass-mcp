from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class WindowInfo:
    id: str
    name: str
    pid: int
    focused: bool


@dataclass
class ElementHandle:
    id: str
    role: str
    name: str
    value: str | None = None
    states: set[str] = field(default_factory=set)
    children: list[ElementHandle] = field(default_factory=list)


@dataclass
class ActionResult:
    ok: bool
    error: str | None = None


class StaleHandleError(Exception):
    """Raised when an element handle is no longer valid (widget gone or UI changed)."""
