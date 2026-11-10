"""
LinuxBackend input: real keystrokes for web content, AT-SPI EditableText for
native widgets, gi-correct interface access, and the guarantee that no
modifier key stays held, whatever happens mid-call.

Runs on all platforms: gi/AT-SPI stubbed, every subprocess call mocked.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from .test_linux_internals import _make_backend

MODS = ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Super_L", "Super_R")

# What a real gi Atspi.Accessible exposes (at-spi2-core 2.60): interface
# support is listed by get_interfaces(), and interface methods live on the
# interface classes (Atspi.Text.get_text(node, 0, -1)). There is no pyatspi-era
# query_<kind>(). A spec'd mock keeps the backend's attribute lookups honest.
_GI_ACCESSIBLE_METHODS = [
    "get_process_id",
    "get_application",
    "get_role",
    "get_name",
    "get_state_set",
    "grab_focus",
    "get_n_actions",
    "get_action_name",
    "do_action",
    "get_interfaces",
]


def _gi_atspi(backend):
    """Give the stubbed Atspi module the interface classes gi exposes, as mocks."""
    atspi = backend._atspi_mod
    for cls in ("Text", "EditableText", "Value", "Component", "Selection", "Action"):
        setattr(atspi, cls, MagicMock(name=f"Atspi.{cls}"))
    return atspi


def _calls(mock_run):
    return [c[0][0] for c in mock_run.call_args_list]


def _keyups(mock_run):
    return [c for c in _calls(mock_run) if c[:2] == ["xdotool", "keyup"]]


def _text_node(pid=9999, app_pid=8888):
    """Web-content text node: served by a renderer process (pid) that differs
    from the application's (app_pid), the shape WebKitGTK exposes. It has no
    Value interface, so type_text reaches the keystroke path."""
    node = MagicMock(spec=_GI_ACCESSIBLE_METHODS)
    node.get_interfaces.return_value = ["Accessible", "Action", "Component", "EditableText", "Text"]
    node.get_process_id.return_value = pid
    node.get_application.return_value.get_process_id.return_value = app_pid
    node.get_role.return_value = "text"
    return node


def _native_node(pid=4321):
    """Toolkit widget (GTK entry): same pid as its application."""
    return _text_node(pid=pid, app_pid=pid)


# ── press_key ────────────────────────────────────────────────────────


def test_press_key_uses_xtest_not_xsendevent():
    """GTK/WebKitGTK drop `xdotool key --window` (XSendEvent); the key must go
    through XTEST after the window is given focus."""
    backend, _ = _make_backend()
    app = MagicMock()
    app.get_process_id.return_value = 1234

    with (
        patch.object(backend, "_find_app", return_value=app),
        patch.object(backend, "_find_xwid", return_value="99999"),
        patch.object(backend, "_activate_for_pid") as activate,
        patch("subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        result = backend.press_key("Escape", window_id="1234")

    assert result.ok is True
    activate.assert_called_once_with("99999", 1234)
    key_calls = [c for c in _calls(run) if c[:2] == ["xdotool", "key"]]
    assert key_calls == [["xdotool", "key", "Escape"]]
    assert "--window" not in sum(key_calls, [])
    assert "--clearmodifiers" not in sum(key_calls, [])


def test_press_key_releases_modifiers_after_chord():
    backend, _ = _make_backend()
    app = MagicMock()
    app.get_process_id.return_value = 1

    with (
        patch.object(backend, "_find_app", return_value=app),
        patch.object(backend, "_find_xwid", return_value="7"),
        patch.object(backend, "_activate_for_pid"),
        patch("subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        backend.press_key("ctrl+l", window_id="1")

    calls = _calls(run)
    assert ["xdotool", "key", "ctrl+l"] in calls
    ups = _keyups(run)
    assert ups, "no keyup after the chord"
    assert calls.index(ups[-1]) > calls.index(["xdotool", "key", "ctrl+l"])
    for m in MODS:
        assert m in ups[-1]


# ── type_text: web content → keystrokes ──────────────────────────────


def test_type_text_web_content_uses_xtest_with_select_all():
    """WebKitGTK's EditableText.set_text_contents fires no DOM input events,
    so typing into web content must be real keystrokes: select-all, then text."""
    backend, _ = _make_backend()
    atspi = _gi_atspi(backend)
    backend._handles["h"] = _text_node()

    with (
        patch.object(backend, "_find_xwid", return_value="77777"),
        patch.object(backend, "_activate_for_pid"),
        patch("subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        result = backend.type_text("h", "Geo")

    assert result.ok is True
    atspi.EditableText.set_text_contents.assert_not_called()
    calls = [c for c in _calls(run) if c[:2] != ["xdotool", "keyup"]]
    assert calls == [
        ["xdotool", "key", "ctrl+a"],
        ["xdotool", "type", "--delay", "10", "Geo"],
    ]
    assert run.call_args.kwargs.get("shell", False) is False


def test_type_text_falls_back_to_app_pid_for_webkit_nodes():
    """Web content nodes report WebKitWebProcess's pid, which owns no X window;
    the application's pid must be tried next."""
    backend, _ = _make_backend()
    _gi_atspi(backend)
    backend._handles["h"] = _text_node(pid=5555, app_pid=4444)

    with (
        patch.object(
            backend, "_find_xwid", side_effect=lambda pid: "42" if pid == 4444 else None
        ) as fx,
        patch.object(backend, "_activate_for_pid") as activate,
        patch("subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        assert backend.type_text("h", "x").ok is True

    assert [c[0][0] for c in fx.call_args_list] == [5555, 4444]
    activate.assert_called_once_with("42", 4444)


# ── type_text: native widgets → EditableText ─────────────────────────


def test_type_text_native_widget_uses_editable_text_not_keystrokes():
    """A GTK entry (same pid as the app) takes EditableText.set_text_contents:
    atomic, emits change signals, and immune to GTK's inline path completion
    racing char-by-char keystrokes (seen in the file chooser's location bar)."""
    backend, _ = _make_backend()
    atspi = _gi_atspi(backend)
    node = _native_node()
    backend._handles["h"] = node

    with patch("subprocess.run") as run:
        result = backend.type_text("h", "/tmp/x/../style.md")

    assert result.ok is True
    atspi.EditableText.set_text_contents.assert_called_once_with(node, "/tmp/x/../style.md")
    assert not [c for c in _calls(run) if c[:2] in (["xdotool", "type"], ["xdotool", "key"])]


# ── interface access: gi names, not pyatspi ──────────────────────────


def test_get_value_reads_text_through_gi_interface_class():
    """gi has no query_text(); Text methods are called as
    Atspi.Text.get_text(node, 0, -1). The old spelling raised AttributeError
    and made every value read fall back to the accessible name."""
    backend, _ = _make_backend()
    atspi = _gi_atspi(backend)
    node = _native_node()
    atspi.Text.get_text.return_value = "hello"
    backend._handles["h"] = node

    assert backend.get_value("h") == "hello"
    atspi.Text.get_text.assert_called_once_with(node, 0, -1)


def test_get_value_skips_interfaces_the_node_lacks():
    backend, _ = _make_backend()
    atspi = _gi_atspi(backend)
    node = MagicMock(spec=_GI_ACCESSIBLE_METHODS)
    node.get_interfaces.return_value = ["Accessible", "Action"]
    node.get_name.return_value = "Preview font"
    backend._handles["h"] = node

    assert backend.get_value("h") == "Preview font"
    atspi.Text.get_text.assert_not_called()
    atspi.Value.get_current_value.assert_not_called()


def test_pyatspi_spelling_still_accepted():
    backend, _ = _make_backend()
    node = MagicMock(spec=["query_text", "get_name", "get_role", "get_process_id"])
    node.query_text.return_value.get_text.return_value = "legacy"
    backend._handles["h"] = node

    assert backend.get_value("h") == "legacy"


def test_option_selected_treats_name_as_unreadable():
    """WebKitGTK combo boxes expose neither Text nor Value; get_value falls back
    to the accessible name, which is not the selection and must not be read as
    'option not selected'."""
    backend, _ = _make_backend()
    _gi_atspi(backend)
    node = MagicMock(spec=_GI_ACCESSIBLE_METHODS)
    node.get_interfaces.return_value = ["Accessible", "Action", "Selection"]
    node.get_name.return_value = "Preview font"
    backend._handles["combo"] = node

    assert backend._option_selected("combo", "Choose a font…") is None


# ── modifier release: the guarantee ──────────────────────────────────


def _run_that_dies_on(cmd_prefix, exc):
    def fake_run(args, **kw):
        if args[: len(cmd_prefix)] == cmd_prefix:
            raise exc
        return MagicMock(returncode=0)

    return fake_run


def test_type_text_releases_modifiers_when_xdotool_times_out():
    """select-all pressed Control; if the type call hangs and times out, the
    modifiers must still be released."""
    backend, _ = _make_backend()
    _gi_atspi(backend)
    backend._handles["h"] = _text_node()
    boom = subprocess.TimeoutExpired(cmd="xdotool type", timeout=30)

    with (
        patch.object(backend, "_find_xwid", return_value="1"),
        patch.object(backend, "_activate_for_pid"),
        patch("subprocess.run", side_effect=_run_that_dies_on(["xdotool", "type"], boom)) as run,
    ):
        backend.type_text("h", "hello")  # falls through to the other strategies

    calls = _calls(run)
    idx_type = next(i for i, c in enumerate(calls) if c[:2] == ["xdotool", "type"])
    later_keyups = [c for c in calls[idx_type + 1 :] if c[:2] == ["xdotool", "keyup"]]
    assert later_keyups, "modifiers were not released after the failed type"
    for m in MODS:
        assert m in later_keyups[0]


def test_sequence_failing_halfway_leaves_no_key_held():
    """run_sequence: a key chord succeeds, then a type step fails. Every
    modifier the sequence pressed must be released by the time it returns."""
    backend, _ = _make_backend()
    _gi_atspi(backend)
    backend._handles["h"] = _text_node()
    app = MagicMock()
    app.get_process_id.return_value = 1
    err = subprocess.CalledProcessError(1, "xdotool type", stderr=b"boom")

    with (
        patch.object(backend, "_find_app", return_value=app),
        patch.object(backend, "_find_xwid", return_value="1"),
        patch.object(backend, "_activate_for_pid"),
        patch("subprocess.run", side_effect=_run_that_dies_on(["xdotool", "type"], err)) as run,
    ):
        results = backend.run_sequence(
            [
                {"action": "key", "key": "ctrl+l", "window_id": "1"},
                {"action": "type", "handle": "h", "text": "fails"},
                {"action": "key", "key": "Return", "window_id": "1"},
            ],
            stop_on_error=True,
        )

    assert results[0]["ok"] is True
    calls = _calls(run)
    presses = [i for i, c in enumerate(calls) if c[:2] in (["xdotool", "key"], ["xdotool", "type"])]
    for i in presses:
        followed = any(c[:2] == ["xdotool", "keyup"] for c in calls[i + 1 : i + 3])
        assert followed, f"press at {i} not followed by keyup: {calls[i]}"
    # and the very last xdotool call is a release
    assert calls[-1][:2] == ["xdotool", "keyup"]


def test_close_releases_all_modifiers():
    backend, _ = _make_backend()
    with patch("subprocess.run") as run:
        backend.close()
    ups = _keyups(run)
    assert len(ups) == 1
    for m in MODS:
        assert m in ups[0]


def test_release_modifiers_never_raises():
    backend, _ = _make_backend()
    with patch("subprocess.run", side_effect=FileNotFoundError("no xdotool")):
        backend.release_modifiers()  # must not raise


# ── states: aria-pressed ─────────────────────────────────────────────


def test_to_handle_reports_pressed_state():
    """WebKitGTK maps aria-pressed="true" to AT-SPI's PRESSED state (not
    CHECKED), so a toolbar toggle's on/off is invisible unless it's collected."""
    backend, _ = _make_backend()
    atspi = backend._atspi_mod
    atspi.StateType.PRESSED = "PRESSED"
    atspi.StateType.VISIBLE = "VISIBLE"
    node = MagicMock()
    node.get_role_name.return_value = "toggle button"
    node.get_name.return_value = "Toggle layout"
    node.get_interfaces.return_value = ["Accessible", "Action"]
    node.get_state_set.return_value.contains.side_effect = lambda s: s in ("PRESSED", "VISIBLE")

    el = backend._to_handle(node)

    assert "pressed" in el.states
    assert "visible" in el.states
