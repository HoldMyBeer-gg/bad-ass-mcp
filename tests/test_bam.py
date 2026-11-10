"""BAM runner: scripted end-to-end flows over the accessibility tree.

Runs against the in-memory FakeBackend (one window, "Fake App": OK/Cancel
buttons, an editable "Name" text field, a "Size" combobox).
"""

from __future__ import annotations

import json

import pytest

from bad_ass_mcp.bam.report import write_report
from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.bam.script import build_steps, learn_descriptors, pick
from bad_ass_mcp.bam.suite import run_suite
from tests.fake_backend import FakeBackend

# Short step_timeout: deliberate "missing element" tests shouldn't wait out
# the real default while the runner retries.
CONFIG = {"window": "Fake App", "screenshot_proof": True, "step_timeout": 0.2}


def write(tmp_path, name, script):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"name": name, "platforms": ["linux"], **script}))
    return p


# ── script resolution ────────────────────────────────────────────────


def test_pick_resolves_per_platform_values():
    assert pick("plain", "linux") == "plain"
    assert pick({"windows": "w", "linux": "l"}, "linux") == "l"
    assert pick({"windows": "w"}, "linux") is None


def test_learn_descriptors_resolve_platform_values_and_skip_other_platforms():
    learn = {
        "a": {"name": "OK"},
        "b": {"name": {"linux": "Name", "windows": "File name:"}, "role": {"windows": "edit"}},
        "c": {"name": "Only on Windows", "only": ["windows"]},
        "d": {"name": "X", "confirm": True},
    }
    assert learn_descriptors(learn, "linux") == {
        "a": {"name": "OK"},
        "b": {"name": "Name"},
        "d": {"name": "X"},
    }


def test_build_steps_substitutes_handles_and_drops_runner_fields():
    steps = [
        {"action": "click", "target": "ok"},
        {"action": "key", "key": "Escape", "window_id": "$WINDOW", "only": ["linux"]},
        {"action": "key", "key": "Escape", "only": ["windows"]},
        {
            "action": "get_value",
            "target": "name",
            "expect": "$before",
            "save": "x",
            "confirm": True,
        },
        {"action": "type", "target": "name", "text": "$SMOKE/a.md"},
    ]
    sent, meta = build_steps(
        steps, "linux", {"ok": "h1", "name": "h2"}, "win-7", {"SMOKE": "/tmp/s"}
    )
    assert sent == [
        {"action": "click", "handle": "h1"},
        {"action": "key", "key": "Escape", "window_id": "win-7"},
        {"action": "get_value", "handle": "h2"},
        {"action": "type", "handle": "h2", "text": "/tmp/s/a.md"},
    ]
    assert meta[2] == {"expect": "$before", "save": "x"}


# ── one script ───────────────────────────────────────────────────────


def test_a_passing_script_saves_proof_screenshots_per_phase(tmp_path):
    script = write(
        tmp_path,
        "fill-name",
        {
            "phases": [
                {
                    "learn": {"name": {"name": "Name"}},
                    "steps": [
                        {"action": "type", "target": "name", "text": "crab"},
                    ],
                },
                {
                    "learn": {"name": {"name": "Name"}, "ok": {"name": "OK"}},
                    "steps": [
                        {"action": "get_value", "target": "name", "expect": "crab"},
                        {"action": "click", "target": "ok"},
                    ],
                },
            ],
            "checks": [{"present": {"name": "OK"}}, {"absent": {"name": "Nope"}}],
        },
    )
    out = tmp_path / "out"
    r = run_script(FakeBackend(), script, "linux", CONFIG, out)
    assert r["status"] == "PASS", r
    assert (out / "phase-1.png").exists() and (out / "phase-2.png").exists()
    assert json.loads((out / "result.json").read_text())["status"] == "PASS"


def test_a_missing_element_fails_with_where_and_evidence(tmp_path):
    script = write(
        tmp_path,
        "missing",
        {
            "phases": [{"learn": {"ghost": {"name": "Ghost button"}}, "steps": []}],
        },
    )
    out = tmp_path / "out"
    r = run_script(FakeBackend(), script, "linux", CONFIG, out)
    assert r["status"] == "FAIL"
    assert r["failure"]["phase"] == 1
    assert "Ghost button" in r["failure"]["reason"]
    assert (out / "fail.png").exists()
    assert "Fake App" in (out / "tree.json").read_text()


