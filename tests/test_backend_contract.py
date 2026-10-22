"""
Contract tests: every DesktopBackend implementation must pass these.
Run against FakeBackend here; the Linux/Windows/macOS backends import
and run the same suite via pytest parametrize or inheritance.
"""

import pytest

from bad_ass_mcp.types import ActionResult, ElementHandle, StaleHandleError, WindowInfo

from .fake_backend import FAKE_WINDOW_ID, FakeBackend


@pytest.fixture
def backend():
    return FakeBackend()


# ── list_windows ─────────────────────────────────────────────────────


def test_list_windows_returns_window_info_list(backend):
    windows = backend.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1
    assert all(isinstance(w, WindowInfo) for w in windows)


def test_list_windows_has_required_fields(backend):
    w = backend.list_windows()[0]
    assert isinstance(w.id, str) and w.id
    assert isinstance(w.name, str)
    assert isinstance(w.pid, int)
    assert isinstance(w.focused, bool)
    assert isinstance(w.accessible, bool)
    assert w.bounds is None or (
        isinstance(w.bounds, tuple)
        and len(w.bounds) == 4
        and all(isinstance(v, int) for v in w.bounds)
    )


# ── get_tree ─────────────────────────────────────────────────────────


def test_get_tree_returns_element_handle(backend):
    tree = backend.get_tree(FAKE_WINDOW_ID)
    assert isinstance(tree, ElementHandle)


def test_get_tree_root_has_children(backend):
    tree = backend.get_tree(FAKE_WINDOW_ID)
    assert len(tree.children) > 0


# ── find_elements ─────────────────────────────────────────────────────


def test_find_by_role(backend):
    buttons = backend.find_elements(FAKE_WINDOW_ID, role="button")
    assert len(buttons) >= 1
    assert all(el.role == "button" for el in buttons)


def test_find_by_name(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, name="OK")
    assert len(results) >= 1
    assert results[0].name == "OK"


def test_find_by_role_and_name(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, role="button", name="Cancel")
    assert len(results) == 1
    assert results[0].role == "button"
    assert results[0].name == "Cancel"


def test_find_nonexistent_returns_empty(backend):
    results = backend.find_elements(FAKE_WINDOW_ID, name="Definitely Not Here")
    assert results == []


# ── click ─────────────────────────────────────────────────────────────


def test_click_button_succeeds(backend):
    result = backend.click("btn-ok")
    assert isinstance(result, ActionResult)
    assert result.ok is True
    assert result.error is None


def test_click_stale_handle_raises(backend):
    backend.invalidate("btn-ok")
    with pytest.raises(StaleHandleError):
        backend.click("btn-ok")


# ── type_text ─────────────────────────────────────────────────────────


def test_type_text_into_editable_field(backend):
    result = backend.type_text("txt-name", "hello")
    assert result.ok is True
    assert backend.get_value("txt-name") == "hello"


def test_type_text_into_non_editable_fails(backend):
    result = backend.type_text("btn-ok", "oops")
    assert result.ok is False
    assert result.error is not None


def test_type_text_stale_handle_raises(backend):
    backend.invalidate("txt-name")
    with pytest.raises(StaleHandleError):
        backend.type_text("txt-name", "hello")


# ── select_option ─────────────────────────────────────────────────────


def test_select_valid_option(backend):
    result = backend.select_option("combo-size", "Large")
    assert result.ok is True
    assert backend.get_value("combo-size") == "Large"


def test_select_invalid_option_fails(backend):
    result = backend.select_option("combo-size", "Enormous")
    assert result.ok is False
    assert result.error is not None


# ── get_value ─────────────────────────────────────────────────────────


def test_get_value_returns_current_value(backend):
    assert backend.get_value("combo-size") == "Medium"


def test_get_value_reflects_type_text(backend):
    backend.type_text("txt-name", "world")
    assert backend.get_value("txt-name") == "world"


def test_get_value_stale_handle_raises(backend):
    backend.invalidate("txt-name")
    with pytest.raises(StaleHandleError):
        backend.get_value("txt-name")


# ── wait_for_window ───────────────────────────────────────────────────


def test_wait_for_window_appears(backend):
    backend.schedule_window(delay=0.02)
    result = backend.wait_for_window("Fake App", timeout=1.0)
    assert result is not None
    assert isinstance(result, WindowInfo)


def test_wait_for_window_times_out(backend):
    result = backend.wait_for_window("Ghost Window", timeout=0.05)
    assert result is None


# ── wait_for_element ──────────────────────────────────────────────────


