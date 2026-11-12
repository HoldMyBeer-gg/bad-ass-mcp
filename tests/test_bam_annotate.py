"""`annotate`: an on-screen overlay that boxes and labels each step.

rusty-win-crab, November 11th: Michael watched a demo-mode video for the
bam-site gallery and missed the step highlights. `--annotate` (or
"annotate": true) runs at demo pace and draws, before each step, a box and
"N  action target" over the target, an arrow for a drag, and a red box with
the reason on the step that failed. The overlay is a separate click-through,
never-focused window process fed JSON lines.
"""

from __future__ import annotations

import json
import sys

import pytest

from bad_ass_mcp.bam import runner
from bad_ass_mcp.bam.runner import run_script
from tests.fake_backend import FakeBackend
from tests.test_bam import CONFIG, write


class FakeOverlay:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    def send(self, item: dict) -> None:
        self.sent.append(item)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def overlay(monkeypatch):
    o = FakeOverlay()
    started = []
    monkeypatch.setattr(runner, "start_overlay", lambda platform: started.append(platform) or o)
    o.started = started
    return o


ANNOTATE = {**CONFIG, "annotate": True, "demo_pace": 0, "annotate_lead": 0}


def _script(tmp_path, steps, name="ann", learn=None):
    return write(
        tmp_path,
        name,
        {
            "phases": [
                {
                    "name": "press",
                    "learn": learn or {"ok": {"name": "OK"}, "size": {"name": "Size"}},
                    "steps": steps,
                }
            ]
        },
    )


def test_annotate_boxes_and_labels_each_step_with_a_target(tmp_path, overlay):
    script = _script(tmp_path, [{"action": "click", "target": "ok"}])
    r = run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert overlay.started == ["linux"]
    assert overlay.sent[0] == {"box": [10.0, 10.0, 80.0, 30.0], "label": "1  click ok"}
    assert overlay.closed


def test_annotate_implies_demo_mode(tmp_path, overlay):
    """Steps run one at a time with their bounds in the timeline, as in demo."""
    script = _script(tmp_path, [{"action": "click", "target": "ok"}])
    out = tmp_path / "out"
    run_script(FakeBackend(), script, "linux", ANNOTATE, out)
    steps = [
        e
        for e in json.loads((out / "timeline.json").read_text(encoding="utf-8"))
        if e["event"] == "step"
    ]
    assert steps and steps[0]["bounds"] == [10.0, 10.0, 80.0, 30.0]


def test_a_step_without_bounds_gets_its_label_at_the_window_corner(tmp_path, overlay):
    script = _script(tmp_path, [{"action": "key", "key": "ctrl+s"}])
    run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    # FakeBackend's window is at (100, 200).
    assert overlay.sent[0] == {"text": [116.0, 216.0], "label": "1  key ctrl+s"}


def test_typed_text_is_never_shown(tmp_path, overlay):
    script = _script(
        tmp_path,
        [{"action": "type", "target": "name", "text": "hunter2"}],
        learn={"name": {"name": "Name"}},
    )
    run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    assert all("hunter2" not in json.dumps(i) for i in overlay.sent)
    assert overlay.sent[0]["label"] == "1  type name"


def test_a_drag_gets_an_arrow_from_start_to_end(tmp_path, overlay):
    script = _script(tmp_path, [{"action": "drag", "from_target": "ok", "dx": 120, "dy": 0}])
    run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    arrows = [i for i in overlay.sent if "arrow" in i]
    assert arrows[0]["arrow"] == [50.0, 25.0, 170.0, 25.0]
    assert arrows[0]["label"] == "1  drag ok"


def test_the_failing_step_gets_a_red_box_with_the_reason(tmp_path, overlay):
    script = _script(
        tmp_path,
        [
            {"action": "click", "target": "ok"},
            {"action": "select", "target": "size", "value": "Gigantic"},
        ],
    )
    r = run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    assert r["status"] == "FAIL", r
    fail = overlay.sent[-1]
    assert fail["kind"] == "fail"
    assert fail["box"] == overlay.sent[-2]["box"]  # the step that failed
    assert fail["label"].startswith("✗ ")
    assert r["failure"]["reason"][:20] in fail["label"]


def test_no_overlay_is_a_missing_extra_not_a_failed_run(tmp_path, monkeypatch):
    def refuse(platform):
        raise RuntimeError("annotations aren't supported on this platform yet")

    monkeypatch.setattr(runner, "start_overlay", refuse)
    script = _script(tmp_path, [{"action": "click", "target": "ok"}])
    r = run_script(FakeBackend(), script, "linux", ANNOTATE, tmp_path / "out")
    assert r["status"] == "PASS", r
    assert r["annotate_error"] == "annotations aren't supported on this platform yet"


def test_without_annotate_no_overlay_starts(tmp_path, overlay):
    script = _script(tmp_path, [{"action": "click", "target": "ok"}])
    run_script(
        FakeBackend(), script, "linux", {**CONFIG, "demo": True, "demo_pace": 0}, tmp_path / "o"
    )
    assert overlay.started == []


# ── CLI, config, summary ─────────────────────────────────────────────


def test_cli_annotate_flag_sets_the_override():
    from bad_ass_mcp.bam.cli import parse

    args = parse(["x.json", "--annotate"])
    assert args.overrides["annotate"] is True


def test_config_accepts_annotate():
    from bad_ass_mcp.bam.validate import validate_config

    assert validate_config({"annotate": True, "annotate_lead": 0.4}) == []


def test_the_summary_says_why_there_were_no_annotations(tmp_path, capsys, monkeypatch):
    from bad_ass_mcp.bam.cli import print_summary
    from bad_ass_mcp.bam.suite import run_suite

    def refuse(platform):
        raise RuntimeError("annotations need a desktop BAM can draw on")

    monkeypatch.setattr(runner, "start_overlay", refuse)
    script = write(tmp_path, "ann", {"phases": []})
    run_dir = run_suite(
        FakeBackend(), [script], "linux", ANNOTATE, tmp_path / "results", commit="abc"
    )
    print_summary(run_dir, color=False)
    assert "no annotations: annotations need a desktop" in capsys.readouterr().out


# ── the overlay client ───────────────────────────────────────────────


def test_overlay_client_writes_one_json_line_per_item(tmp_path):
    from bad_ass_mcp.overlay import Overlay

    sink = tmp_path / "sink.txt"
    cmd = [sys.executable, "-c", f"import sys; open({str(sink)!r}, 'w').write(sys.stdin.read())"]
    o = Overlay(cmd)
    o.send({"box": [1, 2, 3, 4], "label": "1  click ok"})
    o.send({"arrow": [1, 2, 3, 4], "label": "2  drag"})
    o.close()
    lines = sink.read_text().splitlines()
    assert [json.loads(x) for x in lines] == [
        {"box": [1, 2, 3, 4], "label": "1  click ok"},
        {"arrow": [1, 2, 3, 4], "label": "2  drag"},
    ]


def test_overlay_client_survives_an_overlay_that_died(tmp_path):
    from bad_ass_mcp.overlay import Overlay

    o = Overlay([sys.executable, "-c", "pass"])
    o.proc.wait()
    o.send({"box": [1, 2, 3, 4], "label": "x"})  # no BrokenPipeError
    o.close()


def test_start_overlay_refuses_wayland(monkeypatch):
    from bad_ass_mcp import overlay

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    with pytest.raises(RuntimeError, match="Wayland"):
        overlay.start_overlay("linux")
