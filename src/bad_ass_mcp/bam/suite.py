"""Run a list of BAM scripts into one results folder.

    results/<timestamp>_<commit>_<platform>/
      run.json              order, status of each, commit, platform, config
      01_<script>/          result.json, phase-N.png, fail.png, tree.json, run.mp4
      report.html

run.json is rewritten after every script, so a run that dies (or a model
whose context was cleared) can resume: on the same commit, scripts that
already passed are kept and the rest run again.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from .report import write_report
from .runner import _now, run_script

_DONE = {"PASS", "SKIP"}


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return s or "script"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def effective_config(suite: dict, script: dict, overrides: dict) -> dict:
    """Suite file, then the script's own `configuration`, then the command
    line: later wins. Keys that widen what a script may do come only from
    the suite file and the command line."""
    from .validate import RUNNER_ONLY

    own = {k: v for k, v in script.get("configuration", {}).items() if k not in RUNNER_ONLY}
    return {**suite, **own, **overrides}


def _latest_run(results: Path, commit: str, platform: str) -> Path | None:
    if not results.is_dir():
        return None
    for d in sorted(results.iterdir(), reverse=True):
        try:
            run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if run.get("commit") == commit and run.get("platform") == platform:
            return d
    return None


def _new_run_dir(results: Path, commit: str, platform: str) -> Path:
    base = f"{time.strftime('%Y-%m-%dT%H-%M-%S')}_{_safe(commit)}_{_safe(platform)}"
    d = results / base
    n = 2
    while d.exists():
        d = results / f"{base}-{n}"
        n += 1
    d.mkdir(parents=True)
    return d


def run_suite(
    backend,
    scripts: list[Path | str],
    platform: str,
    config: dict,
    results_dir: Path | str,
    *,
    commit: str = "unknown",
    resume: bool = False,
    overrides: dict | None = None,
    progress=None,
) -> Path:
    """Run `scripts` in order; returns the run folder (with report.html).

    `progress(event)` gets {"kind": "phase", "done", "total", "script",
    "phase"} after every phase and {"kind": "script", "name", "status",
    "done", "total"} after every script. Work is counted in phases (plus one
    unit per script for its final checks), so a bar moves smoothly."""
    results = Path(results_dir)
    scripts = [Path(s) for s in scripts]
    run_dir = _latest_run(results, commit, platform) if resume else None
    previous: dict[str, dict] = {}
    if run_dir is not None:
        old = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        previous = {s["folder"]: s for s in old.get("scripts", [])}
    else:
        run_dir = _new_run_dir(results, commit, platform)

    overrides = overrides or {}
    config = {**config, **overrides}
    loaded = [_load(p) for p in scripts]
    entries = []
    for i, path in enumerate(scripts, 1):
        name = loaded[i - 1].get("name") or path.stem
        entries.append({"name": name, "script": str(path), "folder": f"{i:02d}_{_safe(name)}"})
    units = [len(s.get("phases", [])) + 1 for s in loaded]
    total = sum(units)
    started = time.monotonic()
    run = {
        "commit": commit,
        "platform": platform,
        "config": config,
        "started_at": _now(),
        "scripts": entries,
    }

    def save() -> None:
        (run_dir / "run.json").write_text(
            json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def emit(event: dict) -> None:
        if progress:
            progress({**event, "total": total})

    fail_fast = config.get("fail_fast", False)
    retries = int(config.get("retries", 0))
    stop = False
    base = 0
    for idx, entry in enumerate(entries):
        prior = previous.get(entry["folder"])
        if prior and prior.get("status") in _DONE:
            entry.update({k: prior[k] for k in ("status", "attempts", "seconds") if k in prior})
        elif stop:
            entry["status"] = "NOT_RUN"
        else:
            out = run_dir / entry["folder"]
            attempts = 0

            def on_phase(n: int, _base=base, _idx=idx, _name=entry["name"]) -> None:
                done = _base + min(n, units[_idx] - 1)
                emit({"kind": "phase", "done": done, "script": _name, "phase": n})

            while True:
                attempts += 1
                if out.exists():
                    shutil.rmtree(out)
                cfg = effective_config(config, loaded[idx], overrides)
                result = run_script(backend, scripts[idx], platform, cfg, out, progress=on_phase)
                user = (result.get("failure") or {}).get("action") == "user"
                if result["status"] != "FAIL" or attempts > retries or user:
                    break
            entry["status"] = result["status"]
            entry["attempts"] = attempts
            entry["seconds"] = result.get("seconds", 0.0)
            if "failure" in result:
                entry["failure"] = result["failure"]
            else:
                entry.pop("failure", None)
            save()
            if result["status"] == "FAIL" and (
                fail_fast is True or (fail_fast == "first_script" and idx == 0)
            ):
                stop = True
            if (result.get("failure") or {}).get("action") == "user":
                stop = True  # someone is using the machine: never carry on
        base += units[idx]
        emit({"kind": "phase", "done": base, "script": entry["name"], "phase": None})
        emit({"kind": "script", "done": base, "name": entry["name"], "status": entry["status"]})

    run["finished_at"] = _now()
    run["seconds"] = round(time.monotonic() - started, 2)
    run["totals"] = dict(Counter(e["status"] for e in entries))
    save()
    write_report(run_dir)
    return run_dir
