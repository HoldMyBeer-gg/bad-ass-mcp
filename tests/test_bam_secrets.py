"""Secret values: a type step's "secret": "env:NAME" is read at run time and
never written anywhere a run leaves behind."""

from __future__ import annotations

import json

from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.bam.validate import validate_script
from tests.fake_backend import FakeBackend

CONFIG = {"window": "Fake App", "screenshot_proof": False, "step_timeout": 0.2}
VALUE = "s3cr3t-Pa55"


class _Login(FakeBackend):
    def is_password(self, handle_id):
        self._resolve(handle_id)
        return handle_id == "txt-name"


def _script(tmp_path, steps, name="sec"):
    p = tmp_path / f"{name}.json"
    p.write_text(
        json.dumps(
            {
                "name": name,
                "platforms": ["linux"],
                "phases": [{"learn": {"f": {"name": "Name"}}, "steps": steps}],
            }
        )
    )
    return p


def _everything_written(out):
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in out.rglob("*.json"))


PLAIN_OK = {**CONFIG, "allow_secrets_in_plain_fields": True}


def test_a_secret_is_typed_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    b = FakeBackend()
    p = _script(tmp_path, [{"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"}])
    r = run_script(b, p, "linux", PLAIN_OK, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert b.get_value("txt-name") == VALUE


def test_a_secret_never_reaches_the_results_even_in_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    p = _script(
        tmp_path,
        [
            {"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"},
            # Fails, and the failure message quotes the field's value.
            {"action": "get_value", "target": "f", "expect": "something else"},
        ],
    )
    out = tmp_path / "out"
    r = run_script(FakeBackend(), p, "linux", PLAIN_OK, out)
    assert r["status"] == "FAIL"
    assert VALUE not in json.dumps(r)
    assert "•••" in r["failure"]["reason"]
    assert VALUE not in _everything_written(out)


def test_a_missing_variable_fails_before_typing(tmp_path, monkeypatch):
    monkeypatch.delenv("BAM_NOT_SET", raising=False)
    b = FakeBackend()
    p = _script(tmp_path, [{"action": "type", "target": "f", "secret": "env:BAM_NOT_SET"}])
    r = run_script(b, p, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert r["failure"]["reason"] == "secret env:BAM_NOT_SET is not set"
    assert b.get_value("txt-name") == ""


def test_a_password_field_takes_a_secret_not_literal_text(tmp_path, monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    allow = {**CONFIG, "allow_password_fields": True}
    literal = _script(tmp_path, [{"action": "type", "target": "f", "text": "hunter2"}], "lit")
    r = run_script(_Login(), literal, "linux", allow, tmp_path / "a")
    assert r["status"] == "FAIL"
    assert r["failure"]["reason"] == 'password field: use "secret", not literal text'
    ok = _script(tmp_path, [{"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"}], "ok")
    r = run_script(_Login(), ok, "linux", allow, tmp_path / "b")
    assert r["status"] == "PASS", r


def test_a_secret_into_a_password_field_still_needs_the_machine_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    p = _script(tmp_path, [{"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"}])
    r = run_script(_Login(), p, "linux", CONFIG, tmp_path / "out")
    assert r["failure"]["reason"] == "password field: not allowed by this machine's config"


def test_schema_takes_text_or_secret_but_not_both():
    def script(step):
        return {
            "name": "s",
            "platforms": ["linux"],
            "phases": [{"learn": {"f": {"name": "Name"}}, "steps": [step]}],
        }

    assert validate_script(script({"action": "type", "target": "f", "secret": "env:PW"})) == []
    assert validate_script(script({"action": "type", "target": "f", "text": "x"})) == []
    both = {"action": "type", "target": "f", "text": "x", "secret": "env:PW"}
    assert validate_script(script(both))
    assert validate_script(script({"action": "type", "target": "f"}))
    assert validate_script(script({"action": "type", "target": "f", "secret": "PW"}))


def test_a_secret_into_a_plain_field_is_refused_by_default(tmp_path, monkeypatch):
    # An unmasked field shows the value on screen: screenshots and video
    # would carry it, and redacting the logs can't reach pixels.
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    b = FakeBackend()
    p = _script(tmp_path, [{"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"}])
    r = run_script(b, p, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert r["failure"]["reason"].startswith("secret into a field that isn't a password field")
    assert b.get_value("txt-name") == ""


def test_once_a_secret_is_on_screen_no_pictures_are_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("BAM_TEST_PW", VALUE)
    p = tmp_path / "two.json"
    p.write_text(
        json.dumps(
            {
                "name": "two",
                "platforms": ["linux"],
                "phases": [
                    {"learn": {"ok": {"name": "OK"}}, "steps": []},
                    {
                        "learn": {"f": {"name": "Name"}},
                        "steps": [
                            {"action": "type", "target": "f", "secret": "env:BAM_TEST_PW"},
                            {"action": "get_value", "target": "f", "expect": "nope"},
                        ],
                    },
                ],
            }
        )
    )
    out = tmp_path / "out"
    cfg = {**PLAIN_OK, "screenshot_proof": True, "record_video": True}
    r = run_script(FakeBackend(), p, "linux", cfg, out)
    assert r["status"] == "FAIL"
    # Phase 1 ran before the secret was typed: its proof is kept.
    assert (out / "phase-1.png").exists()
    assert not (out / "fail.png").exists() and "fail_screenshot" not in r
    assert "video" not in r and not (out / "run.mp4").exists()
    assert "secret" in r.get("pictures_withheld", "")


def test_allow_secrets_in_plain_fields_is_runner_only():
    from bad_ass_mcp.bam.suite import effective_config

    script = {"configuration": {"allow_secrets_in_plain_fields": True}}
    assert "allow_secrets_in_plain_fields" not in effective_config({}, script, {})
