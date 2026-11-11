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
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..backend.base import last_injected_at
from .describe import (
    describe_checks,
    describe_phase,
    is_real_input,
    needs_foreground,
    phase_title,
)
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
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


class _Run:
    def __init__(self, backend, platform: str, config: dict, out_dir: Path, script: dict):
        self.secret_values: set[str] = set()
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
        self.timeline: list[dict] = []
        self.t0 = time.monotonic()
        self.phase_name = ""
        self.targets: dict[str, str] = {}  # handle -> learn label, for the timeline
        self.pid: int | None = config.get("pid")
        self.app_pid: int | None = None  # the main window's process, once found
        self.prev_front: int | None = None  # the user's foreground app, to hand back
        self.hung_after: float | None = None
        # Yield to a person: in scripts that send real input, any keyboard or
        # mouse input that isn't ours stops the run (and the suite).
        self.yields = bool(config.get("yield_to_user", True)) and needs_foreground(script, platform)
        self.own_until = time.monotonic() + 0.5  # grace: the Enter key-up that started us
        self.user_stopped = False
        self.before: set[str] | None = None
        self.progress = None  # callable(phase_number) after each phase

    # ── windows ───────────────────────────────────────────────────────
    def _ours(self, w) -> bool:
        """Windows the run may drive: the launched process (or a window that
        appeared after the launch, for apps started through a wrapper), the
        configured pid, or anything when neither is known."""
        if self._helper(w):
            return True
        if self.app_pid is not None:
            # Once the app's window is known, only its process counts: a
            # window another app opens mid-run is never driven.
            return w.pid in (self.app_pid, self.pid)
        if self.pid is None:
            return True
        return w.pid == self.pid or (self.before is not None and w.id not in self.before)

    def _helper(self, w) -> bool:
        """A window of a named helper process (`helpers` in the config), such
        as the xdg-desktop-portal that owns file dialogs on Linux: it runs
        before the launch, so ownership by pid would never admit it."""
        helpers = pick(self.config.get("helpers"), self.platform) or []
        return any(w.matches(h) for h in helpers)

    def find_window(
        self, pattern: str, timeout: float, *, unique: bool = False, exclude: str | None = None
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            hits = [w for w in self.b.list_windows() if w.matches(pattern) and self._ours(w)]
            hits = [w for w in hits if w.id != exclude]
            if len(hits) > 1 and unique and self.pid is None:
                raise ScriptFailure(
                    f"{len(hits)} windows match {pattern!r} "
                    f"(pids {', '.join(str(w.pid) for w in hits)}); set `app` so the run "
                    "launches its own copy, or `pid` to pick one. Refusing to guess."
                )
            if hits:
                return hits[0].id
            # The watchdog killed a hung app: nothing will appear. (Not "the
            # process exited": wrapper launchers such as `open -a` exit at once.)
            if time.monotonic() >= deadline or self.hung_after is not None:
                return None
            time.sleep(0.2)

    def count_windows(self, pattern: str) -> int:
        """Windows matching `pattern` that this run may drive. The Linux and
        macOS backends list one entry per application, so there the app's
        top-level window children are counted (AT-SPI `frame`, AX `window`);
        an app exposing none counts as one."""
        n = 0
        for w in self.b.list_windows():
            if not (w.matches(pattern) and self._ours(w)):
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

    def log(self, event: str, **fields) -> None:
        self.timeline.append({"t": round(time.monotonic() - self.t0, 3), "event": event, **fields})

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
            (self.out / "tree.json").write_text(
                json.dumps(self.redact(data), indent=1, default=str), encoding="utf-8"
            )
        except Exception:
            pass

    # ── flow ──────────────────────────────────────────────────────────
    def setup(self) -> None:
        self.files.mkdir(parents=True, exist_ok=True)
        for key, value in self.script.get("variables", {}).items():
            v = pick(value, self.platform)
            if v is not None:
                self.vars[key] = expand(v, self.vars)
        # The script's own values, shown resolved in step text (saved values
        # aren't known until the steps run).
        self.static_vars = {k: v for k, v in self.vars.items() if k != "WINDOW"}
        for entry in self.script.get("setup", []):
            if not applies(entry, self.platform) or "shell" not in entry:
                continue
            r = _shell(entry["shell"], self.vars)
            if r.returncode != 0:
                raise ScriptFailure(f"setup failed: {entry['shell']!r}: {r.stderr.strip()}")

    def launch(self) -> None:
        # Where the user's focus was when the run began: it goes back there
        # at the end if the run had to raise the app.
        try:
            self.start_front = self.b.foreground_pid()
        except Exception:
            self.start_front = None
        pattern = pick(self.config.get("window"), self.platform)
        if not pattern:
            raise ScriptFailure("config has no 'window' title pattern")
        app = pick(self.config.get("app"), self.platform)
        if app:
            cmd = app if isinstance(app, list) else [app]
            args = [str(self.files / a) for a in self.script.get("launch", [])]
            self.before = {w.id for w in self.b.list_windows()}
            self.proc = subprocess.Popen([*cmd, *args])
            self.pid = self.proc.pid
            self.start_watchdog()
            timeout = float(self.config.get("launch_timeout", 20))
        else:
            timeout = float(self.config.get("step_timeout", 5))
        self.window = self.find_window(pattern, timeout, unique=True)
        if self.window is None:
            raise ScriptFailure(f"no window matching {pattern!r}")
        self.app_pid = next((w.pid for w in self.b.list_windows() if w.id == self.window), self.pid)
        # A window can exist before its content does (WebView2 paints after
        # the window appears), and keys sent then are lost. Wait for an
        # element that only exists once the app is usable.
        ready = pick(self.config.get("ready"), self.platform)
        if ready:
            desc = learn_descriptors({"r": ready}, self.platform)["r"]
            limit = float(self.config.get("launch_timeout", 20))
            deadline = time.monotonic() + limit
            while not self.find(self.window, desc):
                if time.monotonic() >= deadline or self.hung_after is not None:
                    raise ScriptFailure(f"app not ready: no {_describe(desc)} within {limit:g}s")
                time.sleep(0.2)

    def run_teardown(self) -> list[dict]:
        """The script's own cleanup (`teardown`): after the last phase and
        after a failure, before the app is closed and focus goes back. Each
        entry is a shell command or a step; best-effort, so a failing entry
        is recorded and the rest still run, and the result never changes.
        Steps are skipped when there is no app window to send them to (it
        never appeared, hung, or the user took over)."""
        out: list[dict] = []
        entries = [e for e in self.script.get("teardown", []) if applies(e, self.platform)]
        scale = float(self.config.get("timeout_scale", 1))
        for i, entry in enumerate(entries, 1):
            try:
                if "shell" in entry:
                    r = _shell(entry["shell"], self.vars)
                    ok, err = r.returncode == 0, (r.stderr.strip() or None)
                elif self.window is None or self.hung_after is not None or self.user_stopped:
                    ok, err = False, "skipped: no app window to send it to"
                else:
                    sent, _ = build_steps(
                        [entry], self.platform, {}, self.window, self.vars, timeout_scale=scale
                    )
                    if not sent:
                        continue
                    if is_real_input(sent[0]):
                        self.guard(sent[0], 0, i)
                    res = self.b.run_sequence(sent, stop_on_error=True)[0]
                    ok, err = bool(res.get("ok")), res.get("error")
            except ScriptFailure as f:
                ok, err = False, f.reason
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            out.append({"step": i, "ok": ok, **({"error": err} if err and not ok else {})})
            self.log("teardown", step=i, ok=ok)
        return out

    def teardown(self) -> None:
        try:
            self._close_app()
        finally:
            self._hand_focus_back()

    def start_watchdog(self) -> None:
        """A call into a hung app blocks forever, even the window lookup at
        launch. Poll app_hung(pid) from a side thread (pure OS calls, never
        the a11y API); once the app has been hung for `hang_timeout` seconds,
        kill the launched app, which makes the blocked call return, and the
        script fails as "app not responding". Started right after the
        launch, keyed on the process, not the window. (Attach mode can't kill
        the user's app; the hang is still reported.)"""
        if getattr(self, "_stop_watch", None) is not None:
            return
        limit = float(self.config.get("hang_timeout", 30))
        self.hung_after = None
        self._stop_watch = threading.Event()
        if limit <= 0:
            return

        def watch() -> None:
            since = None
            while not self._stop_watch.wait(min(1.0, limit / 3)):
                pid = self.app_pid or self.pid
                if pid is None:
                    continue
                try:
                    hung = self.b.app_hung(pid)
                except Exception:
                    hung = None
                if not hung:
                    since = None
                    continue
                since = since or time.monotonic()
                if time.monotonic() - since >= limit:
                    self.hung_after = limit
                    self.log("hang", seconds=limit)
                    if self.proc is not None:
                        try:
                            self.proc.kill()
                        except Exception:
                            pass
                    return

        threading.Thread(target=watch, name="bam-watchdog", daemon=True).start()

    def stop_watchdog(self) -> None:
        stop = getattr(self, "_stop_watch", None)
        if stop is not None:
            stop.set()

    def _hand_focus_back(self) -> None:
        """After the app is gone (closing it would otherwise pass the focus to
        whatever window is next), give the user's app its focus back, gently:
        no synthetic keys that could land in another app."""
        if self.prev_front is None or self.user_stopped:
            return  # after a person took over, focus stays where they put it
        try:
            ok = self.b.bring_to_front(self.prev_front, gentle=True)
        except Exception:
            ok = False
        self.log("restore", ok=bool(ok))

    def _close_app(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run_phases(self) -> None:
        planned = []
        for phase in self.script.get("phases", []):
            if "id" in phase:
                self.phases_by_id[phase["id"]] = phase
            if "repeat" in phase:
                target = self.phases_by_id.get(phase["repeat"])
                if target is None:
                    raise ScriptFailure(f"repeat of unknown phase {phase['repeat']!r}")
                phase = target
            if applies(phase, self.platform):
                planned.append(phase)
        for i, phase in enumerate(planned):
            try:
                self.run_phase(phase)
            except BaseException:
                # List the rest as not reached, so the report shows the whole plan.
                for j, rest in enumerate(planned[i + 1 :], self.phase_no + 1):
                    self.phase_log.append({**self._plan(rest, j), "status": "NOT_RUN"})
                raise

    def _plan(self, phase: dict, n: int) -> dict:
        described = describe_phase(phase, self.platform, getattr(self, "static_vars", {}))
        return {"name": phase_title(phase, described, n), **described}

    def _empty_here(self, phase: dict, described: dict) -> bool:
        """The phase has steps, elements or checks, but all for other
        platforms. (A phase that's empty everywhere is a deliberate
        screenshot point and still runs.)"""
        has_content = any(phase.get(k) for k in ("steps", "learn", "checks", "window"))
        return (
            has_content
            and not described["steps"]
            and not described["checks"]
            and not learn_descriptors(phase.get("learn", {}), self.platform)
            and pick(phase.get("window"), self.platform) is None
        )

    def run_phase(self, phase: dict) -> None:
        self.phase_no += 1
        n = self.phase_no
        entry = {**self._plan(phase, n), "status": "FAIL"}
        self.phase_log.append(entry)
        if self._empty_here(phase, entry):
            # Every step, element and check is for other platforms.
            entry.update(status="SKIP", seconds=0.0)
            if self.progress:
                self.progress(n)
            return
        self.phase_entry = entry
        self.phase_name = entry["name"]
        self.log("phase", name=entry["name"], number=n)
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
                raise ScriptFailure(f"no other window matching {other!r}", phase=n, action="window")
        elif pattern is not None:
            window = self.find_window(pattern, float(self.config.get("step_timeout", 5)))
            if window is None:
                raise ScriptFailure(f"no window matching {pattern!r}", phase=n, action="window")

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
                raise ScriptFailure(
                    "element not found: " + ", ".join(missing), phase=n, action="learn"
                )
            self.targets.update({h: label for label, h in handles.items() if h})

            sent, meta = build_steps(
                phase.get("steps", []),
                self.platform,
                handles,
                window,
                self.vars,
                timeout_scale=float(self.config.get("timeout_scale", 1)),
                secrets=self._resolve_secrets(phase.get("steps", []), n),
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
                self.phase_entry["screenshot"] = name

    def find(self, window: str | None, desc: dict) -> list:
        """find_elements by role/name, then keep those carrying `state`."""
        found = self.b.find_elements(window, role=desc.get("role"), name=desc.get("name"))
        if "state" in desc:
            found = [e for e in found if desc["state"] in (getattr(e, "states", None) or ())]
        return found

    def run_steps(self, sent: list[dict], meta: list[dict], phase: int) -> None:
        if not self.config.get("demo"):
            self._run_chunked(sent, meta, phase)
            return
        # Demo mode: one step at a time, logged with its target's bounds, with
        # a pause after each, and clicks through the real pointer so they
        # show up on video.
        pace = float(self.config.get("demo_pace", 0.8))
        for i, step in enumerate(sent):
            bounds = None
            if "handle" in step:
                try:
                    b = self.b.element_bounds(step["handle"])
                    bounds = [float(v) for v in b] if b else None
                except Exception:
                    bounds = None
            self.log(
                "step",
                phase=self.phase_name,
                action=step.get("action"),
                target=self.targets.get(step.get("handle")),
                bounds=bounds,
            )
            self.check_user(phase, i + 1)
            plain_click = (
                step.get("action") == "click"
                and bounds
                and step.get("button", "left") == "left"
                and int(step.get("clicks", 1)) == 1
            )
            if plain_click:
                x, y, w, h = bounds
                self.guard({"action": "click_at", "x": x + w / 2, "y": y + h / 2}, phase, i + 1)
                r = self.b.click_at(x + w / 2, y + h / 2, self.window)
                if not r.ok:
                    raise ScriptFailure(
                        f"click failed: {r.error or 'not ok'}",
                        phase=phase,
                        step=i + 1,
                        action="click",
                    )
            else:
                self._run_chunked([step], [meta[i]], phase, base=i)
            if pace:
                time.sleep(pace)

    def check_user(self, phase: int, number: int) -> None:
        """Stop if a person used the keyboard or mouse since our own last
        input: BAM must never fight a user for the machine."""
        if not self.yields:
            return
        try:
            idle = self.b.idle_seconds()
        except Exception:
            idle = None
        if idle is None:
            return
        ours = max(self.own_until, last_injected_at())
        now = time.monotonic()
        if now - idle > ours + 0.25:
            self.user_stopped = True
            # Numbers only (no window or app names), so a stop can be told
            # apart from a false alarm caused by our own input.
            self.log(
                "user",
                last_input_age_s=round(idle, 3),
                ours_age_s=round(now - last_injected_at(), 3),
                grace_left_s=round(max(0.0, self.own_until - now), 3),
            )
            raise ScriptFailure(
                "Run stopped: user input detected.", phase=phase, step=number, action="user"
            )

    def guard(self, step: dict, phase: int, number: int) -> None:
        """Before real input: the app must be the one receiving it. Raise it
        if needed; if something else still holds the foreground (or covers the
        target point), fail without sending anything. The message never names
        the other app: that would leak what else the user is running."""
        pid = self.app_pid
        if pid is None:
            return
        if step.get("action") == "key":
            probe = self.b.foreground_pid
        else:
            point = self._point_of(step)
            if point is None:
                return
            probe = lambda: self.b.pid_at(*point)  # noqa: E731
        owner = probe()
        if owner is None or owner == pid:
            return
        if self.prev_front is None:
            start = getattr(self, "start_front", None)
            front = start if start is not None else self.b.foreground_pid()
            if front is not None and front != pid:
                self.prev_front = front
        landed = self.b.bring_to_front(pid)
        self.log("raise", ok=bool(landed), attempts=list(getattr(self.b, "last_raise", None) or []))
        if probe() != pid:
            raise ScriptFailure(
                "Unexpected app in foreground.", phase=phase, step=number, action="foreground"
            )

    def _pointer_click(self, step: dict, phase: int, number: int) -> dict:
        """A click marked `pointer` becomes a click_at the element's centre,
        read right before it runs, so it gets the same point guard."""
        b = self.b.element_bounds(step["handle"])
        if not b:
            raise ScriptFailure(
                "pointer click: element reports no screen bounds",
                phase=phase,
                step=number,
                action="click",
            )
        x, y, w, h = b
        return {
            "action": "click_at",
            "x": x + w / 2,
            "y": y + h / 2,
            "window_id": self.window,
            "button": step.get("button", "left"),
            "clicks": int(step.get("clicks", 1)),
        }

    def _resolve_secrets(self, steps: list[dict], phase: int) -> dict[str, str]:
        """Read each `secret` reference (env:NAME) a phase uses, now, and
        remember the values so every output can be scrubbed of them."""
        out: dict[str, str] = {}
        for step in steps:
            ref = step.get("secret") if applies(step, self.platform) else None
            if not ref or ref in out:
                continue
            value = os.environ.get(ref[len("env:") :]) if ref.startswith("env:") else None
            if not value:
                raise ScriptFailure(f"secret {ref} is not set", phase=phase, action="type")
            out[ref] = value
            self.secret_values.add(value)
        return out

    def redact(self, obj):
        return _redact(obj, self.secret_values)

    def _password_gate(self, step: dict, phase: int, number: int) -> None:
        """Refuse to type into a password field unless this machine's own
        config allows it, for every app (true) or the named ones."""
        try:
            secret = self.b.is_password(step["handle"])
        except Exception:
            secret = None
        if not secret:
            return
        allow = self.config.get("allow_password_fields", False)
        allowed = allow is True
        if isinstance(allow, list) and allow:
            win = next((w for w in self.b.list_windows() if w.id == self.window), None)
            allowed = win is not None and any(win.matches(p) for p in allow)
        if not allowed:
            raise ScriptFailure(
                "password field: not allowed by this machine's config",
                phase=phase,
                step=number,
                action="type",
            )
        if not step.get("secret"):
            raise ScriptFailure(
                'password field: use "secret", not literal text',
                phase=phase,
                step=number,
                action="type",
            )

    def _point_of(self, step: dict) -> tuple[float, float] | None:
        if step.get("action") == "click_at":
            return float(step["x"]), float(step["y"])
        if "from_x" in step and "from_y" in step:
            return float(step["from_x"]), float(step["from_y"])
        if "from_handle" in step:
            b = self.b.element_bounds(step["from_handle"])
            if b:
                x, y, w, h = b
                return x + w / 2, y + h / 2
        return None

    def _run_chunked(self, sent: list[dict], meta: list[dict], phase: int, base: int = 0) -> None:
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
                        step=base + start + 1,
                        action="shell",
                    )
                start += 1
                continue
            self.check_user(phase, base + start + 1)
            if sent[start].get("action") == "click" and sent[start].get("pointer"):
                sent[start] = self._pointer_click(sent[start], phase, base + start + 1)
            end = start
            if is_real_input(sent[start]):
                # Its own chunk, checked right before it runs.
                self.guard(sent[start], phase, base + start + 1)
            else:
                while (
                    end < len(sent) - 1
                    and not meta[end]
                    and sent[end + 1].get("action") != "shell"
                    and not is_real_input(sent[end + 1])
                ):
                    end += 1
            chunk = sent[start : end + 1]
            for offset, step in enumerate(chunk):
                if step.get("action") == "type":
                    self._password_gate(step, phase, base + start + offset + 1)
            results = self.b.run_sequence(chunk, stop_on_error=True)
            # Then check again: a person may have touched the machine while
            # that chunk ran, even if it sent no input itself.
            self.check_user(phase, base + start + 1)
            for offset, r in enumerate(results):
                i = start + offset
                if not r.get("ok"):
                    raise ScriptFailure(
                        f"{r.get('action')} failed: {r.get('error') or 'not ok'}",
                        phase=phase,
                        step=base + i + 1,
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
                            f"{r.get('action')}: expected {want!r}, got {got!r}",
                            phase=phase,
                            step=base + i + 1,
                            action=r.get("action"),
                        )
                if "expect_not" in m:
                    avoid = expand(m["expect_not"], self.vars)
                    got = r.get("value")
                    if str(got) == str(avoid):
                        raise ScriptFailure(
                            f"{r.get('action')}: value unchanged, still {got!r}",
                            phase=phase,
                            step=base + i + 1,
                            action=r.get("action"),
                        )
            if len(results) < len(chunk):
                raise ScriptFailure(
                    "sequence stopped early", phase=phase, step=base + start + len(results) + 1
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
                if want_present:
                    # Web views (WKWebView mid-transition) can briefly expose
                    # an almost empty tree: retry until step_timeout.
                    want = int(spec["count"]) if "count" in spec else None
                    deadline = time.monotonic() + float(self.config.get("step_timeout", 5))
                    while (
                        (len(found) != want) if want is not None else not found
                    ) and time.monotonic() < deadline:
                        time.sleep(0.2)
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
                n = self.count_windows(pick(self.config["window"], self.platform))
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


def _redact(obj, secrets: set[str]):
    """A copy of `obj` with every secret value replaced by •••."""
    if not secrets:
        return obj
    if isinstance(obj, str):
        for value in sorted(secrets, key=len, reverse=True):
            obj = obj.replace(value, "•••")
        return obj
    if isinstance(obj, dict):
        return {k: _redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v, secrets) for v in obj]
    return obj


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
    if script.get("covers"):
        result["covers"] = script["covers"]
    started = time.monotonic()

    run = None
    if platform not in script.get("platforms", [platform]):
        result["status"] = "SKIP"
    else:
        run = _Run(backend, platform, config, out_dir, script)
        run.progress = progress
        recording = None
        try:
            run.setup()
            run.launch()
            run.start_watchdog()
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
            # Before the watchdog stops: a teardown step into a hung app
            # must still be unblocked by it.
            cleanup = run.run_teardown()
            if cleanup:
                result["teardown"] = cleanup
            run.stop_watchdog()
            if getattr(run, "hung_after", None) is not None:
                # Whatever the unblocked call said, the cause is the hang.
                result["status"] = "FAIL"
                where = {k: v for k, v in (result.get("failure") or {}).items() if k == "phase"}
                result["failure"] = {
                    **where,
                    "action": "hang",
                    "reason": f"app not responding (killed after {run.hung_after:g} s)",
                }
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
        final = describe_checks(script.get("checks", []), platform)
        if final:
            result["checks"] = final
        (out_dir / "timeline.json").write_text(
            json.dumps(run.redact(run.timeline), indent=1), encoding="utf-8"
        )
        if run.window is not None:
            result["window"] = run.window
        result["screenshots"] = run.screenshots
        if run.manual:
            result["manual"] = run.manual
        if (out_dir / "fail.png").exists():
            result["fail_screenshot"] = "fail.png"

    result["finished_at"] = _now()
    result["seconds"] = round(time.monotonic() - started, 2)
    if run is not None:
        result = run.redact(result)
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result
