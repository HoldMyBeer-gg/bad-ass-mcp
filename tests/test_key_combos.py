"""press_key combinations: "Cmd+A", "Ctrl+Shift+G", "Alt+F4".

One string works everywhere. "Cmd" is the platform's primary shortcut
modifier (Command on macOS, Ctrl on Windows and Linux) so "Cmd+A" means
select-all on every OS; "Super"/"Win" is the literal Windows/Super key.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from bad_ass_mcp.backend.base import parse_combo, platform_modifiers

# ── parsing ──────────────────────────────────────────────────────────


def test_a_plain_key_is_not_a_combo():
    assert parse_combo("Return") is None
    assert parse_combo("a") is None
    assert parse_combo("+") is None


def test_combo_parts_are_normalised():
    assert parse_combo("Cmd+A") == (("cmd",), "A")
    assert parse_combo("ctrl+shift+g") == (("ctrl", "shift"), "g")
    assert parse_combo("Option+Command+Escape") == (("alt", "cmd"), "Escape")
    assert parse_combo("Win+E") == (("super",), "E")


def test_the_plus_key_itself_can_be_the_target():
    assert parse_combo("Ctrl++") == (("ctrl",), "+")


def test_an_unknown_modifier_is_an_error_not_a_literal():
    with pytest.raises(ValueError, match="Hyper"):
        parse_combo("Hyper+A")


def test_cmd_is_the_primary_modifier_per_platform():
    assert platform_modifiers(("cmd", "shift"), "macos") == ("cmd", "shift")
    assert platform_modifiers(("cmd", "shift"), "windows") == ("ctrl", "shift")
    assert platform_modifiers(("cmd",), "linux") == ("ctrl",)
    assert platform_modifiers(("super",), "macos") == ("cmd",)
    assert platform_modifiers(("super",), "windows") == ("super",)
    # Ctrl+Cmd on Windows collapses to one Ctrl
    assert platform_modifiers(("ctrl", "cmd"), "windows") == ("ctrl",)


# ── Linux: xdotool chord string ──────────────────────────────────────


def test_linux_chord_string_for_xdotool():
    from bad_ass_mcp.backend.linux_keys import xdotool_chord

    assert xdotool_chord(("ctrl", "shift"), "G") == "ctrl+shift+g"
    assert xdotool_chord(("super",), "e") == "super+e"
    assert xdotool_chord(("alt",), "F4") == "alt+F4"
    assert xdotool_chord(("ctrl",), "Return") == "ctrl+Return"
    assert xdotool_chord(("ctrl",), "+") == "ctrl+plus"


# ── Windows: SendInput sequence ──────────────────────────────────────


def test_windows_combo_presses_modifiers_then_key_then_releases_in_reverse():
    from bad_ass_mcp.backend.windows import _combo_inputs

    ins = _combo_inputs(("ctrl", "shift"), 0x47)  # G
    seq = [(i.ki.wVk, bool(i.ki.dwFlags & 0x2)) for i in ins]
    assert seq == [
        (0x11, False),  # Ctrl down
        (0x10, False),  # Shift down
        (0x47, False),  # G down
        (0x47, True),
        (0x10, True),
        (0x11, True),
    ]
    assert all(i.type == 1 for i in ins)


def test_windows_vk_for_letters_digits_and_names():
    from bad_ass_mcp.backend.windows import _vk_for

    assert _vk_for("a") == 0x41
    assert _vk_for("A") == 0x41
    assert _vk_for("7") == 0x37
    assert _vk_for("F4") == 0x73
    assert _vk_for("Escape") == 0x1B


# ── macOS: CGEvent flags and key codes ───────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_combo_posts_the_key_with_modifier_flags():
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    sent = []
    with (
        patch.object(backend, "_pid_for_window", return_value=4242),
        patch.object(backend, "_bring_to_front", return_value=None),
        patch.object(backend, "_frontmost_pid", return_value=4242),
        patch.object(macos, "_quartz_key_press", side_effect=lambda *a, **k: sent.append((a, k))),
    ):
        r = backend.press_key("Cmd+Shift+G", window_id="4242")
    assert r.ok
    (args, kw) = sent[0]
    assert args[0] == 0x05  # G on an ANSI keyboard
    assert args[1] == macos.kCGEventFlagMaskCommand | macos.kCGEventFlagMaskShift
    # Posted as real input, not to the pid: an NSOpenPanel runs out of
    # process and never sees pid-targeted keys (TextEdit, Audition).
    assert kw["pid"] is None


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_combo_brings_the_app_forward_and_gives_focus_back():
    """Menu shortcuts only reach the active app."""
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    order = []
    with (
        patch.object(backend, "_pid_for_window", return_value=4242),
        patch.object(
            backend, "_bring_to_front", side_effect=lambda pid: order.append("front") or 777
        ),
        patch.object(
            backend, "_set_frontmost", side_effect=lambda pid: order.append(f"back:{pid}")
        ),
        patch.object(backend, "_frontmost_pid", return_value=4242),
        patch.object(macos, "_quartz_key_press", side_effect=lambda *a, **k: order.append("key")),
    ):
        assert backend.press_key("Cmd+A", window_id="4242").ok
    assert order == ["front", "key", "back:777"]


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_unknown_key_in_a_combo_fails_cleanly():
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    with patch.object(macos, "_quartz_key_press") as kp:
        r = backend.press_key("Cmd+Ü")
    assert not r.ok and "Ü" in r.error
    kp.assert_not_called()


def test_unknown_modifier_reaches_the_caller_as_a_failed_result():
    from bad_ass_mcp import server
    from tests.fake_backend import FakeBackend

    desk = FakeBackend()
    with patch.object(server, "_backend", return_value=desk):
        r = server.press_key("Hyper+A")
    assert r["ok"] is False and "Hyper" in r["error"]
    assert desk.key_log == []


# ── Linux backend ────────────────────────────────────────────────────


def test_linux_combo_activates_the_window_and_sends_one_xtest_chord():
    from tests.test_linux_internals import _make_backend

    backend, _mod = _make_backend()
    app = MagicMock(get_process_id=MagicMock(return_value=77))
    sent = []
    with (
        patch.object(backend, "_find_app", return_value=app),
        patch.object(backend, "_find_xwid", return_value="0x400007"),
        patch.object(backend, "_activate_for_pid"),
        patch.object(backend, "_xtest", side_effect=lambda args, **k: sent.append(args)),
    ):
        assert backend.press_key("Cmd+Shift+G", window_id="77").ok
    assert sent == [["key", "ctrl+shift+g"]]


def test_linux_combo_on_a_native_wayland_window_fails_clearly():
    from tests.test_linux_internals import _make_backend

    backend, _mod = _make_backend()
    app = MagicMock(get_process_id=MagicMock(return_value=77))
    with (
        patch.object(backend, "_find_app", return_value=app),
        patch.object(backend, "_find_xwid", return_value=None),
        patch.object(backend, "_xtest") as xt,
    ):
        r = backend.press_key("Ctrl+A", window_id="77")
    assert not r.ok and "X11" in r.error
    xt.assert_not_called()


def test_linux_combo_without_a_window_goes_to_the_focused_app():
    from tests.test_linux_internals import _make_backend

    backend, _mod = _make_backend()
    sent = []
    with patch.object(backend, "_xtest", side_effect=lambda args, **k: sent.append(args)):
        assert backend.press_key("Alt+F4").ok
    assert sent == [["key", "alt+F4"]]


# ── BAM scripts ──────────────────────────────────────────────────────


def test_a_combo_key_step_is_real_input_even_with_a_window():
    """Combinations are sent as real keystrokes to the front window, so the
    runner must guard them like other real input."""
    from bad_ass_mcp.bam.describe import is_real_input

    assert is_real_input({"action": "key", "key": "Cmd+S", "window_id": "$WINDOW"})
    assert not is_real_input({"action": "key", "key": "Return", "window_id": "$WINDOW"})


def test_a_script_with_an_unknown_modifier_is_rejected_before_running():
    from bad_ass_mcp.bam.validate import validate_script

    s = {
        "name": "t",
        "platforms": ["linux"],
        "phases": [{"name": "p", "steps": [{"action": "key", "key": "Hyper+A"}]}],
    }
    issues = validate_script(s)
    assert any("Hyper" in i.message and i.path.endswith(".key") for i in issues)


def test_the_tool_refuses_a_combo_without_a_window():
    """Without window_id a chord goes to whatever is in front, unchecked; a
    stray Alt+F4 closes the user's app. The MCP tool insists on a target.
    (BAM key steps without window_id are guarded by the runner instead.)"""
    from bad_ass_mcp import server
    from tests.fake_backend import FakeBackend

    desk = FakeBackend()
    with patch.object(server, "_backend", return_value=desk):
        r = server.press_key("Alt+F4")
    assert r["ok"] is False and "window_id" in r["error"]
    assert desk.key_log == []
    with patch.object(server, "_backend", return_value=desk):
        assert server.press_key("Return")["ok"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_combo_failure_names_the_whole_combination():
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    with (
        patch.object(backend, "_pid_for_window", return_value=4242),
        patch.object(backend, "_bring_to_front", side_effect=RuntimeError("could not raise")),
    ):
        r = backend.press_key("Cmd+Shift+G", window_id="4242")
    assert not r.ok and "Cmd+Shift+G" in r.error


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_combo_refuses_when_the_target_is_not_in_front_after_all():
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    with (
        patch.object(backend, "_pid_for_window", return_value=4242),
        patch.object(backend, "_bring_to_front", return_value=None),
        patch.object(backend, "_frontmost_pid", return_value=111),
        patch.object(macos, "_quartz_key_press") as kp,
    ):
        r = backend.press_key("Cmd+Shift+G", window_id="4242")
    assert not r.ok and "Cmd+Shift+G" in r.error
    kp.assert_not_called()


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_frontmost_pid_pumps_the_run_loop():
    """frontmostApplication() is updated by the same notifications as
    runningApplications(): without a run loop it never changes, so a raise
    that worked was reported as failed."""
    from bad_ass_mcp.backend import macos

    pumped = []
    with patch.object(macos, "_pump_run_loop", side_effect=lambda: pumped.append(1)):
        macos.MacOSBackend._frontmost_pid()
    assert pumped


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_failed_raise_hands_focus_back():
    """The raise can land after we stop waiting; then the user's app must
    get focus back rather than being left behind the target."""
    from bad_ass_mcp.backend import macos

    backend = macos.MacOSBackend()
    restored = []
    with (
        patch.object(backend, "_frontmost_pid", return_value=111),
        patch.object(
            backend,
            "_set_frontmost",
            side_effect=lambda pid: restored.append(pid) or (pid == 111),
        ),
    ):
        with pytest.raises(RuntimeError):
            backend._bring_to_front(4242)
    assert restored == [4242, 111]
