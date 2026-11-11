"""`bad-ass-mcp run`: run BAM scripts from the command line.

bad-ass-mcp run smoke/ --config bam.json [--resume] [--fail-fast [first_script]]
    [--video [on_failure]] [--retries N] [--no-screenshots] [--results DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import subprocess
import sys
from pathlib import Path

_PLATFORMS = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}


# Colours from the TMS9928A, the video chip in the Coleco Adam (and the
# ColecoVision, TI-99/4A, MSX1): the 1980s home-computer palette.
_TMS = {
    "dark_blue": (84, 85, 237),
    "cyan": (66, 235, 245),
    "light_yellow": (230, 206, 128),
    "medium_green": (33, 200, 66),
    "dark_red": (212, 82, 77),
    "gray": (204, 204, 204),
    "white": (255, 255, 255),
}
_STATUS_COLOR = {"PASS": "medium_green", "FAIL": "dark_red", "SKIP": "gray", "NOT_RUN": "gray"}


def _fg(name: str, text: str, color: bool) -> str:
    if not color:
        return text
    r, g, b = _TMS[name]
    return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


def use_color(stream) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def render_bar(
    done: int,
    total: int,
    *,
    width: int = 30,
    label: str = "",
    color: bool = True,
    results: list[tuple[int, str]] | None = None,
) -> str:
    """`[████░░░░]  25% done · 1 passed · 1 failed label`, in TMS9928A colours.

    `results` is (units, status) per finished script: the filled part is
    split by result (green pass, red fail, grey other), with the script still
    running in cyan after them, so "100%" can't be read as "all passed"."""
    frac = 1.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = round(frac * width)
    parts, used = [], 0
    for units, status in results or []:
        n = min(filled - used, round(units / total * width) if total > 0 else 0)
        if n > 0:
            parts.append(_fg(_STATUS_COLOR.get(status, "light_yellow"), "█" * n, color))
            used += n
    if filled > used:
        parts.append(_fg("cyan", "█" * (filled - used), color))
    bar = "".join(parts) + _fg("dark_blue", "░" * (width - filled), color)
    pct = _fg("light_yellow", f"{round(frac * 100):3d}% done", color)
    counts = []
    for status, word in (("PASS", "passed"), ("FAIL", "failed")):
        n = sum(1 for _, s in results or [] if s == status)
        if n:
            counts.append(_fg(_STATUS_COLOR[status], f"{n} {word}", color))
    tally = "".join(f" · {c}" for c in counts)
    frame = _fg("dark_blue", "[", color), _fg("dark_blue", "]", color)
    return f"{frame[0]}{bar}{frame[1]} {pct}{tally} {label}".rstrip()


