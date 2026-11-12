"""BAM schema: scripts and configs are validated before anything runs, with
the path to each problem and a "did you mean" for near-miss keys and values."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bad_ass_mcp.bam.validate import validate_config, validate_script

ROOT = Path(__file__).resolve().parent.parent
SMOKE = sorted((Path(__file__).parent / "fixtures" / "smoke").glob("*.json"))


def script(**phase_or_top):
    base = {"name": "t", "platforms": ["linux"], "phases": []}
    return {**base, **phase_or_top}


def one_phase(**phase):
    return script(phases=[phase])


def messages(issues):
    return [f"{i.path}: {i.message}" for i in issues]


# ── real scripts are valid ───────────────────────────────────────────


@pytest.mark.parametrize("path", SMOKE, ids=lambda p: p.stem)
def test_real_smoke_scripts_are_valid(path):
    assert messages(validate_script(json.loads(path.read_text(encoding="utf-8")))) == []


def test_the_examples_in_the_docs_are_valid():
    doc = (ROOT / "docs" / "bam.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.S)]
    scripts = [b for b in blocks if "phases" in b]
    assert scripts, "docs/bam.md should carry at least one full script example"
    for s in scripts:
        assert messages(validate_script(s)) == []


def test_a_minimal_script_is_valid():
    assert validate_script(script()) == []


# ── keys ─────────────────────────────────────────────────────────────


def test_a_misspelled_step_key_names_the_path_and_suggests_the_fix():
    s = one_phase(
        learn={"emoji": {"name": "Insert emoji"}},
        steps=[{"action": "click", "taget": "emoji"}],
    )
    assert messages(validate_script(s)) == [
        "phases[0].steps[0]: unknown key 'taget' (did you mean 'target'?)"
    ]


def test_an_unknown_action_suggests_the_closest_one():
    s = one_phase(steps=[{"action": "clik"}])
    assert messages(validate_script(s)) == [
        "phases[0].steps[0].action: unknown action 'clik' (did you mean 'click'?)"
    ]


def test_a_key_that_belongs_to_another_action_is_rejected():
    s = one_phase(steps=[{"action": "sleep", "seconds": 1, "text": "hi"}])
    assert messages(validate_script(s)) == ["phases[0].steps[0]: unknown key 'text'"]


def test_a_missing_required_field_is_named():
    s = one_phase(learn={"n": {"name": "Name"}}, steps=[{"action": "type", "target": "n"}])
    assert messages(validate_script(s)) == ["phases[0].steps[0]: needs one of 'text', 'secret'"]


def test_wrong_types_say_what_was_expected():
    s = one_phase(steps=[{"action": "sleep", "seconds": "two"}])
    assert messages(validate_script(s)) == ["phases[0].steps[0].seconds: should be a number"]


def test_a_misspelled_platform_in_a_per_platform_value_is_caught():
    s = one_phase(learn={"x": {"name": {"maocs": "Settings…", "linux": "Settings"}}})
    assert messages(validate_script(s)) == [
        "phases[0].learn.x.name: unknown key 'maocs' (did you mean 'macos'?)"
    ]


def test_unknown_top_level_and_check_keys_are_caught():
    s = script(platfroms=["linux"], checks=[{"presnt": {"name": "OK"}}])
    assert messages(validate_script(s)) == [
        "(script): unknown key 'platfroms' (did you mean 'platforms'?)",
        "checks[0]: unknown key 'presnt' (did you mean 'present'?)",
    ]


def test_notes_for_people_are_allowed_everywhere():
    s = one_phase(
        note="why",
        learn={"x": {"name": "X", "note": "why"}},
        steps=[{"action": "click", "target": "x", "note": "why"}],
        checks=[{"present": {"name": "X"}, "note": "why"}],
    )
    assert validate_script({**s, "notes": "why", "covers": "what"}) == []


# ── meaning, not just shape ──────────────────────────────────────────


def test_a_target_must_be_learned_in_the_same_phase():
    s = one_phase(
        learn={"search": {"name": "Search emoji"}},
        steps=[{"action": "type", "target": "serch", "text": "crab"}],
    )
    assert messages(validate_script(s)) == [
        "phases[0].steps[0].target: 'serch' isn't learned in this phase (did you mean 'search'?)"
    ]


def test_repeat_must_name_an_earlier_phase_id():
    s = script(phases=[{"id": "open", "learn": {}, "steps": []}, {"repeat": "opn"}])
    assert messages(validate_script(s)) == [
        "phases[1].repeat: no earlier phase with id 'opn' (did you mean 'open'?)"
    ]


# ── configs ──────────────────────────────────────────────────────────


def test_a_good_config_is_valid():
    cfg = {
        "window": "MarkTheCrab",
        "app": {"macos": "/Applications/MarkTheCrab.app/Contents/MacOS/markthecrab"},
        "ready": {"name": "Insert emoji"},
        "launch_timeout": 30,
        "record_video": "on_failure",
    }
    assert validate_config(cfg) == []


def test_config_typos_and_bad_values_are_caught():
    cfg = {"window": "X", "step_timout": 5, "record_video": "sometimes"}
    assert messages(validate_config(cfg)) == [
        "(config): unknown key 'step_timout' (did you mean 'step_timeout'?)",
        "record_video: should be one of true, false, 'on_failure'",
    ]


def test_a_script_configuration_block_is_checked_as_config():
    s = script(configuration={"screenshot_prof": False})
    assert messages(validate_script(s)) == [
        "configuration: unknown key 'screenshot_prof' (did you mean 'screenshot_proof'?)"
    ]


# ── the schema stays in step with the runner ─────────────────────────


def test_every_runner_only_step_key_is_in_the_schema():
    from bad_ass_mcp.bam.script import _RUNNER_KEYS
    from bad_ass_mcp.bam.validate import step_keys

    allowed = set().union(*step_keys().values())
    assert _RUNNER_KEYS - {"confirm"} <= allowed


def test_every_backend_action_is_in_the_schema():
    from bad_ass_mcp.bam.validate import step_keys

    src = (ROOT / "src" / "bad_ass_mcp" / "backend" / "base.py").read_text(encoding="utf-8")
    backend_actions = set(re.findall(r'action == "([a-z_]+)"', src))
    assert backend_actions <= set(step_keys())


# ── command line ─────────────────────────────────────────────────────


def test_check_command_reports_problems_and_exit_status(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import check_main

    good = tmp_path / "good.json"
    good.write_text(json.dumps(script()))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(one_phase(steps=[{"action": "clik"}])))

    assert check_main([str(good)]) == 0
    assert check_main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "bad.json: phases[0].steps[0].action: unknown action 'clik'" in out

    assert check_main([str(bad), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data == [
        {
            "file": str(bad),
            "path": "phases[0].steps[0].action",
            "message": "unknown action 'clik' (did you mean 'click'?)",
        }
    ]


def test_check_reports_unreadable_json(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import check_main

    broken = tmp_path / "broken.json"
    broken.write_text("{ nope")
    assert check_main([str(broken)]) == 1
    assert "broken.json: not valid JSON" in capsys.readouterr().out


def test_run_refuses_invalid_scripts_before_touching_the_desktop(tmp_path, monkeypatch, capsys):
    from bad_ass_mcp.bam import cli

    def boom(*a, **k):
        raise AssertionError("run_suite must not start")

    monkeypatch.setattr("bad_ass_mcp.bam.suite.run_suite", boom)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(one_phase(steps=[{"action": "clik"}])))
    assert cli.main([str(bad)]) == 2
    assert "unknown action 'clik'" in capsys.readouterr().out


def test_shell_step_cmd_may_be_per_platform():
    """The runner picks a per-platform cmd like any other step value; the
    schema rejected it as 'should be a string'."""
    s = one_phase(
        name="p",
        steps=[
            {
                "action": "shell",
                "cmd": {"linux": 'test -s "$SMOKE/a.png"', "windows": "powershell.exe -c 1"},
            }
        ],
    )
    assert messages(validate_script(s)) == []
