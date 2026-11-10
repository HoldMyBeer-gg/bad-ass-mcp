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
from datetime import datetime
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
        self.phase_log: list[dict] = []
        self.pid: int | None = config.get("pid")
        self.before: set[str] | None = None
        self.progress = None  # callable(phase_number) after each phase

    # ── windows ───────────────────────────────────────────────────────
    def _ours(self, w) -> bool:
        """Windows the run may drive: the launched process (or a window that
        appeared after the launch, for apps started through a wrapper), the
        configured pid, or anything when neither is known."""
        if self.pid is None:
            return True
        return w.pid == self.pid or (self.before is not None and w.id not in self.before)

    def find_window(
        self, pattern: str, timeout: float, *, unique: bool = False, exclude: str | None = None
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            hits = [w for w in self.b.list_windows() if _matches(w.name, pattern) and self._ours(w)]
            hits = [w for w in hits if w.id != exclude]
            if len(hits) > 1 and unique and self.pid is None:
                raise ScriptFailure(
                    f"{len(hits)} windows match {pattern!r} "
                    f"(pids {', '.join(str(w.pid) for w in hits)}); set `app` so the run "
                    "launches its own copy, or `pid` to pick one. Refusing to guess."
                )
            if hits:
                return hits[0].id
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    def count_windows(self, pattern: str) -> int:
        """Windows matching `pattern` that this run may drive. The Linux and
        macOS backends list one entry per application, so there the app's
        top-level window children are counted (AT-SPI `frame`, AX `window`);
        an app exposing none counts as one."""
        n = 0
        for w in self.b.list_windows():
            if not (_matches(w.name, pattern) and self._ours(w)):
                continue
            if self.platform in ("linux", "macos"):
                try:
                    tree = self.b.get_tree(w.id, max_depth=1)
                    frames = [c for c in tree.children if c.role in ("frame", "window")]
                except Exception:
                    frames = []
                n += max(1, len(frames))
            else:
                n += 1
        return n

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
        for key, value in self.script.get("variables", {}).items():
            v = pick(value, self.platform)
            if v is not None:
                self.vars[key] = expand(v, self.vars)
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
            self.before = {w.id for w in self.b.list_windows()}
            self.proc = subprocess.Popen([*cmd, *args])
            self.pid = self.proc.pid
            timeout = float(self.config.get("launch_timeout", 20))
        else:
            timeout = float(self.config.get("step_timeout", 5))
        self.window = self.find_window(pattern, timeout, unique=True)
        if self.window is None:
            raise ScriptFailure(f"no window matching {pattern!r}")
        # A window can exist before its content does (WebView2 paints after
        # the window appears), and keys sent then are lost. Wait for an
        # element that only exists once the app is usable.
        ready = pick(self.config.get("ready"), self.platform)
        if ready:
            desc = learn_descriptors({"r": ready}, self.platform)["r"]
            limit = float(self.config.get("launch_timeout", 20))
            deadline = time.monotonic() + limit
            while not self.find(self.window, desc):
                if time.monotonic() >= deadline:
                    raise ScriptFailure(f"app not ready: no {_describe(desc)} within {limit:g}s")
                time.sleep(0.2)

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
        entry = {"name": phase.get("name") or phase.get("id") or f"phase {n}", "status": "FAIL"}
        self.phase_log.append(entry)
        started = time.monotonic()
        try:
            self._run_phase(phase, n)
            entry["status"] = "PASS"
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            if self.progress:
                self.progress(n)

    def _run_phase(self, phase: dict, n: int) -> None:
        window = self.window
        pattern = pick(phase.get("window"), self.platform)
        if isinstance(pattern, dict) and "other" in pattern:
            # Another window matching the pattern, not the main one (e.g. the
            # second editor window after New Window, on Windows where each
            # window is listed separately).
            other = pick(pattern["other"], self.platform)
            window = self.find_window(
                other, float(self.config.get("step_timeout", 5)), exclude=self.window
            )
            if window is None:
                raise ScriptFailure(f"no other window matching {other!r}", phase=n)
        elif pattern is not None:
            window = self.find_window(pattern, float(self.config.get("step_timeout", 5)))
            if window is None:
                raise ScriptFailure(f"no window matching {pattern!r}", phase=n)

        try:
            descs = learn_descriptors(phase.get("learn", {}), self.platform)
            # Elements can render a beat after the step that opened them:
            # retry missing ones until step_timeout.
            deadline = time.monotonic() + float(self.config.get("step_timeout", 5))
            while True:
                plain = {k: v for k, v in descs.items() if "state" not in v}
                handles = self.b.learn_layout(window, plain) if plain else {}
                for k, d in descs.items():
                    if "state" in d:
                        hits = self.find(window, d)
                        handles[k] = hits[0].id if hits else None
                missing = [f"{k} ({_describe(descs[k])})" for k, v in handles.items() if v is None]
                if not missing or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
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

    def find(self, window: str | None, desc: dict) -> list:
        """find_elements by role/name, then keep those carrying `state`."""
        found = self.b.find_elements(window, role=desc.get("role"), name=desc.get("name"))
        if "state" in desc:
            found = [e for e in found if desc["state"] in (getattr(e, "states", None) or ())]
        return found

    def run_steps(self, sent: list[dict], meta: list[dict], phase: int) -> None:
        # Run server-side in chunks, ending each chunk at a step whose value
        # we need to check, so a wrong value stops the flow right there.
        # `shell` steps aren't backend actions: the runner runs them itself.
        start = 0
        while start < len(sent):
            if sent[start].get("action") == "shell":
                r = _shell(sent[start].get("cmd", ""), self.vars)
                if r.returncode != 0:
                    raise ScriptFailure(
                        f"shell step failed ({r.returncode}): {sent[start].get('cmd')!r} "
                        f"{r.stderr.strip()}".strip(),
                        phase=phase,
                        step=start + 1,
                        action="shell",
                    )
                start += 1
                continue
            end = start
            while end < len(sent) - 1 and not meta[end] and sent[end + 1].get("action") != "shell":
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
                spec = c["present" if want_present else "absent"]
                found = self.find(window, desc)
                if want_present and "count" in spec:
                    if len(found) != int(spec["count"]):
                        raise ScriptFailure(
                            f"expected {spec['count']} of {_describe(desc)}, found {len(found)}",
                            phase=phase,
                        )
                elif want_present and not found:
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
    if "state" in desc:
        parts.append(f"state {desc['state']!r}")
    return ", ".join(parts) or "any element"


def _plain_tree(node) -> dict:
    return {
        "role": node.role,
        "name": node.name,
        "value": getattr(node, "value", None),
        "children": [_plain_tree(c) for c in node.children],
    }


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_script(
    backend,
    script_path: Path | str,
    platform: str,
    config: dict,
    out_dir: Path | str,
    progress=None,
) -> dict[str, Any]:
    """Run one script; write out_dir/result.json and return the result.
    `progress(phase_number)` is called after each phase."""
    script_path = Path(script_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    name = script.get("name") or script_path.stem
    result: dict[str, Any] = {
        "name": name,
        "script": str(script_path),
        "platform": platform,
        "started_at": _now(),
    }
    started = time.monotonic()

    if platform not in script.get("platforms", [platform]):
        result["status"] = "SKIP"
    else:
        run = _Run(backend, platform, config, out_dir, script)
        run.progress = progress
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
        result["phases"] = run.phase_log
        if run.window is not None:
            result["window"] = run.window
        result["screenshots"] = run.screenshots
        if run.manual:
            result["manual"] = run.manual
        if (out_dir / "fail.png").exists():
            result["fail_screenshot"] = "fail.png"

    result["finished_at"] = _now()
    result["seconds"] = round(time.monotonic() - started, 2)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result
