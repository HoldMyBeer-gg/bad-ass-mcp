"""
Regression tests for LinuxBackend key injection — run on all platforms
(no AT-SPI or X11 required; all subprocess calls are mocked).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────


def _make_backend():
    """Return a LinuxBackend instance with all gi/AT-SPI imports stubbed out."""
    import sys
    import types

    # Stub gi hierarchy so linux.py can import without AT-SPI installed
    gi_mod = types.ModuleType("gi")
    gi_repo = types.ModuleType("gi.repository")
    atspi_mod = types.ModuleType("gi.repository.Atspi")
    empty_desktop = MagicMock(get_child_count=MagicMock(return_value=0))
    atspi_mod.get_desktop = MagicMock(return_value=empty_desktop)
    atspi_mod.StateType = MagicMock()
    atspi_mod.KeySynthType = MagicMock()
    atspi_mod.generate_keyboard_event = MagicMock()
    atspi_mod.CoordType = MagicMock()
    gi_mod.require_version = MagicMock()
    gi_mod.repository = gi_repo
    gi_repo.Atspi = atspi_mod

    with patch.dict(
        sys.modules,
        {
            "gi": gi_mod,
            "gi.repository": gi_repo,
            "gi.repository.Atspi": atspi_mod,
        },
    ):
        # Force re-import so the stubs are used
        import importlib

        if "bad_ass_mcp.backend.linux" in sys.modules:
            del sys.modules["bad_ass_mcp.backend.linux"]

        mod = importlib.import_module("bad_ass_mcp.backend.linux")
        backend = mod.LinuxBackend()
        backend._atspi_mod = atspi_mod  # stash for assertions
        return backend, mod


# ── press_key: xdotool --window path ─────────────────────────────────


def test_press_key_with_window_id_uses_xdotool_window():
    """press_key should call xdotool key --window XWID without windowactivate."""
    backend, _mod = _make_backend()

    fake_app = MagicMock()
    fake_app.get_process_id.return_value = 1234

    with patch.object(backend, "_find_app", return_value=fake_app):
        with patch.object(backend, "_find_xwid", return_value="99999"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = backend.press_key("Return", window_id="1234")

    assert result.ok is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]  # first positional arg is the command list
    assert args[0] == "xdotool"
    assert "key" in args
    assert "--window" in args
    assert "99999" in args
    assert "--clearmodifiers" in args
    assert "Return" in args
    # Must NOT contain windowactivate (the old focus-stealing call)
    assert "windowactivate" not in args


def test_press_key_no_window_id_skips_xdotool():
    """press_key without a window_id should not call xdotool at all."""
    backend, _mod = _make_backend()

    # Patch Atspi on the already-imported module inside the backend's module
    import sys

    atspi_mod = sys.modules.get("gi.repository.Atspi")
    if atspi_mod is None:
        atspi_mod = backend._atspi_mod

    with patch("subprocess.run") as mock_run:
        with patch.object(atspi_mod, "generate_keyboard_event"):
            backend.press_key("Escape")

    mock_run.assert_not_called()


def test_press_key_falls_back_to_atspi_when_xdotool_missing():
    """If xdotool is not installed (FileNotFoundError), press_key must not crash
    and must fall back to AT-SPI generate_keyboard_event."""
    import sys
    import types as _types

    backend, _mod = _make_backend()

    atspi_mod = sys.modules.get("gi.repository.Atspi") or backend._atspi_mod

    # Stub GLib so the drain loop inside press_key returns immediately
    glib_ctx = MagicMock()
    glib_ctx.iteration.return_value = False
    glib_stub = _types.ModuleType("gi.repository.GLib")
    glib_stub.main_context_default = MagicMock(return_value=glib_ctx)

    fake_app = MagicMock()
    fake_app.get_process_id.return_value = 5678

    # Re-add the full gi stub tree so runtime `from gi.repository import GLib` works
    import types as _types2

    gi_stub = _types2.ModuleType("gi")
    gi_repo_stub = _types2.ModuleType("gi.repository")
    gi_stub.repository = gi_repo_stub
    gi_stub.require_version = MagicMock()
    gi_repo_stub.Atspi = atspi_mod

    with patch.dict(
        sys.modules,
        {
            "gi": gi_stub,
            "gi.repository": gi_repo_stub,
            "gi.repository.Atspi": atspi_mod,
            "gi.repository.GLib": glib_stub,
        },
    ):
        with patch.object(backend, "_find_app", return_value=fake_app):
            with patch.object(backend, "_find_xwid", return_value="11111"):
                with patch("subprocess.run", side_effect=FileNotFoundError("no xdotool")):
                    with patch.object(atspi_mod, "generate_keyboard_event") as mock_gen:
                        result = backend.press_key("Tab", window_id="5678")

    # FileNotFoundError must not propagate; AT-SPI fallback is used instead
    assert result.ok is True
    mock_gen.assert_called_once()


# ── type_text: xdotool type --window path ────────────────────────────


def test_type_text_fallback_uses_xdotool_type_window():
    """The AT-SPI keyboard injection fallback should prefer xdotool type --window."""
    backend, _mod = _make_backend()

    # Set up a node that rejects SetValue and EditableText so we reach the fallback
    fake_node = MagicMock()
    fake_node.query_value.side_effect = Exception("no value iface")
    fake_node.query_editable_text.side_effect = Exception("no editable iface")
    fake_node.get_process_id.return_value = 9999
    backend._handles["h-text"] = fake_node
    # Make _resolve succeed without live AT-SPI
    fake_node.get_role.return_value = "text"

    with patch.object(backend, "_find_xwid", return_value="77777"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.type_text("h-text", "hello world")

    assert result.ok is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "xdotool"
    assert "type" in args
    assert "--window" in args
    assert "77777" in args
    assert "--clearmodifiers" in args
    assert "hello world" in args
    # Must not use shell=True
    assert mock_run.call_args.kwargs.get("shell", False) is False
