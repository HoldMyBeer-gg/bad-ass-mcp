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
from pathlib import Path

from .report import write_report
from .runner import run_script

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
    line: later wins."""
    return {**suite, **script.get("configuration", {}), **overrides}


def _latest_run(results: Path, commit: str, platform: str) -> Path | None:
    if not results.is_dir():
        return None
    for d in sorted(results.iterdir(), reverse=True):
        try:
            run = json.loads((d / "run.json").read_text())
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
) -> Path:
    """Run `scripts` in order; returns the run folder (with report.html)."""
    results = Path(results_dir)
    scripts = [Path(s) for s in scripts]
    run_dir = _latest_run(results, commit, platform) if resume else None
    previous: dict[str, dict] = {}
    if run_dir is not None:
        old = json.loads((run_dir / "run.json").read_text())
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
    run = {
        "commit": commit,
        "platform": platform,
        "config": config,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scripts": entries,
    }

    def save() -> None:
        (run_dir / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False))

    fail_fast = config.get("fail_fast", False)
    retries = int(config.get("retries", 0))
    stop = False
    for idx, entry in enumerate(entries):
        prior = previous.get(entry["folder"])
        if prior and prior.get("status") in _DONE:
            entry.update({k: prior[k] for k in ("status", "attempts") if k in prior})
            continue
        if stop:
            entry["status"] = "NOT_RUN"
            continue
        out = run_dir / entry["folder"]
        attempts = 0
        while True:
            attempts += 1
            if out.exists():
                shutil.rmtree(out)
            cfg = effective_config(config, loaded[idx], overrides)
            result = run_script(backend, scripts[idx], platform, cfg, out)
            if result["status"] != "FAIL" or attempts > retries:
                break
        entry["status"] = result["status"]
        entry["attempts"] = attempts
        if "failure" in result:
            entry["failure"] = result["failure"]
        else:
            entry.pop("failure", None)
        save()
        if result["status"] == "FAIL" and (
            fail_fast is True or (fail_fast == "first_script" and idx == 0)
        ):
            stop = True

    run["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    write_report(run_dir)
    return run_dir
