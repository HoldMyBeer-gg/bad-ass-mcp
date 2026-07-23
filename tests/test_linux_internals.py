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


# ── webview a11y wake-probe ──────────────────────────────────────────


def _win(pid, accessible, name="app"):
    from bad_ass_mcp.types import WindowInfo

    return WindowInfo(id=str(pid), name=name, pid=pid, focused=False, accessible=accessible)


def test_list_windows_wakes_webview_and_reprobes():
    """An X11-fallback window whose process smells like a webview should
    trigger the screen-reader flag poke, and the AT-SPI re-probe should
    replace the accessible=False entry with the real one."""
    backend, _mod = _make_backend()

    # First AT-SPI pass: empty. After the wake, pid 42 has registered.
    atspi_results = [[], [_win(42, True, "electron-app")]]

    with (
        patch.object(backend, "_atspi_list_windows", side_effect=atspi_results),
        patch.object(backend, "_x11_list_windows", return_value=[_win(42, False)]),
        patch.object(backend, "_pid_smells_webview", return_value=True),
        patch.object(backend, "_ensure_screen_reader_flag", return_value=True) as flag,
        patch("time.sleep"),
    ):
        result = backend.list_windows()

    flag.assert_called_once()
    assert len(result) == 1
    assert result[0].pid == 42
    assert result[0].accessible is True


def test_list_windows_keeps_inaccessible_when_wake_fails():
    """If the woken process never registers with AT-SPI, the X11-fallback
    entry must survive (accessible=False) rather than disappearing."""
    backend, _mod = _make_backend()
    backend._WAKE_TIMEOUT = 0.0  # don't spin the poll loop for real

    with (
        patch.object(backend, "_atspi_list_windows", return_value=[]),
        patch.object(backend, "_x11_list_windows", return_value=[_win(42, False)]),
        patch.object(backend, "_pid_smells_webview", return_value=True),
        patch.object(backend, "_ensure_screen_reader_flag", return_value=True),
        patch("time.sleep"),
    ):
        result = backend.list_windows()

    assert len(result) == 1
    assert result[0].accessible is False


def test_list_windows_skips_wake_for_non_webview():
    """Games / OpenGL canvases must not trigger the flag poke or any wait."""
    backend, _mod = _make_backend()

    with (
        patch.object(backend, "_atspi_list_windows", return_value=[]) as atspi,
        patch.object(backend, "_x11_list_windows", return_value=[_win(77, False, "game")]),
        patch.object(backend, "_pid_smells_webview", return_value=False),
        patch.object(backend, "_ensure_screen_reader_flag") as flag,
    ):
        result = backend.list_windows()

    flag.assert_not_called()
    atspi.assert_called_once()  # no re-probe loop
    assert result[0].accessible is False


def test_wake_attempted_once_per_pid():
    """Second list_windows call for the same stubborn pid must not wait again."""
    backend, _mod = _make_backend()
    backend._WAKE_TIMEOUT = 0.0

    with (
        patch.object(backend, "_atspi_list_windows", return_value=[]),
        patch.object(backend, "_x11_list_windows", return_value=[_win(42, False)]),
        patch.object(backend, "_pid_smells_webview", return_value=True),
        patch.object(backend, "_ensure_screen_reader_flag", return_value=True) as flag,
        patch("time.sleep"),
    ):
        backend.list_windows()
        backend.list_windows()

    flag.assert_called_once()  # pid 42 is in _woken_pids after the first pass


def test_find_app_waking_retries_after_wake():
    """get_tree path: _find_app misses, wake succeeds, retry finds the app."""
    backend, _mod = _make_backend()
    backend._WAKE_TIMEOUT = 1.0

    fake_app = MagicMock()
    finds = [None, fake_app]

    with (
        patch.object(backend, "_find_app", side_effect=finds),
        patch.object(backend, "_wake_webview_a11y", return_value=True),
        patch("time.sleep"),
    ):
        result = backend._find_app_waking("42")

    assert result is fake_app


def test_find_app_waking_no_wake_for_names():
    """Non-numeric window ids (app names) can't be woken — return None fast."""
    backend, _mod = _make_backend()

    with (
        patch.object(backend, "_find_app", return_value=None),
        patch.object(backend, "_wake_webview_a11y") as wake,
    ):
        assert backend._find_app_waking("SomeApp") is None

    wake.assert_not_called()


def test_pid_smells_webview_matches_electron_cmdline(tmp_path):
    """Marker matching against a real-format cmdline blob."""
    backend, _mod = _make_backend()

    cmdline = tmp_path / "cmdline"
    cmdline.write_bytes(b"/usr/lib/slack/slack\x00--type=browser\x00")

    real_open = open

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/42/cmdline":
            return real_open(cmdline, *a, **kw)
        raise FileNotFoundError(path)

    with patch("builtins.open", side_effect=fake_open):
        with patch("os.path.realpath", return_value="/usr/lib/slack/electron-exe"):
            assert backend._pid_smells_webview(42) is True

    with patch("builtins.open", side_effect=FileNotFoundError):
        with patch("os.path.realpath", return_value="/usr/bin/supertuxkart"):
            assert backend._pid_smells_webview(42) is False
