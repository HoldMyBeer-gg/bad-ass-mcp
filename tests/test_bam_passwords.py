"""Typing into password fields is blocked unless the runner's own config
allows it. A script (or its `configuration` block) can never allow it."""

from __future__ import annotations

import json

import pytest

from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.bam.suite import effective_config
from bad_ass_mcp.bam.validate import validate_script
from tests.fake_backend import FakeBackend

CONFIG = {"window": "Fake App", "screenshot_proof": False, "step_timeout": 0.2}
BLOCKED = "password field: not allowed by this machine's config"


class _Login(FakeBackend):
    """The "Name" field is a password box."""

    def is_password(self, handle_id):
        self._resolve(handle_id)
        return handle_id == "txt-name"


SCRIPT = {
    "name": "login",
    "platforms": ["linux"],
    "phases": [
        {
            "learn": {"pw": {"name": "Name"}},
            # A password field only takes a secret (see test_bam_secrets).
            "steps": [{"action": "type", "target": "pw", "secret": "env:BAM_TEST_PW"}],
        }
    ],
}


@pytest.fixture(autouse=True)
def _password_env(monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", "hunter2")


def _run(tmp_path, backend, config):
    p = tmp_path / "login.json"
    p.write_text(json.dumps(SCRIPT))
    return run_script(backend, p, "linux", config, tmp_path / "out")


def test_typing_into_a_password_field_is_blocked_by_default(tmp_path):
    b = _Login()
    r = _run(tmp_path, b, CONFIG)
    assert r["status"] == "FAIL"
    assert r["failure"]["reason"] == BLOCKED
    assert b.get_value("txt-name") == ""


def test_the_runner_config_can_allow_it_everywhere(tmp_path):
    b = _Login()
    r = _run(tmp_path, b, {**CONFIG, "allow_password_fields": True})
    assert r["status"] == "PASS", r
    assert b.get_value("txt-name") == "hunter2"


def test_the_runner_config_can_allow_it_for_named_apps(tmp_path):
    ok = _run(tmp_path, _Login(), {**CONFIG, "allow_password_fields": ["fake app"]})
    assert ok["status"] == "PASS", ok
    other = _run(tmp_path, _Login(), {**CONFIG, "allow_password_fields": ["Hold My Beer"]})
    assert other["status"] == "FAIL" and other["failure"]["reason"] == BLOCKED


def test_ordinary_fields_are_not_affected(tmp_path):
    p = tmp_path / "plain.json"
    plain = {
        **SCRIPT,
        "phases": [
            {**SCRIPT["phases"][0], "steps": [{"action": "type", "target": "pw", "text": "hello"}]}
        ],
    }
    p.write_text(json.dumps(plain))
    r = run_script(FakeBackend(), p, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "PASS", r


def test_a_script_configuration_block_cannot_allow_it():
    script = {"configuration": {"allow_password_fields": True}}
    assert "allow_password_fields" not in effective_config({}, script, {})
    assert (
        effective_config({"allow_password_fields": True}, script, {})["allow_password_fields"]
        is True
    )
    issues = validate_script({**SCRIPT, **script})
    assert any("allow_password_fields" in i.path + i.message for i in issues)


def test_secret_looking_type_targets_are_flagged_before_a_run():
    from bad_ass_mcp.bam.describe import secret_hints

    script = {
        "phases": [
            {
                "learn": {
                    "pw": {"name": "Password"},
                    "user": {"name": "Username"},
                    "tok": {"name": {"linux": "API token", "windows": "Token"}},
                },
                "steps": [
                    {"action": "type", "target": "pw", "text": "x"},
                    {"action": "type", "target": "user", "text": "x"},
                    {"action": "type", "target": "tok", "text": "x"},
                    {"action": "click", "target": "pw"},
                ],
            }
        ]
    }
    assert secret_hints(script, "linux") == ["pw", "tok"]


def test_base_is_password_falls_back_to_a_masked_value():
    b = FakeBackend()
    assert b.is_password("txt-name") is None
    b._values["txt-name"] = "•••"
    assert b.is_password("txt-name") is True


def test_check_names_scripts_that_type_into_secret_looking_fields(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import check_main

    script = {
        "name": "login",
        "platforms": ["linux", "macos", "windows"],
        "phases": [
            {
                "learn": {"pw": {"name": "Password"}},
                "steps": [{"action": "type", "target": "pw", "text": "x"}],
            }
        ],
    }
    p = tmp_path / "login.json"
    p.write_text(json.dumps(script))
    assert check_main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "password" in out.lower() and "login (pw)" in out
