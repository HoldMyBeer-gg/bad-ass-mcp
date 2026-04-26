# bad-ass-mcp

*Every other tool needs a browser or a vision model. This one reads what the OS already knows.*

A cross-platform MCP server for desktop GUI automation — driven by accessibility APIs, not screenshots.

Works with Claude, Codex, Gemini, Ollama, or any OpenAI-compatible client that supports MCP.

![bad-ass-mcp recording itself interacting with MarkTheCrab](docs/demo.gif)
*bad-ass-mcp recording itself interacting with [MarkTheCrab](https://github.com/HoldMyBeer-gg/MarkTheCrab)*

## Why

Most AI desktop automation tools work by taking screenshots and asking a vision model "what do you see?" That's slow, expensive, and fragile.

Every major OS exposes a structured accessibility tree — the same one used by screen readers — that describes every button, text field, combo box, and menu in every running application. **bad-ass-mcp** speaks that language directly.

- **macOS**: AXUIElement via PyObjC
- **Linux**: AT-SPI2 via `gi.repository.Atspi`
- **Windows**: UI Automation via `comtypes`

Actions fire on control objects directly, so the target window doesn't need focus. The user can keep working while automation runs in the background.

## Tools

| Tool | Description |
|------|-------------|
| `list_windows` | List all visible application windows |
| `get_tree` | Full accessibility tree for a window as nested JSON |
| `find_elements` | Find elements by role and/or name |
| `click` | Click / invoke an element (foreground-independent) |
| `click_at` | Click at absolute screen coordinates — fallback for webview/canvas UI |
| `type_text` | Type text into a field (SetValue → EditableText → key injection) |
| `select_option` | Select an option in a combo box or list |
| `get_value` | Get the current value or text of an element |
| `wait_for_window` | Wait until a window matching a pattern appears |
| `wait_for_element` | Wait until an element exists and optionally has a given state |
| `screenshot` | Capture a PNG (write to `output_path` or return base64) |
| `start_recording` | Start recording the screen (or a specific window) |
| `stop_recording` | Stop recording and export as a GIF |
| `learn_layout` | Resolve semantic names → live handle IDs (one call per session) |
| `run_sequence` | Execute a list of actions server-side — no per-action round-trips |

## Installation

**Requirements**: Python 3.11+, `ffmpeg` + `gifsicle` for recording (optional)

```bash
# macOS
pip install 'bad-ass-mcp[macos]'

# Linux — install system PyGObject + AT-SPI bindings first, then:
pip install bad-ass-mcp

# Windows
pip install 'bad-ass-mcp[windows]'
```

**Linux**: PyGObject and AT-SPI are not on PyPI — install them from your distro:

```bash
# Debian/Ubuntu
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core

# Arch
sudo pacman -S python-gobject at-spi2-core

# Fedora
sudo dnf install python3-gobject at-spi2-core
```

Then make sure AT-SPI is enabled (most desktop environments — GNOME, KDE, XFCE — have it on by default):

```bash
# Check
gsettings get org.gnome.desktop.interface toolkit-accessibility

# Enable
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

If you're using a venv, create it with `--system-site-packages` so it can see the distro-installed `gi`:

```bash
python -m venv --system-site-packages venv
```

**Windows**: Elevated (admin) applications can only be automated from an elevated Python process. No special permissions are needed for normal apps.

### Register with Claude Code

```bash
claude mcp add bad-ass-mcp --scope user -- bad-ass-mcp
```

Or manually in `~/.claude.json`:

```json
{
  "mcpServers": {
    "bad-ass-mcp": {
      "type": "stdio",
      "command": "bad-ass-mcp"
    }
  }
}
```

## Usage

```
list the windows on screen
→ [{ "id": "12345", "name": "Firefox", ... }]

find the search box in window 12345 and type "hello"
→ find_elements(window_id="12345", role="entry")
→ type_text(handle_id="...", text="hello")
```

For apps that don't expose a clean accessibility tree (WebKit-based editors, Electron apps), `type_text` falls back to platform-native key injection automatically (AT-SPI on Linux, Quartz events on macOS, `PostMessage` on Windows).

## Architecture

```
bad_ass_mcp/
├── server.py          # FastMCP tool definitions
├── types.py           # WindowInfo, ElementHandle, ActionResult, StaleHandleError
└── backend/
    ├── base.py        # Abstract DesktopBackend interface
    ├── linux.py       # AT-SPI2 + xdotool
    ├── macos.py       # AXUIElement + Quartz
    └── windows.py     # UI Automation + ctypes Win32
```

The abstract backend interface means adding a new platform is just implementing one class. The same 24 contract tests run against every backend via `FakeBackend`.

## Releasing

Tag a version and the release workflow builds and attaches the wheel automatically:

```bash
git tag v0.2.0 && git push --tags
```

## License

MIT