def test_expect_mismatch_and_saved_values(tmp_path):
    script = write(
        tmp_path,
        "values",
        {
            "phases": [
                {
                    "learn": {"size": {"name": "Size"}},
                    "steps": [
                        {"action": "get_value", "target": "size", "save": "before"},
                        {"action": "select", "target": "size", "value": "Large"},
                        {"action": "get_value", "target": "size", "expect": "$before"},
                    ],
                }
            ],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert "Medium" in r["failure"]["reason"] and "Large" in r["failure"]["reason"]


def test_a_failing_action_stops_the_script(tmp_path):
    script = write(
        tmp_path,
        "bad-select",
        {
            "phases": [
                {
                    "learn": {"size": {"name": "Size"}},
                    "steps": [
                        {"action": "select", "target": "size", "value": "Gigantic"},
                    ],
                }
            ],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL" and "Gigantic" in r["failure"]["reason"]


def test_setup_shell_and_shell_checks_see_SMOKE(tmp_path):
    script = write(
        tmp_path,
        "files",
        {
            "setup": [{"shell": 'printf hi > "$SMOKE/a.txt"'}],
            "phases": [],
            "checks": [{"shell": 'grep -q hi "$SMOKE/a.txt"'}],
        },
    )
    assert run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")["status"] == "PASS"


def test_manual_checks_are_reported_not_passed(tmp_path):
    script = write(tmp_path, "manual", {"phases": [], "checks": [{"manual": "look at it"}]})
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "MANUAL" and r["manual"] == ["look at it"]


def test_repeat_reruns_a_named_phase_with_fresh_handles(tmp_path):
    backend = FakeBackend()
    script = write(
        tmp_path,
        "repeat",
        {
            "phases": [
                {
                    "id": "press",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "key", "key": "a"}],
                },
                {"repeat": "press"},
            ],
        },
    )
    assert run_script(backend, script, "linux", CONFIG, tmp_path / "out")["status"] == "PASS"
    assert backend.key_log == ["a", "a"]


def test_scripts_for_other_platforms_are_skipped(tmp_path):
    script = write(tmp_path, "win-only", {"platforms": ["windows"], "phases": []})
    assert run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")["status"] == "SKIP"


def test_window_count_and_no_window_checks(tmp_path):
    script = write(
        tmp_path,
        "windows",
        {
            "phases": [],
            "checks": [{"windows": 1}, {"no_window": "Unsaved changes"}],
        },
    )
    assert run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")["status"] == "PASS"


# ── suite: results folder, resume, fail-fast, report ─────────────────


def suite_scripts(tmp_path):
    good = write(tmp_path, "a-good", {"phases": [], "checks": [{"present": {"name": "OK"}}]})
    bad = write(tmp_path, "b-bad", {"phases": [{"learn": {"g": {"name": "Ghost"}}, "steps": []}]})
    also = write(
        tmp_path, "c-also-good", {"phases": [], "checks": [{"present": {"name": "Cancel"}}]}
    )
    return [good, bad, also]


def test_suite_writes_numbered_folders_and_run_json(tmp_path):
    run_dir = run_suite(
        FakeBackend(),
        suite_scripts(tmp_path),
        "linux",
        CONFIG,
        tmp_path / "results",
        commit="abc123",
    )
    run = json.loads((run_dir / "run.json").read_text())
    assert run["commit"] == "abc123" and run["platform"] == "linux"
    assert [(s["name"], s["status"]) for s in run["scripts"]] == [
        ("a-good", "PASS"),
        ("b-bad", "FAIL"),
        ("c-also-good", "PASS"),
    ]
    assert (run_dir / "01_a-good" / "result.json").exists()
    assert (run_dir / "02_b-bad" / "fail.png").exists()


def test_fail_fast_stops_after_the_first_failure(tmp_path):
    cfg = {**CONFIG, "fail_fast": True}
    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", cfg, tmp_path / "results", commit="abc"
    )
    statuses = [s["status"] for s in json.loads((run_dir / "run.json").read_text())["scripts"]]
    assert statuses == ["PASS", "FAIL", "NOT_RUN"]


def test_first_script_fail_fast_only_stops_when_the_first_one_fails(tmp_path):
    cfg = {**CONFIG, "fail_fast": "first_script"}
    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", cfg, tmp_path / "results", commit="abc"
    )
    statuses = [s["status"] for s in json.loads((run_dir / "run.json").read_text())["scripts"]]
    assert statuses == ["PASS", "FAIL", "PASS"]


def test_resume_reruns_only_what_did_not_pass_on_the_same_commit(tmp_path):
    scripts = suite_scripts(tmp_path)
    results = tmp_path / "results"
    first = run_suite(FakeBackend(), scripts, "linux", CONFIG, results, commit="abc")
    backend = FakeBackend()
    calls: list[str] = []
    real_find = backend.find_elements
    backend.find_elements = lambda *a, **k: calls.append(k.get("name")) or real_find(*a, **k)
    again = run_suite(backend, scripts, "linux", CONFIG, results, commit="abc", resume=True)
    assert again == first
    assert "OK" not in calls and "Cancel" not in calls  # passes kept
    assert "Ghost" in calls  # the failure re-ran


def test_resume_starts_over_on_a_new_commit(tmp_path):
    scripts = suite_scripts(tmp_path)
    results = tmp_path / "results"
    first = run_suite(FakeBackend(), scripts, "linux", CONFIG, results, commit="abc")
    second = run_suite(FakeBackend(), scripts, "linux", CONFIG, results, commit="def", resume=True)
    assert second != first


def test_retries_mark_a_flaky_pass(tmp_path):
    backend = FakeBackend()
    attempts = {"n": 0}
    real_find = backend.find_elements

    def flaky(*a, **k):
        if k.get("name") == "OK":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return []
        return real_find(*a, **k)

    backend.find_elements = flaky
    script = write(tmp_path, "flaky", {"phases": [{"learn": {"ok": {"name": "OK"}}, "steps": []}]})
    run_dir = run_suite(
        backend,
        [script],
        "linux",
        {**CONFIG, "retries": 1, "step_timeout": 0},
        tmp_path / "results",
        commit="abc",
    )
    s = json.loads((run_dir / "run.json").read_text())["scripts"][0]
    assert s["status"] == "PASS" and s["attempts"] == 2