def _duration(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(round(seconds), 60)
    return f"{m}m{s:02d}s"


def print_summary(run_dir: Path, *, color: bool = False) -> None:
    run = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    for s in run["scripts"]:
        status = s.get("status", "NOT_RUN")
        line = f"{_fg(_STATUS_COLOR.get(status, 'gray'), f'{status:8}', color)} {s['name']}"
        if "seconds" in s:
            line += f"  ({_duration(s['seconds'])})"
        if s.get("attempts", 1) > 1:
            line += f"  [attempt {s['attempts']}]"
        if s.get("failure"):
            line += f"\n         {s['failure'].get('reason', '')}"
        print(line)
    totals = run.get("totals", {})
    words = {
        "PASS": "passed",
        "FAIL": "failed",
        "SKIP": "skipped",
        "MANUAL": "need a human",
        "NOT_RUN": "not run",
    }
    parts = [
        f"{n} {words.get(k, k.lower())}"
        for k, n in sorted(
            totals.items(), key=lambda kv: list(words).index(kv[0]) if kv[0] in words else 99
        )
    ]
    print(
        f"\n{', '.join(parts)} in {_duration(run.get('seconds', 0))}"
        f"  ·  {run.get('platform')}  ·  commit {run.get('commit')}  ·  {run.get('started_at', '')}"
    )
    print(f"report: {Path(run_dir) / 'report.html'}")


def _collect(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    return out


def _commit(scripts: list[Path] | None = None) -> str:
    """HEAD of the repo the scripts live in (the app under test), not of
    wherever the command was typed."""
    where = Path(scripts[0]).resolve().parent if scripts else Path.cwd()
    try:
        r = subprocess.run(
            ["git", "-C", str(where), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
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
    args.suite_config = _load_config(args.config) if args.config else {}
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


def _load_config(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        msg = f"config {path}: not valid JSON at line {e.lineno} column {e.colno}: {e.msg}"
    except OSError as e:
        msg = f"config {path}: {e.strerror or e}"
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _problems(scripts: list[Path], config_path: str | None) -> list[dict]:
    """Schema problems in the scripts (and the suite config), as
    {file, path, message}; unreadable JSON is one problem for its file."""
    from .validate import validate_config, validate_script

    out: list[dict] = []
    files = [(Path(config_path), validate_config)] if config_path else []
    files += [(p, validate_script) for p in scripts]
    for path, check in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append({"file": str(path), "path": "", "message": f"not valid JSON: {e}"})
            continue
        out += [{"file": str(path), "path": i.path, "message": i.message} for i in check(data)]
    return out


def _print_problems(problems: list[dict]) -> None:
    for p in problems:
        where = f"{p['file']}: {p['path']}: " if p["path"] else f"{p['file']}: "
        print(where + p["message"])


_INPUT_GRABBERS = ("vmware", "virtualboxvm", "vmconnect", "prl_client_app")


def _process_names() -> list[str]:
    """Running process names, best effort, for the VM warning only. Printed
    nowhere and stored nowhere."""
    try:
        if os.name == "nt":
            cmd = ["tasklist", "/fo", "csv", "/nh"]
        else:
            cmd = ["ps", "-A", "-o", "comm="]
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        return [line.split(",")[0].strip('"') for line in r.stdout.splitlines() if line]
    except Exception:
        return []


def vm_running() -> bool:
    """A VM app is open. VMs grab the keyboard and mouse with a low-level hook
    and swallow synthetic input even when our app is in front."""
    names = [n.lower().rsplit("/", 1)[-1] for n in _process_names()]
    return any(g in n for n in names for g in _INPUT_GRABBERS)


def hands_off_notice(names: list[str], total: int, *, vm_running: bool = False) -> str | None:
    if not names:
        return None
    msg = (
        f"{len(names)} of {total} scripts use real keyboard/mouse input ({', '.join(names)}). "
        "Hands off the keyboard and mouse until the run finishes; go grab a coffee."
    )
    if vm_running:
        msg += (
            " A virtual machine window is open: VMs grab the keyboard and mouse and can "
            "swallow this input. Release its grab (Ctrl+Alt) or minimize it first."
        )
    return msg


def _secret_names(scripts: list[Path], platform: str) -> list[str]:
    from .describe import secret_hints

    names = []
    for p in scripts:
        s = json.loads(Path(p).read_text(encoding="utf-8"))
        if platform in s.get("platforms", [platform]):
            hits = secret_hints(s, platform)
            if hits:
                names.append(f"{s.get('name') or Path(p).stem} ({', '.join(hits)})")
    return names


def _real_input_names(scripts: list[Path], platform: str) -> list[str]:
    from .describe import needs_foreground

    names = []
    for p in scripts:
        s = json.loads(Path(p).read_text(encoding="utf-8"))
        if platform in s.get("platforms", [platform]) and needs_foreground(s, platform):
            names.append(s.get("name") or Path(p).stem)
    return names


def check_main(argv: list[str]) -> int:
    """`bad-ass-mcp check PATH... [--config bam.json] [--json]`: validate
    scripts without running them. Exit 1 if anything is wrong."""
    ap = argparse.ArgumentParser(prog="bad-ass-mcp check", description="Validate BAM scripts.")
    ap.add_argument("paths", nargs="+", help="script files or folders of *.json scripts")
    ap.add_argument("--config", help="suite config JSON to validate too")
    ap.add_argument("--json", action="store_true", help="problems as a JSON list")
    args = ap.parse_args(argv)
    problems = _problems(_collect(args.paths), args.config)
    if args.json:
        print(json.dumps(problems, indent=2))
    elif problems:
        _print_problems(problems)
    else:
        scripts = _collect(args.paths)
        print(f"ok: {len(scripts)} script(s) valid")
        here = _PLATFORMS.get(_platform.system(), "linux")
        real = _real_input_names(scripts, here)
        if real:
            print(f"needs foreground on {here}: {', '.join(real)}")
        secret = _secret_names(scripts, here)
        if secret:
            print(
                "types into what looks like a password or secret (blocked unless this "
                f"machine's config sets allow_password_fields): {', '.join(secret)}"
            )
    return 1 if problems else 0


def _utf8_console() -> None:
    """The summary uses non-ASCII ("·", bar blocks). A Windows console that
    is UTF-8 still gets cp1252 bytes from Python when output is piped, and
    shows them as replacement characters."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str]) -> int:
    from ..server import _backend
    from . import suite

    _utf8_console()
    args = parse(argv)
    problems = _problems(args.scripts, args.config)
    if problems:
        # Refuse before launching anything: a typo shouldn't cost a run.
        _print_problems(problems)
        print(f"{len(problems)} problem(s); nothing was run. `bad-ass-mcp check` to re-check.")
        return 2
    run_suite = suite.run_suite
    real = _real_input_names(args.scripts, args.platform)
    notice = hands_off_notice(real, len(args.scripts), vm_running=bool(real) and vm_running())
    if notice:
        print(notice, file=sys.stderr)
    secret = _secret_names(args.scripts, args.platform)
    if secret:
        print(
            "Types into what looks like a password or secret (blocked unless this machine's "
            f"config sets allow_password_fields): {', '.join(secret)}",
            file=sys.stderr,
        )
    color = use_color(sys.stdout)
    live = sys.stderr.isatty()

    finished: list[tuple[int, str]] = []
    mark = [0]  # units done when the last script finished

    def progress(e: dict) -> None:
        if e["kind"] == "script":
            finished.append((e["done"] - mark[0], e.get("status", "")))
            mark[0] = e["done"]
        if not live:
            return
        label = e.get("script") or e.get("name") or ""
        if e["kind"] == "phase" and e.get("phase"):
            label += f" · phase {e['phase']}"
        sys.stderr.write(
            "\r\x1b[2K"
            + render_bar(e["done"], e["total"], label=label, color=color, results=finished)
        )
        sys.stderr.flush()

    try:
        backend = _backend()
    except RuntimeError as e:
        # e.g. the platform extra isn't installed: one line, not a traceback.
        raise SystemExit(f"bad-ass-mcp run: {e}") from None
    run_dir = run_suite(
        backend,
        args.scripts,
        args.platform,
        args.suite_config,
        args.results,
        commit=args.commit or _commit(args.scripts),
        resume=args.resume,
        overrides=args.overrides,
        progress=progress,
    )
    if live:
        sys.stderr.write("\r\x1b[2K")
    print_summary(run_dir, color=color)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return 1 if any(s.get("status") == "FAIL" for s in run["scripts"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
