"""Run one BAM script against a live app and record what happened.

A script's result lands in its own folder: result.json, a proof screenshot
per phase (screenshot_proof), and on failure fail.png plus the window's
pruned accessibility tree (tree.json) at the moment it failed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .script import applies, build_steps, expand, learn_descriptors, pick


class ScriptFailure(Exception):
    def __init__(
        self,
        reason: str,
        *,
        phase: int | None = None,
        step: int | None = None,
        action: str | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.phase = phase
        self.step = step
        self.action = action


def _shell(cmd: str, env: dict[str, str], timeout: float = 60) -> subprocess.CompletedProcess:
    # Scripts are written for a POSIX shell ($SMOKE, printf, grep). On Windows
    # that means Git Bash or WSL's bash on PATH.
    bash = shutil.which("bash")
    args = [bash, "-c", cmd] if bash else cmd
    return subprocess.run(
        args,
        shell=bash is None,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _matches(name: str, pattern: str) -> bool:
    return pattern.lower() in (name or "").lower()


class _Run:
    def __init__(self, backend, platform: str, config: dict, out_dir: Path, script: dict):
        self.b = backend
        self.platform = platform
        self.config = config
        self.out = out_dir
        self.script = script
        self.files = out_dir / "files"
        self.vars: dict[str, str] = {"SMOKE": str(self.files)}
        self.window: str | None = None
        self.proc: subprocess.Popen | None = None
        self.phase_no = 0
        self.screenshots: list[str] = []
        self.manual: list[str] = []
        self.phases_by_id: dict[str, dict] = {}

    # ── windows ───────────────────────────────────────────────────────
    def find_window(self, pattern: str, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            for w in self.b.list_windows():
                if _matches(w.name, pattern):
                    return w.id
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    def count_windows(self, pattern: str) -> int:
        return sum(1 for w in self.b.list_windows() if _matches(w.name, pattern))

    # ── evidence ──────────────────────────────────────────────────────
    def shot(self, name: str, window: str | None) -> str | None:
        try:
            path = self.out / name
            self.b.screenshot(window, output_path=str(path))
            return name if path.exists() else None
        except Exception:
            return None

    def dump_tree(self, window: str | None) -> None:
        if window is None:
            return
        try:
            from ..backend.base import prune_tree

            tree = prune_tree(self.b.get_tree(window))
            try:
                from ..server import _serialise_tree

                data = _serialise_tree(tree)
            except Exception:
                data = _plain_tree(tree)
            (self.out / "tree.json").write_text(json.dumps(data, indent=1, default=str))
        except Exception:
            pass

    # ── flow ──────────────────────────────────────────────────────────
    def setup(self) -> None:
        self.files.mkdir(parents=True, exist_ok=True)
        for entry in self.script.get("setup", []):
            if not applies(entry, self.platform) or "shell" not in entry:
                continue
            r = _shell(entry["shell"], self.vars)
            if r.returncode != 0:
                raise ScriptFailure(f"setup failed: {entry['shell']!r}: {r.stderr.strip()}")

    def launch(self) -> None:
        pattern = self.config.get("window")
        if not pattern:
            raise ScriptFailure("config has no 'window' title pattern")
        app = pick(self.config.get("app"), self.platform)
        if app:
            cmd = app if isinstance(app, list) else [app]
            args = [str(self.files / a) for a in self.script.get("launch", [])]
            self.proc = subprocess.Popen([*cmd, *args])
            timeout = float(self.config.get("launch_timeout", 20))
        else:
            timeout = float(self.config.get("step_timeout", 5))
        self.window = self.find_window(pattern, timeout)
        if self.window is None:
            raise ScriptFailure(f"no window matching {pattern!r}")

    def teardown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run_phases(self) -> None:
        for phase in self.script.get("phases", []):
            if "id" in phase:
                self.phases_by_id[phase["id"]] = phase
            if "repeat" in phase:
                target = self.phases_by_id.get(phase["repeat"])
                if target is None:
                    raise ScriptFailure(f"repeat of unknown phase {phase['repeat']!r}")
                phase = target
            if not applies(phase, self.platform):
                continue
            self.run_phase(phase)

    def run_phase(self, phase: dict) -> None:
        self.phase_no += 1
        n = self.phase_no
        window = self.window
        if "window" in phase:
            pattern = pick(phase["window"], self.platform)
            window = self.find_window(pattern, float(self.config.get("step_timeout", 5)))
            if window is None:
                raise ScriptFailure(f"no window matching {pattern!r}", phase=n)

        try:
            descs = learn_descriptors(phase.get("learn", {}), self.platform)
            handles = self.b.learn_layout(window, descs) if descs else {}
            missing = [f"{k} ({_describe(descs[k])})" for k, v in handles.items() if v is None]
            if missing:
                raise ScriptFailure("element not found: " + ", ".join(missing), phase=n)

            sent, meta = build_steps(
                phase.get("steps", []), self.platform, handles, window, self.vars
            )
            self.run_steps(sent, meta, n)
            if "checks" in phase:
                self.run_checks(phase["checks"], window, phase=n)
        except ScriptFailure as f:
            self.fail_evidence(window)
            raise f

        if self.config.get("screenshot_proof", True):
            name = self.shot(f"phase-{n}.png", window)
            if name:
                self.screenshots.append(name)

    def run_steps(self, sent: list[dict], meta: list[dict], phase: int) -> None:
        # Run server-side in chunks, ending each chunk at a step whose value
        # we need to check, so a wrong value stops the flow right there.
        start = 0
        while start < len(sent):
            end = start
            while end < len(sent) - 1 and not meta[end]:
                end += 1
            chunk = sent[start : end + 1]
            results = self.b.run_sequence(chunk, stop_on_error=True)
            for offset, r in enumerate(results):
                i = start + offset
                if not r.get("ok"):
                    raise ScriptFailure(
                        f"{r.get('action')} failed: {r.get('error') or 'not ok'}",
                        phase=phase,
                        step=i + 1,
                        action=r.get("action"),
                    )
                m = meta[i]
                if "save" in m:
                    self.vars[m["save"]] = str(r.get("value"))
                if "expect" in m:
                    want = expand(m["expect"], self.vars)
                    got = r.get("value")
                    if str(got) != str(want):
                        raise ScriptFailure(
                            f"get_value: expected {want!r}, got {got!r}",
                            phase=phase,
                            step=i + 1,
                            action="get_value",
                        )
            if len(results) < len(chunk):
                raise ScriptFailure(
                    "sequence stopped early", phase=phase, step=start + len(results) + 1
                )
            start = end + 1

    def run_checks(self, checks: list[dict], window: str | None, phase: int | None = None) -> None:
        for c in checks:
            if not applies(c, self.platform):
                continue
            if "present" in c or "absent" in c:
                want_present = "present" in c
                desc = learn_descriptors(
                    {"x": c["present" if want_present else "absent"]}, self.platform
                )["x"]
                found = self.b.find_elements(window, role=desc.get("role"), name=desc.get("name"))
                if want_present and not found:
                    raise ScriptFailure(f"expected present: {_describe(desc)}", phase=phase)
                if not want_present and found:
                    raise ScriptFailure(f"expected absent: {_describe(desc)}", phase=phase)
            elif "windows" in c:
                n = self.count_windows(self.config["window"])
                if n != int(c["windows"]):
                    raise ScriptFailure(
                        f"expected {c['windows']} window(s), found {n}", phase=phase
                    )
            elif "no_window" in c:
                pattern = pick(c["no_window"], self.platform)
                if self.count_windows(pattern):
                    raise ScriptFailure(f"unexpected window matching {pattern!r}", phase=phase)
            elif "shell" in c:
                r = _shell(c["shell"], self.vars)
                if r.returncode != 0:
                    raise ScriptFailure(f"check failed: {c['shell']!r}", phase=phase)
            elif "manual" in c:
                self.manual.append(c["manual"])

    def fail_evidence(self, window: str | None) -> None:
        self.shot("fail.png", window)
        self.dump_tree(window)


def _describe(desc: dict) -> str:
    parts = [f"name {desc['name']!r}"] if "name" in desc else []
    if "role" in desc:
        parts.append(f"role {desc['role']!r}")
    return ", ".join(parts) or "any element"


def _plain_tree(node) -> dict:
    return {
        "role": node.role,
        "name": node.name,
        "value": getattr(node, "value", None),
        "children": [_plain_tree(c) for c in node.children],
    }


def run_script(
    backend, script_path: Path | str, platform: str, config: dict, out_dir: Path | str
) -> dict[str, Any]:
    """Run one script; write out_dir/result.json and return the result."""
    script_path = Path(script_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    name = script.get("name") or script_path.stem
    result: dict[str, Any] = {"name": name, "script": str(script_path), "platform": platform}
    started = time.monotonic()

    if platform not in script.get("platforms", [platform]):
        result["status"] = "SKIP"
    else:
        run = _Run(backend, platform, config, out_dir, script)
        recording = None
        try:
            run.setup()
            run.launch()
            if config.get("record_video"):
                recording = backend.start_recording(run.window)
            run.run_phases()
            run.run_checks(script.get("checks", []), run.window)
            result["status"] = "MANUAL" if run.manual else "PASS"
        except ScriptFailure as f:
            if not (out_dir / "fail.png").exists():
                run.fail_evidence(run.window)
            result["status"] = "FAIL"
            result["failure"] = {
                k: v
                for k, v in {
                    "phase": f.phase,
                    "step": f.step,
                    "action": f.action,
                    "reason": f.reason,
                }.items()
                if v is not None
            }
        except Exception as e:  # a bug in the script or the backend, not the app
            run.fail_evidence(run.window)
            result["status"] = "FAIL"
            result["failure"] = {"reason": f"{type(e).__name__}: {e}"}
        finally:
            if recording is not None:
                keep = config.get("record_video") is True or result.get("status") == "FAIL"
                try:
                    path = backend.stop_recording(recording, str(out_dir / "run.mp4"))
                    if keep:
                        result["video"] = "run.mp4"
                    elif path and Path(path).exists():
                        Path(path).unlink()
                except Exception:
                    pass
            run.teardown()
        result["screenshots"] = run.screenshots
        if run.manual:
            result["manual"] = run.manual
        if (out_dir / "fail.png").exists():
            result["fail_screenshot"] = "fail.png"

    result["seconds"] = round(time.monotonic() - started, 2)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result
