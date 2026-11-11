"""Key-combination strings for xdotool. Kept free of gi/AT-SPI imports so
it can be tested anywhere."""

from __future__ import annotations

# xdotool keysym names for characters that aren't themselves valid names.
_XDO_SYMBOLS = {
    "+": "plus",
    "-": "minus",
    "=": "equal",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    "[": "bracketleft",
    "]": "bracketright",
    "`": "grave",
    " ": "space",
}


def xdotool_chord(mods: tuple[str, ...], key: str) -> str:
    """("ctrl", "shift"), "G" -> "ctrl+shift+g". Letters go lower-case so
    Shift is only pressed when asked for; named keys (Return, F4) pass
    through as X11 keysym names."""
    if len(key) == 1:
        key = _XDO_SYMBOLS.get(key, key.lower())
    return "+".join([*mods, key])
