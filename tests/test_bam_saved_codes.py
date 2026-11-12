"""A setup shell entry can save its output (a pairing or MFA code) for later
steps, only when this machine's config sets allow_temp_saving_mfa_codes.
The value lives in memory for the run and never reaches any output."""

from __future__ import annotations

import json

from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.bam.suite import effective_config
from bad_ass_mcp.bam.validate import validate_script
from tests.fake_backend import FakeBackend

CONFIG = {"window": "Fake App", "screenshot_proof": True, "step_timeout": 0.2}
ALLOW = {**CONFIG, "allow_temp_saving_mfa_codes": True}
CODE = "829461"


def _script(tmp_path, setup, steps):
    p = tmp_path / "pair.json"
    p.write_text(
        json.dumps(
            {
                "name": "pair",
                "platforms": ["linux"],
                "setup": setup,
                "phases": [{"learn": {"f": {"name": "Name"}}, "steps": steps}],
            }
        )
    )
    return p


TYPE = [{"action": "type", "target": "f", "text": "$code"}]


def test_a_saved_code_is_typed_by_a_later_step(tmp_path):
    b = FakeBackend()
    p = _script(tmp_path, [{"shell": f"echo ' {CODE} '", "save": "code"}], TYPE)
    r = run_script(b, p, "linux", ALLOW, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert b.get_value("txt-name") == CODE


def test_saving_a_code_needs_the_machine_config(tmp_path):
    marker = tmp_path / "ran"
    p = _script(tmp_path, [{"shell": f"touch {marker}; echo {CODE}", "save": "code"}], TYPE)
    b = FakeBackend()
    r = run_script(b, p, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert "allow_temp_saving_mfa_codes" in r["failure"]["reason"]
    assert not marker.exists()  # refused before the command ran
    assert b.get_value("txt-name") == ""


def test_an_empty_output_fails_setup(tmp_path):
    p = _script(tmp_path, [{"shell": "true", "save": "code"}], TYPE)
    r = run_script(FakeBackend(), p, "linux", ALLOW, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert "code" in r["failure"]["reason"] and "nothing" in r["failure"]["reason"]


def test_a_saved_code_never_reaches_the_results(tmp_path):
    p = _script(
        tmp_path,
        [{"shell": f"echo {CODE}", "save": "code"}],
        TYPE + [{"action": "get_value", "target": "f", "expect": "nope"}],
    )
    out = tmp_path / "out"
    r = run_script(FakeBackend(), p, "linux", ALLOW, out)
    assert r["status"] == "FAIL"
    assert CODE not in json.dumps(r)
    written = "".join(f.read_text(errors="replace") for f in out.rglob("*.json"))
    assert CODE not in written
    # One-time codes: screenshots are kept.
    assert (out / "fail.png").exists()


def test_the_permission_is_runner_only():
    script = {"configuration": {"allow_temp_saving_mfa_codes": True}}
    assert "allow_temp_saving_mfa_codes" not in effective_config({}, script, {})
    issues = validate_script({"name": "x", "platforms": ["linux"], "phases": [], **script})
    assert any("allow_temp_saving_mfa_codes" in i.path for i in issues)


def test_save_is_valid_on_a_setup_shell_entry():
    s = {
        "name": "x",
        "platforms": ["linux"],
        "phases": [],
        "setup": [{"shell": "echo 1", "save": "code"}],
    }
    assert validate_script(s) == []
    s["setup"][0]["save"] = "not a name"
    assert validate_script(s)