def test_report_shows_green_passes_and_the_red_failure(tmp_path):
    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    html = write_report(run_dir).read_text()
    assert html.count('class="card pass"') == 2
    assert html.count('class="card fail"') == 1
    assert "02_b-bad/fail.png" in html
    assert "Ghost" in html
    assert "abc" in html


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_script_names_cannot_escape_the_run_folder(tmp_path, bad):
    script = tmp_path / "x.json"
    script.write_text(json.dumps({"name": bad, "platforms": ["linux"], "phases": []}))
    run_dir = run_suite(
        FakeBackend(), [script], "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    for p in run_dir.rglob("*"):
        assert run_dir in p.resolve().parents or p.resolve() == run_dir


# ── configuration precedence and the CLI ─────────────────────────────


def test_config_precedence_suite_then_script_then_cli(tmp_path):
    from bad_ass_mcp.bam.suite import effective_config

    suite = {"window": "Fake App", "screenshot_proof": True, "retries": 0, "record_video": False}
    script = {"configuration": {"screenshot_proof": False, "retries": 2}}
    cli = {"retries": 5}
    assert effective_config(suite, script, cli) == {
        "window": "Fake App",
        "screenshot_proof": False,
        "retries": 5,
        "record_video": False,
    }


def test_a_script_can_turn_off_proof_screenshots(tmp_path):
    script = write(
        tmp_path,
        "quiet",
        {
            "configuration": {"screenshot_proof": False},
            "phases": [{"learn": {"ok": {"name": "OK"}}, "steps": []}],
        },
    )
    run_dir = run_suite(
        FakeBackend(), [script], "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    assert not list(run_dir.rglob("phase-*.png"))


def test_cli_collects_scripts_and_builds_overrides(tmp_path):
    from bad_ass_mcp.bam.cli import parse

    (tmp_path / "smoke").mkdir()
    for n in ("b", "a"):
        (tmp_path / "smoke" / f"{n}.json").write_text("{}")
    (tmp_path / "smoke" / "README.md").write_text("")
    cfg = tmp_path / "bam.json"
    cfg.write_text(json.dumps({"window": "MarkTheCrab", "retries": 1}))

    args = parse(
        [
            str(tmp_path / "smoke"),
            "--config",
            str(cfg),
            "--fail-fast",
            "first_script",
            "--video",
            "on_failure",
            "--no-screenshots",
            "--platform",
            "linux",
        ]
    )
    assert [p.name for p in args.scripts] == ["a.json", "b.json"]
    assert args.suite_config == {"window": "MarkTheCrab", "retries": 1}
    assert args.overrides == {
        "fail_fast": "first_script",
        "record_video": "on_failure",
        "screenshot_proof": False,
    }
    assert args.platform == "linux"


# ── result details: names, timestamps, durations ─────────────────────


def test_results_record_timestamps_and_per_phase_durations(tmp_path):
    script = write(
        tmp_path,
        "timed",
        {
            "phases": [
                {
                    "name": "open settings",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "click", "target": "ok"}],
                },
                {"learn": {"g": {"name": "Ghost"}}, "steps": []},
            ],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["started_at"] <= r["finished_at"] and "T" in r["started_at"]
    assert [p["name"] for p in r["phases"]] == ["open settings", "phase 2"]
    assert [p["status"] for p in r["phases"]] == ["PASS", "FAIL"]
    assert all(isinstance(p["seconds"], float) for p in r["phases"])


def test_run_json_has_totals_and_duration(tmp_path):
    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    run = json.loads((run_dir / "run.json").read_text())
    assert run["totals"] == {"PASS": 2, "FAIL": 1}
    assert isinstance(run["seconds"], float)
    assert run["started_at"] <= run["finished_at"]
    assert all("seconds" in s for s in run["scripts"])


def test_cli_summary_shows_durations_and_totals(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import print_summary

    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    print_summary(run_dir, color=False)
    out = capsys.readouterr().out
    assert "FAIL" in out and "b-bad" in out
    assert "2 passed, 1 failed" in out
    assert "s)" in out  # each line carries its duration


def test_report_shows_phase_names_and_times(tmp_path):
    script = write(
        tmp_path,
        "timed",
        {
            "phases": [{"name": "open settings", "learn": {"ok": {"name": "OK"}}, "steps": []}],
        },
    )
    run_dir = run_suite(
        FakeBackend(), [script], "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    html = write_report(run_dir).read_text()
    assert "open settings" in html


# ── progress ─────────────────────────────────────────────────────────


def test_progress_events_count_phases_across_the_run(tmp_path):
    events = []
    run_suite(
        FakeBackend(),
        suite_scripts(tmp_path),
        "linux",
        CONFIG,
        tmp_path / "results",
        commit="abc",
        progress=events.append,
    )
    done = [(e["done"], e["total"]) for e in events if e["kind"] == "phase"]
    # scripts have 0, 1 and 0 phases; each script also counts as one unit for its checks
    assert done[-1][0] == done[-1][1]
    assert all(a <= b for (a, _), (b, _) in zip(done, done[1:]))
    assert [e["status"] for e in events if e["kind"] == "script"] == ["PASS", "FAIL", "PASS"]


def test_progress_bar_renders_percent_plain_and_in_colour():
    from bad_ass_mcp.bam.cli import render_bar

    plain = render_bar(1, 4, width=20, label="emoji-picker", color=False)
    assert "25%" in plain and "emoji-picker" in plain
    assert plain.count("█") == 5 and "\x1b[" not in plain
    full = render_bar(4, 4, width=20, color=False)
    assert "100%" in full and full.count("█") == 20
    coloured = render_bar(2, 4, width=20, color=True)
    assert "\x1b[38;2;" in coloured  # truecolor (TMS9928A palette)
    assert render_bar(0, 0, width=10, color=False).count("%") == 1  # no divide-by-zero


# ── never guess which app to drive ───────────────────────────────────


class _TwoApps(FakeBackend):
    """Two windows match the pattern: the user's own copy and a test build."""

    def list_windows(self):
        from bad_ass_mcp.types import WindowInfo

        return [
            WindowInfo(id="user-window", name="Fake App", pid=111, focused=True),
            WindowInfo(id="fake-window-1", name="Fake App", pid=9999, focused=False),
        ]


def test_attach_mode_refuses_when_several_windows_match(tmp_path):
    script = write(tmp_path, "ambiguous", {"phases": [], "checks": [{"present": {"name": "OK"}}]})
    r = run_script(_TwoApps(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert "2 windows match" in r["failure"]["reason"]


def test_a_pid_pins_the_window_in_attach_mode(tmp_path):
    script = write(tmp_path, "pinned", {"phases": [], "checks": [{"present": {"name": "OK"}}]})
    r = run_script(_TwoApps(), script, "linux", {**CONFIG, "pid": 9999}, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert r["window"] == "fake-window-1"


def test_launched_app_only_drives_its_own_windows(tmp_path, monkeypatch):
    from bad_ass_mcp.bam import runner as runner_mod

    class _Proc:
        pid = 9999

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _Proc())
    script = write(tmp_path, "launched", {"phases": [], "checks": [{"present": {"name": "OK"}}]})
    r = run_script(_TwoApps(), script, "linux", {**CONFIG, "app": "fake-app"}, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert r["window"] == "fake-window-1"  # pid 9999 is ours; the user's pid 111 is never touched


def test_report_screenshots_open_in_a_lightbox_with_prev_next_close(tmp_path):
    run_dir = run_suite(
        FakeBackend(), suite_scripts(tmp_path), "linux", CONFIG, tmp_path / "results", commit="abc"
    )
    html = write_report(run_dir).read_text()
    assert 'id="lightbox"' in html
    for label in ("Previous screenshot", "Next screenshot", "Close"):
        assert f'aria-label="{label}"' in html
    # Every screenshot link is part of the gallery, with a caption, and keeps
    # its href so it still opens the file without JavaScript.
    import re

    links = re.findall(r"<a [^>]*data-shot[^>]*>", html)
    imgs = re.findall(r'<img src="[^"]+\.png"', html)
    assert len(links) == len(imgs) > 0
    assert all('href="' in a and 'data-caption="' in a for a in links)
    assert "Escape" in html and "ArrowLeft" in html and "ArrowRight" in html


# ── format extras from the Linux runs (deck-crab) ────────────────────


def test_null_window_pattern_runs_the_phase_in_the_main_window(tmp_path):
    script = write(
        tmp_path,
        "nullwin",
        {
            "phases": [
                {
                    "window": {"windows": "Save As", "linux": None},
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [
                        {
                            "action": "wait_for_window",
                            "pattern": {"windows": "Save As", "linux": None},
                        },
                        {"action": "click", "target": "ok"},
                    ],
                }
            ],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "PASS", r


def test_learn_and_checks_can_require_a_state(tmp_path):
    # FakeBackend's "Name" field is editable; nothing there is "focused".
    ok = write(
        tmp_path,
        "state-ok",
        {
            "phases": [{"learn": {"name": {"role": "text", "state": "editable"}}, "steps": []}],
            "checks": [{"present": {"role": "text", "state": "editable"}}],
        },
    )
    assert run_script(FakeBackend(), ok, "linux", CONFIG, tmp_path / "a")["status"] == "PASS"
    missing = write(
        tmp_path,
        "state-missing",
        {
            "phases": [{"learn": {"name": {"role": "text", "state": "focused"}}, "steps": []}],
        },
    )
    r = run_script(FakeBackend(), missing, "linux", CONFIG, tmp_path / "b")
    assert r["status"] == "FAIL" and "focused" in r["failure"]["reason"]


def test_present_count_is_exact(tmp_path):
    two = write(
        tmp_path, "count", {"phases": [], "checks": [{"present": {"role": "button", "count": 2}}]}
    )
    assert run_script(FakeBackend(), two, "linux", CONFIG, tmp_path / "a")["status"] == "PASS"
    three = write(
        tmp_path, "count3", {"phases": [], "checks": [{"present": {"role": "button", "count": 3}}]}
    )
    r = run_script(FakeBackend(), three, "linux", CONFIG, tmp_path / "b")
    assert r["status"] == "FAIL" and "3" in r["failure"]["reason"] and "2" in r["failure"]["reason"]


def test_per_platform_variables_expand_SMOKE_and_reach_steps_and_shell(tmp_path):
    backend = FakeBackend()
    script = write(
        tmp_path,
        "vars",
        {
            "variables": {"target": {"windows": "$SMOKE\\\\A.MD", "linux": "$SMOKE/sub/../a.md"}},
            "variables_note": "for humans",
            "phases": [
                {
                    "learn": {"name": {"name": "Name"}},
                    "steps": [
                        {"action": "type", "target": "name", "text": "$target"},
                        {"action": "get_value", "target": "name", "save": "typed"},
                        {"action": "shell", "cmd": 'printf \'%s\' "$target" > "$SMOKE/out.txt"'},
                    ],
                }
            ],
        },
    )
    out = tmp_path / "out"
    r = run_script(backend, script, "linux", CONFIG, out)
    assert r["status"] == "PASS", r
    smoke = str(out / "files")
    assert backend._values["txt-name"] == f"{smoke}/sub/../a.md"
    assert (out / "files" / "out.txt").read_text() == f"{smoke}/sub/../a.md"


def test_a_failing_shell_step_fails_at_that_step(tmp_path):
    script = write(
        tmp_path,
        "shellfail",
        {
            "phases": [
                {
                    "learn": {},
                    "steps": [
                        {"action": "shell", "cmd": "exit 3"},
                        {"action": "key", "key": "a"},
                    ],
                }
            ],
        },
    )
    backend = FakeBackend()
    r = run_script(backend, script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL" and r["failure"]["step"] == 1
    assert backend.key_log == []  # nothing after the failed shell step ran


# ── wait for the app to be ready, not just for its window ────────────


class _SlowContent(FakeBackend):
    """The window exists at once; its content shows up `delay` s later
    (WebView2 paints after the window appears)."""

    def __init__(self, delay=0.4):
        super().__init__()
        import time as _t

        self._ready_at = _t.monotonic() + delay
        self.keys_before_ready: list[str] = []

    def _loaded(self):
        import time as _t

        return _t.monotonic() >= self._ready_at

    def find_elements(self, window_id, *, role=None, name=None, index=0):
        return super().find_elements(window_id, role=role, name=name) if self._loaded() else []

    def press_key(self, key, window_id=None):
        if not self._loaded():
            self.keys_before_ready.append(key)
        return super().press_key(key, window_id)


def test_ready_element_is_awaited_before_the_first_step(tmp_path):
    backend = _SlowContent()
    script = write(
        tmp_path,
        "early-keys",
        {
            "phases": [{"learn": {}, "steps": [{"action": "key", "key": "x"}]}],
        },
    )
    r = run_script(backend, script, "linux", {**CONFIG, "ready": {"name": "OK"}}, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert backend.keys_before_ready == []  # nothing typed into a page that isn't there yet


def test_an_app_that_never_gets_ready_fails_clearly(tmp_path):
    script = write(tmp_path, "never", {"phases": []})
    cfg = {**CONFIG, "ready": {"name": "No such thing"}, "launch_timeout": 0.3}
    r = run_script(FakeBackend(), script, "linux", cfg, tmp_path / "out")
    assert r["status"] == "FAIL"
    assert "not ready" in r["failure"]["reason"] and "No such thing" in r["failure"]["reason"]


def test_learn_retries_late_elements_until_step_timeout(tmp_path):
    script = write(tmp_path, "late", {"phases": [{"learn": {"ok": {"name": "OK"}}, "steps": []}]})
    r = run_script(
        _SlowContent(0.4), script, "linux", {**CONFIG, "step_timeout": 2}, tmp_path / "out"
    )
    assert r["status"] == "PASS", r


# ── window counting where the backend lists applications, not windows ─


class _OneAppTwoFrames(FakeBackend):
    """Linux/macOS backends list one entry per application; its windows are
    top-level children of the app's tree."""

    def get_tree(self, window_id, *, max_depth=None):
        from bad_ass_mcp.types import ElementHandle

        frame = lambda i: ElementHandle(id=f"f{i}", role="frame", name="Fake App")  # noqa: E731
        return ElementHandle(
            id="app", role="application", name="Fake App", children=[frame(1), frame(2)]
        )


def test_windows_check_counts_frames_on_linux(tmp_path):
    script = write(tmp_path, "two", {"phases": [], "checks": [{"windows": 2}]})
    r = run_script(_OneAppTwoFrames(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "PASS", r


def test_an_app_without_frame_children_still_counts_as_one_window(tmp_path):
    script = write(tmp_path, "one", {"phases": [], "checks": [{"windows": 1}]})
    assert run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")["status"] == "PASS"


# ── phases in "the other window" (Windows lists each window separately) ─


class _TwoWindowsOneApp(FakeBackend):
    def list_windows(self):
        from bad_ass_mcp.types import WindowInfo

        return [
            WindowInfo(id="main", name="Fake App", pid=9999, focused=False),
            WindowInfo(id="second", name="Fake App", pid=9999, focused=True),
        ]


def test_a_phase_can_target_the_other_matching_window(tmp_path):
    backend = _TwoWindowsOneApp()
    seen = []
    real_learn = backend.learn_layout
    backend.learn_layout = lambda win, d: seen.append(win) or real_learn(win, d)
    script = write(
        tmp_path,
        "other",
        {
            "phases": [
                {"learn": {"ok": {"name": "OK"}}, "steps": []},
                {"window": {"other": "Fake App"}, "learn": {"ok": {"name": "OK"}}, "steps": []},
            ],
        },
    )
    r = run_script(backend, script, "linux", {**CONFIG, "pid": 9999}, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert seen == ["main", "second"]


def test_other_window_fails_when_there_is_no_second_window(tmp_path):
    script = write(
        tmp_path,
        "nosecond",
        {
            "phases": [{"window": {"other": "Fake App"}, "learn": {}, "steps": []}],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "FAIL" and "other window" in r["failure"]["reason"]


# ── slow machines: one knob stretches every wait ─────────────────────


def test_timeout_scale_stretches_waits_and_sleeps(tmp_path):
    from bad_ass_mcp.bam.script import build_steps

    steps = [
        {"action": "wait_for_element", "window_id": "$WINDOW", "name": "X", "timeout": 3},
        {"action": "wait_for_window", "pattern": "Save", "timeout": 5},
        {"action": "sleep", "seconds": 0.5},
        {"action": "click", "target": "ok"},
    ]
    sent, _ = build_steps(steps, "linux", {"ok": "h"}, "W", {}, timeout_scale=3)
    assert sent[0]["timeout"] == 9
    assert sent[1]["timeout"] == 15
    assert sent[2]["seconds"] == 1.5
    assert sent[3] == {"action": "click", "handle": "h"}


def test_timeout_scale_defaults_to_one_and_reaches_the_runner(tmp_path):
    backend = FakeBackend()
    seen = []
    real = backend.run_sequence
    backend.run_sequence = lambda steps, stop_on_error=True: (
        seen.extend(steps) or real(steps, stop_on_error)
    )
    script = write(
        tmp_path,
        "scaled",
        {"phases": [{"learn": {}, "steps": [{"action": "sleep", "seconds": 0.01}]}]},
    )
    run_script(backend, script, "linux", {**CONFIG, "timeout_scale": 2}, tmp_path / "a")
    assert seen[-1]["seconds"] == 0.02


def test_drag_steps_take_targets_and_offsets(tmp_path):
    """`from_target`/`to_target` become handles; `dx`/`dy` pass through."""
    sent, meta = build_steps(
        [
            {"action": "drag", "from_target": "ok", "to_target": "size"},
            {"action": "drag", "from_target": "ok", "dx": 120, "dy": 0, "note": "splitter"},
        ],
        "linux",
        {"ok": "btn-ok", "size": "combo-size"},
        "w1",
        {},
    )
    assert sent == [
        {"action": "drag", "from_handle": "btn-ok", "to_handle": "combo-size"},
        {"action": "drag", "from_handle": "btn-ok", "dx": 120, "dy": 0},
    ]
    assert meta == [{}, {}]


def test_get_bounds_with_expect_not_proves_a_drag_moved_something(tmp_path):
    """The splitter flow: remember where the handle is, drag it, and require
    that its bounds changed. A drag that did nothing fails at that step."""
    script = write(
        tmp_path,
        "resize",
        {
            "phases": [
                {
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [
                        {"action": "get_bounds", "target": "ok", "save": "before"},
                        {"action": "drag", "from_target": "ok", "dx": 120, "dy": 0},
                        {"action": "get_bounds", "target": "ok", "expect_not": "$before"},
                    ],
                }
            ],
        },
    )
    r = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert r["status"] == "PASS", r.get("failure")

    stuck = write(
        tmp_path,
        "stuck",
        {
            "phases": [
                {
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [
                        {"action": "get_bounds", "target": "ok", "save": "before"},
                        {"action": "drag", "from_target": "ok", "dx": 0, "dy": 0},
                        {"action": "get_bounds", "target": "ok", "expect_not": "$before"},
                    ],
                }
            ],
        },
    )
    r = run_script(FakeBackend(), stuck, "linux", CONFIG, tmp_path / "out2")
    assert r["status"] == "FAIL"
    assert r["failure"]["step"] == 3
    assert "unchanged" in r["failure"]["reason"]


# ── demo mode: timeline, pacing, visible pointer ──────────────────────


def test_every_run_writes_a_timeline_of_phases(tmp_path):
    script = write(
        tmp_path,
        "tl",
        {
            "phases": [
                {
                    "name": "first",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "click", "target": "ok"}],
                },
                {"name": "second", "learn": {}, "steps": []},
            ]
        },
    )
    out = tmp_path / "out"
    run_script(FakeBackend(), script, "linux", CONFIG, out)
    events = json.loads((out / "timeline.json").read_text())
    phases = [e for e in events if e["event"] == "phase"]
    assert [p["name"] for p in phases] == ["first", "second"]
    assert phases[0]["t"] <= phases[1]["t"]


def test_demo_mode_logs_each_step_with_bounds_and_paces_it(tmp_path):
    script = write(
        tmp_path,
        "demo",
        {
            "phases": [
                {
                    "name": "press ok",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [
                        {"action": "click", "target": "ok"},
                        {"action": "key", "key": "a"},
                    ],
                }
            ]
        },
    )
    out = tmp_path / "out"
    r = run_script(FakeBackend(), script, "linux", {**CONFIG, "demo": True, "demo_pace": 0.05}, out)
    assert r["status"] == "PASS", r
    steps = [e for e in json.loads((out / "timeline.json").read_text()) if e["event"] == "step"]
    assert [s["action"] for s in steps] == ["click", "key"]
    assert steps[0]["phase"] == "press ok" and steps[0]["target"] == "ok"
    assert steps[0]["bounds"] == [10.0, 10.0, 80.0, 30.0]  # FakeBackend's OK button
    assert steps[1]["t"] - steps[0]["t"] >= 0.05  # paced


def test_demo_mode_clicks_with_the_real_pointer_at_the_element_centre(tmp_path):
    backend = FakeBackend()
    script = write(
        tmp_path,
        "pointer",
        {
            "phases": [
                {
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [
                        {"action": "click", "target": "ok"},
                    ],
                }
            ]
        },
    )
    run_script(backend, script, "linux", {**CONFIG, "demo": True, "demo_pace": 0}, tmp_path / "out")
    assert ("click", 50.0, 25.0, "left", 1) in backend.pointer_log


# ── plain-language phases: what each one does and expects ────────────


def test_describe_phase_turns_steps_and_checks_into_plain_language():
    from bad_ass_mcp.bam.describe import describe_phase

    phase = {
        "learn": {
            "emoji": {"name": "Insert emoji"},
            "search": {"name": "Search emoji"},
            "split": {"role": "separator"},
            "mac_only": {"name": "Palette", "only": ["macos"]},
        },
        "steps": [
            {"action": "click", "target": "emoji"},
            {"action": "click", "target": "emoji", "button": "right"},
            {"action": "click", "target": "emoji", "clicks": 2},
            {"action": "wait_for_element", "name": "Search emoji", "timeout": 5},
            {"action": "type", "target": "search", "text": "crab"},
            {"action": "sleep", "seconds": 0.3},
            {"action": "key", "key": "Return", "only": ["windows"]},
            {"action": "key", "key": "Escape"},
            {"action": "select", "target": "search", "value": "Arial"},
            {"action": "get_value", "target": "search", "expect": "crab"},
            {"action": "get_bounds", "target": "split", "save": "before"},
            {"action": "drag", "from_target": "split", "dx": 120, "dy": 0},
            {"action": "get_bounds", "target": "split", "expect_not": "$before"},
            {"action": "wait_for_window", "pattern": {"linux": None, "macos": "Save"}},
            {"action": "shell", "cmd": "test -f a.md"},
            {"action": "click_at", "x": 10, "y": 20},
        ],
        "checks": [
            {"present": {"name": "Done"}},
            {"present": {"name": "Tab", "count": 2}},
            {"absent": {"name": "Search emoji"}},
            {"windows": 1},
            {"no_window": "Save As"},
            {"shell": "grep -q crab a.md"},
            {"manual": "The crab looks happy"},
            {"present": {"name": "Mac thing"}, "only": ["macos"]},
        ],
    }
    d = describe_phase(phase, "linux")
    assert d["steps"] == [
        {"text": "Click “Insert emoji”"},
        {"text": "Right-click “Insert emoji”"},
        {"text": "Double-click “Insert emoji”"},
        {"text": "“Search emoji” appears", "expect": True},
        {"text": 'Type "crab" into “Search emoji”'},
        {"text": "Pause 0.3 s", "pause": True},
        {"text": "Press Escape (real key, to the front window)"},
        {"text": 'Choose "Arial" in “Search emoji”'},
        {"text": '“Search emoji” reads "crab"', "expect": True},
        {"text": "Note where the separator is"},
        {"text": "Drag the separator by (120, 0)"},
        {"text": "The separator is not where it was (saved before)", "expect": True},
        {"text": "Run `test -f a.md`"},
        {"text": "Click at (10, 20)"},
    ]
    assert d["checks"] == [
        "“Done” is present",
        "Exactly 2 × “Tab”",
        "“Search emoji” is gone",
        "1 window open",
        "No “Save As” window",
        "`grep -q crab a.md` succeeds",
        "A human checks: The crab looks happy",
    ]


def test_drag_between_two_elements_and_window_waits_read_naturally():
    from bad_ass_mcp.bam.describe import describe_phase

    phase = {
        "learn": {"a": {"name": "Tab A"}, "b": {"name": "Tab B"}},
        "steps": [
            {"action": "drag", "from_target": "a", "to_target": "b"},
            {"action": "wait_for_window", "pattern": "Save As"},
            {"action": "get_value", "target": "a", "expect_not": "x"},
            {"action": "get_value", "target": "a", "save": "v"},
        ],
    }
    assert describe_phase(phase, "macos")["steps"] == [
        {"text": "Drag “Tab A” onto “Tab B”"},
        {"text": "A “Save As” window appears", "expect": True},
        {"text": "“Tab A” changed", "expect": True},
        {"text": "Read “Tab A” (saved as v)"},
    ]


def test_results_say_what_each_phase_does_and_expects(tmp_path):
    script = write(
        tmp_path,
        "described",
        {
            "covers": "OK and Cancel work",
            "phases": [
                {
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "click", "target": "ok"}],
                    "checks": [{"present": {"name": "Cancel"}}],
                },
                {"name": "named", "learn": {}, "steps": []},
            ],
            "checks": [{"absent": {"name": "Nope"}}],
        },
    )
    res = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert res["status"] == "PASS"
    assert res["covers"] == "OK and Cancel work"
    first, second = res["phases"]
    # An unnamed phase is named after its first action, not "phase 1".
    assert first["name"] == "Click “OK”"
    assert first["steps"] == [{"text": "Click “OK”"}]
    assert first["checks"] == ["“Cancel” is present"]
    assert second["name"] == "named"
    assert res["checks"] == ["“Nope” is gone"]


def test_phases_after_a_failure_are_listed_as_not_reached(tmp_path):
    script = write(
        tmp_path,
        "stops",
        {
            "phases": [
                {
                    "learn": {"x": {"name": "Missing"}},
                    "steps": [{"action": "click", "target": "x"}],
                },
                {"name": "later", "learn": {"ok": {"name": "OK"}}, "steps": []},
            ]
        },
    )
    res = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert [p["status"] for p in res["phases"]] == ["FAIL", "NOT_RUN"]
    assert res["phases"][1]["name"] == "later"


def test_report_lists_steps_and_expectations_and_marks_the_failing_step(tmp_path):
    script = write(
        tmp_path,
        "reported",
        {
            "covers": "Typing a name and reading it back",
            "phases": [
                {
                    "name": "type a name",
                    "learn": {"name": {"name": "Name"}},
                    "steps": [
                        {"action": "type", "target": "name", "text": "crab"},
                        {"action": "get_value", "target": "name", "expect": "lobster"},
                    ],
                },
                {"name": "never runs", "learn": {}, "steps": []},
            ],
        },
    )
    run_dir = run_suite(FakeBackend(), [script], "linux", CONFIG, tmp_path / "res", commit="c")
    html = write_report(run_dir).read_text()
    assert "Typing a name and reading it back" in html
    assert "Type &quot;crab&quot; into “Name”" in html
    assert "“Name” reads &quot;lobster&quot;" in html
    # The step that failed is marked, and the phase after it shows as not reached.
    assert 'class="step expect bad"' in html
    assert "not reached" in html


def test_a_learn_failure_marks_the_phase_steps_not_run(tmp_path):
    script = write(
        tmp_path,
        "nolearn",
        {
            "phases": [
                {
                    "name": "find it",
                    "learn": {"x": {"name": "Missing"}},
                    "steps": [{"action": "click", "target": "x"}],
                }
            ]
        },
    )
    res = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert res["failure"]["action"] == "learn"
    run_dir = run_suite(FakeBackend(), [script], "linux", CONFIG, tmp_path / "res", commit="c")
    html = write_report(run_dir).read_text()
    assert '<li class="step">Click “Missing”</li>' in html
    assert "step ok" not in html


def test_saved_values_read_as_names_not_raw_variables():
    from bad_ass_mcp.bam.describe import describe_phase

    phase = {
        "learn": {"f": {"name": "Preview font"}},
        "steps": [
            {"action": "get_value", "target": "f", "save": "before"},
            {"action": "get_value", "target": "f", "expect": "$before"},
        ],
    }
    assert describe_phase(phase, "macos")["steps"] == [
        {"text": "Read “Preview font” (saved as before)"},
        {"text": "“Preview font” still reads the saved before", "expect": True},
    ]


def test_a_learn_failure_says_what_was_missing_inside_the_phase(tmp_path):
    script = write(
        tmp_path,
        "missing",
        {"phases": [{"name": "p", "learn": {"x": {"name": "Gone"}}, "steps": []}]},
    )
    run_dir = run_suite(FakeBackend(), [script], "linux", CONFIG, tmp_path / "res", commit="c")
    html = write_report(run_dir).read_text()
    assert "Couldn’t find x (name &#x27;Gone&#x27;)" in html
    # Only the mark is coloured, not the phase name.
    assert '<span class="m bad">✗</span>' in html


# ── Windows feedback (ck-crab, 2026-11-10) ───────────────────────────


def test_bam_file_io_is_always_utf8():
    """Windows' default codepage (cp1252) can't write 🦀: every text read and
    write in bam/ must say encoding="utf-8"."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "bad_ass_mcp" / "bam"
    bad = []
    for f in src.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    node.func.attr in ("write_text", "read_text")
                    or any(k.arg == "text" for k in node.keywords)  # subprocess
                )
                and not any(k.arg == "encoding" for k in node.keywords)
            ):
                bad.append(f"{f.name}:{node.lineno}")
    assert bad == []


def test_step_text_shows_variable_values_for_this_platform(tmp_path):
    script = write(
        tmp_path,
        "vars",
        {
            "variables": {"who": {"linux": "crab", "windows": "lobster"}},
            "phases": [
                {
                    "learn": {"n": {"name": "Name"}},
                    "steps": [{"action": "type", "target": "n", "text": "$who"}],
                }
            ],
        },
    )
    res = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert res["phases"][0]["steps"] == [{"text": 'Type "crab" into “Name”'}]


def test_a_phase_with_nothing_for_this_platform_is_skipped_not_passed(tmp_path):
    script = write(
        tmp_path,
        "empty",
        {
            "phases": [
                {
                    "name": "mac only steps",
                    "steps": [{"action": "key", "key": "F1", "only": ["macos"]}],
                },
                {
                    "name": "real",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "click", "target": "ok"}],
                },
            ]
        },
    )
    res = run_script(FakeBackend(), script, "linux", CONFIG, tmp_path / "out")
    assert [(p["name"], p["status"]) for p in res["phases"]] == [
        ("mac only steps", "SKIP"),
        ("real", "PASS"),
    ]
    # Each screenshot belongs to the phase that took it.
    assert "screenshot" not in res["phases"][0]
    assert res["phases"][1]["screenshot"] == res["screenshots"][0]


def test_report_labels_screenshots_by_the_phase_that_took_them(tmp_path):
    script = write(
        tmp_path,
        "labels",
        {
            "phases": [
                {
                    "name": "nothing here",
                    "steps": [{"action": "key", "key": "F1", "only": ["macos"]}],
                },
                {
                    "name": "click ok",
                    "learn": {"ok": {"name": "OK"}},
                    "steps": [{"action": "click", "target": "ok"}],
                },
            ]
        },
    )
    run_dir = run_suite(FakeBackend(), [script], "linux", CONFIG, tmp_path / "res", commit="c")
    html = write_report(run_dir).read_text()
    assert 'alt="click ok"' in html
    assert 'alt="nothing here"' not in html


def test_keys_without_a_window_say_they_go_to_the_front_window():
    from bad_ass_mcp.bam.describe import describe_phase

    phase = {
        "steps": [
            {"action": "key", "key": "F10"},
            {"action": "key", "key": "Escape", "window_id": "$WINDOW"},
        ]
    }
    assert describe_phase(phase, "windows")["steps"] == [
        {"text": "Press F10 (real key, to the front window)"},
        {"text": "Press Escape"},
    ]


def test_expect_not_a_saved_value_says_differs_from_it():
    from bad_ass_mcp.bam.describe import describe_phase

    phase = {
        "learn": {"s": {"name": "Splitter"}},
        "steps": [
            {"action": "get_bounds", "target": "s", "expect_not": "$stacked"},
            {"action": "get_value", "target": "s", "expect_not": "$before"},
        ],
    }
    assert describe_phase(phase, "linux")["steps"] == [
        {"text": "“Splitter” is not where it was (saved stacked)", "expect": True},
        {"text": "“Splitter” no longer reads the saved before", "expect": True},
    ]


def test_report_collapses_repeated_steps(tmp_path):
    script = write(
        tmp_path,
        "downs",
        {
            "phases": [
                {
                    "name": "menu",
                    "steps": [{"action": "key", "key": "Down", "window_id": "$WINDOW"}] * 7,
                }
            ]
        },
    )
    run_dir = run_suite(FakeBackend(), [script], "linux", CONFIG, tmp_path / "res", commit="c")
    html = write_report(run_dir).read_text()
    assert "Press Down ×7" in html
    assert html.count("Press Down") == 1


# ── foreground guard: real input only ever reaches the app ───────────


class _Desk(FakeBackend):
    """A desktop where another app (pid 4242) can hold the foreground or sit
    under the pointer. The app under test is pid 9999."""

    def __init__(self, front=9999, raisable=True, under=9999, known=True):
        super().__init__()
        self.front, self.raisable, self.under, self.known = front, raisable, under, known
        self.raised: list[int] = []

    def foreground_pid(self):
        return self.front if self.known else None

    def pid_at(self, x, y):
        return self.under if self.known else None

    def bring_to_front(self, pid):
        self.raised.append(pid)
        if self.raisable or pid != 9999:
            self.front = pid
            if pid == 9999:
                self.under = 9999
            return True
        return False


def _real_input_script(tmp_path, steps, name="real"):
    return write(
        tmp_path,
        name,
        {"phases": [{"name": "p", "learn": {"ok": {"name": "OK"}}, "steps": steps}]},
    )


def test_a_real_key_brings_the_app_forward_first_and_hands_focus_back_after(tmp_path):
    desk = _Desk(front=4242)
    script = _real_input_script(tmp_path, [{"action": "key", "key": "F10"}])
    res = run_script(desk, script, "linux", CONFIG, tmp_path / "out")
    assert res["status"] == "PASS"
    assert desk.key_log == ["F10"]
    # Raised for the key, then the user's window gets the focus back.
    assert desk.raised == [9999, 4242]


def test_a_key_aimed_at_the_window_needs_no_foreground(tmp_path):
    desk = _Desk(front=4242)
    script = _real_input_script(
        tmp_path, [{"action": "key", "key": "Escape", "window_id": "$WINDOW"}]
    )
    res = run_script(desk, script, "linux", CONFIG, tmp_path / "out")
    assert res["status"] == "PASS"
    assert desk.raised == []


def test_when_the_app_cannot_come_forward_no_key_is_sent_and_nothing_leaks(tmp_path):
    desk = _Desk(front=4242, raisable=False)
    script = _real_input_script(tmp_path, [{"action": "key", "key": "F10"}])
    out = tmp_path / "out"
    res = run_script(desk, script, "linux", CONFIG, out)
    assert res["status"] == "FAIL"
    assert res["failure"] == {
        "phase": 1,
        "step": 1,
        "action": "foreground",
        "reason": "Unexpected app in foreground.",
    }
    assert desk.key_log == []
    for f in ("result.json", "timeline.json"):
        assert "4242" not in (out / f).read_text(encoding="utf-8")


def test_a_click_at_a_point_another_app_covers_is_refused(tmp_path):
    desk = _Desk(under=4242, raisable=False)
    desk.under = 4242
    script = _real_input_script(tmp_path, [{"action": "click_at", "x": 50, "y": 60}])
    res = run_script(desk, script, "linux", CONFIG, tmp_path / "out")
    assert res["failure"]["reason"] == "Unexpected app in foreground."
    assert desk.pointer_log == []


def test_a_drag_checks_the_point_it_starts_from(tmp_path):
    desk = _Desk()
    script = _real_input_script(
        tmp_path, [{"action": "drag", "from_target": "ok", "dx": 40, "dy": 0}]
    )
    res = run_script(desk, script, "linux", CONFIG, tmp_path / "out")
    assert res["status"] == "PASS"
    assert desk.pointer_log and desk.pointer_log[0][0] == "drag"


def test_a_backend_that_cannot_tell_who_is_in_front_does_not_block(tmp_path):
    desk = _Desk(front=4242, known=False)
    script = _real_input_script(tmp_path, [{"action": "key", "key": "F10"}])
    res = run_script(desk, script, "linux", CONFIG, tmp_path / "out")
    assert res["status"] == "PASS"
    assert desk.key_log == ["F10"]


class _LateStranger(FakeBackend):
    """Nothing is open at launch; then the app's window appears (pid 9999,
    started through a wrapper), and another process opens a 'Save As'."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def list_windows(self):
        from bad_ass_mcp.types import WindowInfo

        self.calls += 1
        if self.calls == 1:
            return []
        stranger = WindowInfo(id="stranger", name="Save As", pid=4242, focused=False)
        return super().list_windows() + [stranger]


def test_windows_from_other_processes_are_never_driven(tmp_path, monkeypatch):
    from bad_ass_mcp.bam import runner as runner_mod

    class _Wrapper:
        pid = 5555  # e.g. a launcher script; the app itself is pid 9999

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _Wrapper())
    script = write(
        tmp_path,
        "stranger",
        {"phases": [{"name": "dialog", "window": "Save As", "learn": {}, "steps": []}]},
    )
    cfg = {**CONFIG, "app": "fake-app"}
    res = run_script(_LateStranger(), script, "linux", cfg, tmp_path / "out")
    assert res["window"] == "fake-window-1"
    assert res["status"] == "FAIL"
    assert res["failure"]["action"] == "window"


def test_scripts_that_need_hands_off_are_found_per_platform(tmp_path):
    from bad_ass_mcp.bam.describe import needs_foreground

    assert needs_foreground({"phases": [{"steps": [{"action": "key", "key": "F10"}]}]}, "linux")
    assert not needs_foreground(
        {"phases": [{"steps": [{"action": "key", "key": "F10", "only": ["windows"]}]}]}, "linux"
    )
    assert not needs_foreground(
        {"phases": [{"steps": [{"action": "key", "key": "F10", "window_id": "$WINDOW"}]}]},
        "linux",
    )
    assert needs_foreground({"phases": [{"steps": [{"action": "drag", "dx": 1}]}]}, "linux")
    assert not needs_foreground(
        {"phases": [{"steps": [{"action": "click", "target": "x"}]}]}, "linux"
    )


def test_hands_off_notice_names_the_scripts():
    from bad_ass_mcp.bam.cli import hands_off_notice

    assert hands_off_notice(["new-window", "resize-split"], 6) == (
        "2 of 6 scripts use real keyboard/mouse input (new-window, resize-split). "
        "Hands off the keyboard and mouse until the run finishes; go grab a coffee."
    )
    assert hands_off_notice([], 6) is None
