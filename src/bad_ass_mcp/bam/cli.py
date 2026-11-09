"""`bad-ass-mcp run`: run BAM scripts from the command line.

bad-ass-mcp run smoke/ --config bam.json [--resume] [--fail-fast [first_script]]
    [--video [on_failure]] [--retries N] [--no-screenshots] [--results DIR]
"""

from __future__ import annotations

import argparse
import json
import platform as _platform
import subprocess
import sys
from pathlib import Path

_PLATFORMS = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}


def _collect(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    return out


def _commit() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="bad-ass-mcp run", description="Run BAM scripts.")
    ap.add_argument("paths", nargs="+", help="script files or folders of *.json scripts")
    ap.add_argument("--config", help="suite config JSON (window, app, defaults)")
    ap.add_argument("--results", default="bam-results", help="results folder (default bam-results)")
    ap.add_argument("--platform", choices=sorted(_PLATFORMS.values()), help="default: this OS")
    ap.add_argument("--commit", help="app commit to stamp the run with (default: git HEAD here)")
    ap.add_argument("--resume", action="store_true", help="continue the last run on this commit")
    ap.add_argument("--fail-fast", nargs="?", const="true", choices=["true", "first_script"])
    ap.add_argument("--video", nargs="?", const="true", choices=["true", "on_failure"])
    ap.add_argument("--retries", type=int)
    ap.add_argument("--no-screenshots", action="store_true", help="no proof screenshot per phase")
    args = ap.parse_args(argv)

    args.scripts = _collect(args.paths)
    args.suite_config = (
        json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    )
    o: dict = {}
    if args.fail_fast:
        o["fail_fast"] = True if args.fail_fast == "true" else args.fail_fast
    if args.video:
        o["record_video"] = True if args.video == "true" else args.video
    if args.retries is not None:
        o["retries"] = args.retries
    if args.no_screenshots:
        o["screenshot_proof"] = False
    args.overrides = o
    args.platform = args.platform or _PLATFORMS.get(_platform.system(), "linux")
    return args


def main(argv: list[str]) -> int:
    from ..server import _backend
    from .suite import run_suite

    args = parse(argv)
    run_dir = run_suite(
        _backend(),
        args.scripts,
        args.platform,
        args.suite_config,
        args.results,
        commit=args.commit or _commit(),
        resume=args.resume,
        overrides=args.overrides,
    )
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    for s in run["scripts"]:
        line = f"{s.get('status', 'NOT_RUN'):8} {s['name']}"
        if s.get("failure"):
            line += f"  ({s['failure'].get('reason', '')})"
        print(line)
    print(f"\nreport: {run_dir / 'report.html'}")
    return 1 if any(s.get("status") == "FAIL" for s in run["scripts"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
