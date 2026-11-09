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

CONFIG = {"window": "Fake App", "screenshot_proof": True}


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
        backend, [script], "linux", {**CONFIG, "retries": 1}, tmp_path / "results", commit="abc"
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
