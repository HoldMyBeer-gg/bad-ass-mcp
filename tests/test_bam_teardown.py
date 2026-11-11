"""Script-level teardown: best-effort cleanup that runs after the last phase
and after a failure, before focus goes back.

From rusty-macos-crab's Audition demo: a failed run left the Open dialog up
and the next run tripped over it. In attach mode nothing cleaned up what the
script opened."""

from __future__ import annotations

import json

from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.bam.validate import validate_script
from tests.fake_backend import FakeBackend

CONFIG = {"window": "Fake App", "screenshot_proof": False, "step_timeout": 0.2}


def write(tmp_path, script):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"name": "t", "platforms": ["linux"], **script}), encoding="utf-8")
    return p


ESCAPE = {"action": "key", "key": "Escape", "window_id": "$WINDOW"}


def test_teardown_runs_after_a_passing_script(tmp_path):
    desk = FakeBackend()
    res = run_script(
        desk,
        write(tmp_path, {"phases": [], "teardown": [ESCAPE]}),
        "linux",
        CONFIG,
        tmp_path / "out",
    )
    assert res["status"] == "PASS"
    assert desk.key_log == ["Escape"]
    assert res["teardown"] == [{"step": 1, "ok": True}]


def test_teardown_runs_after_a_failure_and_keeps_the_failure(tmp_path):
    desk = FakeBackend()
    failing = {"name": "p", "learn": {"x": {"name": "Nope"}}, "steps": []}
    res = run_script(
        desk,
        write(tmp_path, {"phases": [failing], "teardown": [ESCAPE, ESCAPE]}),
        "linux",
        CONFIG,
        tmp_path / "out",
    )
    assert res["status"] == "FAIL"
    assert res["failure"]["action"] == "learn"
    assert desk.key_log == ["Escape", "Escape"]


def test_a_failing_teardown_step_never_fails_the_run(tmp_path):
    desk = FakeBackend()
    bad = {"action": "click", "handle": "no-such-handle"}
    res = run_script(
        desk,
        write(tmp_path, {"phases": [], "teardown": [bad, ESCAPE]}),
        "linux",
        CONFIG,
        tmp_path / "out",
    )
    assert res["status"] == "PASS"
    assert res["teardown"][0]["ok"] is False and res["teardown"][0]["error"]
    assert res["teardown"][1] == {"step": 2, "ok": True}  # best-effort: keeps going


def test_shell_teardown_entries_run(tmp_path):
    marker = tmp_path / "cleaned"
    res = run_script(
        FakeBackend(),
        write(tmp_path, {"phases": [], "teardown": [{"shell": f"touch '{marker}'"}]}),
        "linux",
        CONFIG,
        tmp_path / "out",
    )
    assert res["status"] == "PASS" and marker.exists()


def test_teardown_entries_for_other_platforms_are_skipped(tmp_path):
    desk = FakeBackend()
    res = run_script(
        desk,
        write(tmp_path, {"phases": [], "teardown": [{**ESCAPE, "only": ["macos"]}]}),
        "linux",
        CONFIG,
        tmp_path / "out",
    )
    assert desk.key_log == [] and "teardown" not in res


def test_check_accepts_teardown_steps_and_shell():
    s = {
        "name": "t",
        "platforms": ["macos"],
        "phases": [],
        "teardown": [
            ESCAPE,
            {"action": "key", "key": "Cmd+W", "window_id": "$WINDOW"},
            {"shell": "true"},
        ],
    }
    assert validate_script(s) == []


def test_check_rejects_learned_targets_in_teardown():
    s = {
        "name": "t",
        "platforms": ["macos"],
        "phases": [],
        "teardown": [{"action": "click", "target": "cancel"}],
    }
    issues = validate_script(s)
    assert any(i.path == "teardown[0].target" for i in issues)


def test_real_input_in_teardown_counts_as_needing_the_foreground():
    from bad_ass_mcp.bam.describe import needs_foreground

    s = {"phases": [], "teardown": [{"action": "key", "key": "Escape"}]}
    assert needs_foreground(s, "linux")
    assert not needs_foreground({"phases": [], "teardown": [{"shell": "true"}]}, "linux")