def test_wait_for_element_finds_existing(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK")
    assert el is not None
    assert el.name == "OK"


def test_wait_for_element_with_state(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK", state="enabled")
    assert el is not None


def test_wait_for_element_missing_state_returns_none(backend):
    el = backend.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK", state="checked")
    assert el is None


# ── screenshot ────────────────────────────────────────────────────────


def test_screenshot_returns_bytes(backend):
    data = backend.screenshot()
    assert isinstance(data, bytes)
    assert len(data) > 0


# ── learn_layout ──────────────────────────────────────────────────────


def test_learn_layout_resolves_known_elements(backend):
    layout = backend.learn_layout(
        FAKE_WINDOW_ID,
        {
            "ok": {"role": "button", "name": "OK"},
            "name_field": {"role": "text", "name": "Name"},
        },
    )
    assert layout["ok"] is not None
    assert layout["name_field"] is not None


def test_learn_layout_unknown_element_is_none(backend):
    layout = backend.learn_layout(
        FAKE_WINDOW_ID,
        {"ghost": {"role": "button", "name": "Does Not Exist"}},
    )
    assert layout["ghost"] is None


def test_learn_layout_handle_is_usable(backend):
    layout = backend.learn_layout(FAKE_WINDOW_ID, {"ok": {"role": "button", "name": "OK"}})
    result = backend.click(layout["ok"])
    assert result.ok is True


# ── run_sequence ──────────────────────────────────────────────────────


def test_run_sequence_type_and_get_value(backend):
    results = backend.run_sequence(
        [
            {"action": "type", "handle": "txt-name", "text": "hello"},
            {"action": "get_value", "handle": "txt-name"},
        ]
    )
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert results[1]["value"] == "hello"


def test_run_sequence_click(backend):
    results = backend.run_sequence([{"action": "click", "handle": "btn-ok"}])
    assert results[0]["ok"] is True


def test_run_sequence_key(backend):
    results = backend.run_sequence([{"action": "key", "key": "Return"}])
    assert results[0]["ok"] is True


def test_run_sequence_select(backend):
    results = backend.run_sequence(
        [
            {"action": "select", "handle": "combo-size", "value": "Large"},
            {"action": "get_value", "handle": "combo-size"},
        ]
    )
    assert results[0]["ok"] is True
    assert results[1]["value"] == "Large"


def test_run_sequence_sleep(backend):
    results = backend.run_sequence([{"action": "sleep", "seconds": 0.01}])
    assert results[0]["ok"] is True


def test_run_sequence_stops_on_error_by_default(backend):
    results = backend.run_sequence(
        [
            {"action": "click", "handle": "NO_SUCH_HANDLE"},
            {"action": "click", "handle": "btn-ok"},
        ]
    )
    assert len(results) == 1
    assert results[0]["ok"] is False


def test_run_sequence_continues_when_stop_on_error_false(backend):
    results = backend.run_sequence(
        [
            {"action": "click", "handle": "NO_SUCH_HANDLE"},
            {"action": "click", "handle": "btn-ok"},
        ],
        stop_on_error=False,
    )
    assert len(results) == 2
    assert results[0]["ok"] is False
    assert results[1]["ok"] is True


def test_run_sequence_unknown_action(backend):
    results = backend.run_sequence([{"action": "teleport"}])
    assert results[0]["ok"] is False
    assert "Unknown action" in results[0]["error"]


def test_run_sequence_wait_for_element(backend):
    results = backend.run_sequence(
        [
            {
                "action": "wait_for_element",
                "window_id": FAKE_WINDOW_ID,
                "role": "button",
                "name": "OK",
                "timeout": 1.0,
            },
        ]
    )
    assert results[0]["ok"] is True
    assert results[0]["handle"] is not None


# ── run_sequence safety limits ────────────────────────────────────────


def test_run_sequence_truncates_oversized_batch(backend, capsys):
    from bad_ass_mcp.backend.base import _MAX_SEQUENCE_STEPS

    steps = [{"action": "click", "handle": "btn-ok"}] * (_MAX_SEQUENCE_STEPS + 50)
    results = backend.run_sequence(steps)
    assert len(results) == _MAX_SEQUENCE_STEPS
    assert "truncating" in capsys.readouterr().err


def test_run_sequence_aborts_at_runtime_limit(backend, capsys):
    from unittest.mock import patch

    from bad_ass_mcp.backend.base import _MAX_SEQUENCE_RUNTIME

    # Force the deadline strictly into the past. 0.0 is not enough: on
    # Windows Python <3.13 time.monotonic() is GetTickCount64() with 15.6ms
    # resolution, so back-to-back calls return the same tick and
    # `monotonic() > deadline` never fires for a whole batch of fast steps.
    with patch("bad_ass_mcp.backend.base._MAX_SEQUENCE_RUNTIME", -1.0):
        results = backend.run_sequence([{"action": "click", "handle": "btn-ok"}] * 5)

    assert any(r.get("error") == "sequence runtime limit exceeded" for r in results)
    assert "wall-clock limit" in capsys.readouterr().err
    _ = _MAX_SEQUENCE_RUNTIME  # keep import used


# ── click: button / clicks / coordinate fallback ──────────────────────


def test_click_right_button_uses_element_center(backend):
    r = backend.click("btn-ok", button="right")
    assert r.ok
    # btn-ok bounds are (10, 10, 80, 30) → center (50, 25)
    assert backend.pointer_log == [("click", 50.0, 25.0, "right", 1)]


def test_click_double_uses_element_center(backend):
    r = backend.click("btn-ok", clicks=2)
    assert r.ok
    assert backend.pointer_log == [("click", 50.0, 25.0, "left", 2)]


def test_click_right_without_bounds_fails_cleanly(backend):
    r = backend.click("btn-cancel", button="right")
    assert not r.ok
    assert "bounds" in (r.error or "")
    assert backend.pointer_log == []


def test_click_plain_left_does_not_go_physical(backend):
    assert backend.click("btn-ok").ok
    assert backend.pointer_log == []


# ── select_option keystroke fallback ─────────────────────────────────


def test_select_by_keystrokes_clicks_types_and_commits(backend):
    backend._values["combo-size"] = "Large"
    r = backend._select_option_by_keystrokes("combo-size", "Large")
    assert r.ok
    # combo-size bounds (10, 50, 120, 24) → center (70, 62)
    assert backend.pointer_log == [("click", 70.0, 62.0, "left", 1)]
    assert backend.key_log == list("Large") + ["Return"]


def test_select_by_keystrokes_reports_mismatch(backend):
    backend._values["combo-size"] = "Medium"  # control never changes
    r = backend._select_option_by_keystrokes("combo-size", "Large")
    assert not r.ok
    assert "Medium" in (r.error or "")


def test_select_by_keystrokes_without_bounds_fails(backend):
    r = backend._select_option_by_keystrokes("txt-name", "x")
    assert not r.ok
    assert backend.key_log == []


def test_option_selected_is_none_when_unreadable(backend):
    assert backend._option_selected("btn-ok", "OK") is None


def test_option_selected_compares_case_insensitively(backend):
    backend._values["combo-size"] = "  large "
    assert backend._option_selected("combo-size", "Large") is True
    assert backend._option_selected("combo-size", "Small") is False


# ── drag / run_sequence pointer actions ───────────────────────────────


def test_run_sequence_click_with_button_and_clicks(backend):
    res = backend.run_sequence([{"action": "click", "handle": "btn-ok", "button": "right"}])
    assert res[0]["ok"]
    assert backend.pointer_log[-1] == ("click", 50.0, 25.0, "right", 1)


def test_run_sequence_click_at_passes_button(backend):
    res = backend.run_sequence(
        [{"action": "click_at", "x": 1, "y": 2, "button": "middle", "clicks": 2}]
    )
    assert res[0]["ok"]
    assert backend.pointer_log == [("click", 1.0, 2.0, "middle", 2)]


def test_run_sequence_drag(backend):
    res = backend.run_sequence([{"action": "drag", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}])
    assert res[0]["ok"]
    assert backend.pointer_log == [("drag", 1.0, 2.0, 3.0, 4.0, "left")]


# ── recording output validation ──────────────────────────────────────


def test_recording_target_accepts_gif_and_mp4(backend):
    assert backend._recording_target("/tmp/a.gif")[1] == "gif"
    assert backend._recording_target("/tmp/a.MP4")[1] == "mp4"


def test_recording_target_rejects_other_extensions(backend):
    with pytest.raises(ValueError, match=r"\.gif or \.mp4"):
        backend._recording_target("/tmp/a.webm")


def test_export_recording_mp4_moves_without_ffmpeg(backend, tmp_path):
    src = tmp_path / "bam-rec-x.mp4"
    src.write_bytes(b"not really video")
    dst = tmp_path / "out.mp4"
    out = backend._export_recording(str(src), str(dst), "mp4")
    assert out == str(dst)
    assert dst.read_bytes() == b"not really video"
    assert not src.exists()


def test_spawn_recorder_fails_fast_on_bad_command(backend, tmp_path):
    video = tmp_path / "bam-rec-x.mp4"
    # A command that exits immediately stands in for ffmpeg rejecting its device.
    with pytest.raises(RuntimeError, match="before recording started"):
        backend._spawn_recorder(["sh", "-c", "echo device busted >&2; exit 3"], str(video))
    assert not (tmp_path / "bam-rec-x.mp4.log").exists()  # log consumed into the error


def test_finish_recorder_raises_when_no_capture_file(backend, tmp_path):
    import subprocess

    video = tmp_path / "bam-rec-y.mp4"
    log = tmp_path / "bam-rec-y.mp4.log"
    log.write_text("Input/output error\n")
    proc = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    with pytest.raises(RuntimeError, match="Input/output error"):
        backend._finish_recorder((proc, str(video), str(log)))
