"""report.html for a BAM run folder: one card per script, green for a pass
with its proof screenshots, red for a failure with the screenshot taken at
the failing step and the reason. Self-contained apart from the images,
which it links relative to the run folder."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--border:#e3e6eb;
--pass:#1a7f37;--pass-bg:#e7f6ec;--fail:#c62828;--fail-bg:#fdecec;--warn:#9a6700;--warn-bg:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa1ad;
--border:#2a2f3a;--pass:#3fb950;--pass-bg:#12261a;--fail:#ff6b6b;--fail-bg:#2d1416;--warn:#d29922;--warn-bg:#2a2210}}
*{box-sizing:border-box}body{margin:0;padding:24px 16px;background:var(--bg);color:var(--text);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--muted);margin-bottom:16px}.tally{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.pill{padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px}
.pill.pass{background:var(--pass-bg);color:var(--pass)}.pill.fail{background:var(--fail-bg);color:var(--fail)}
.pill.other{background:var(--warn-bg);color:var(--warn)}
.card{background:var(--card);border:1px solid var(--border);border-left:6px solid var(--muted);
border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card.pass{border-left-color:var(--pass)}.card.fail{border-left-color:var(--fail)}
.card header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.card h2{font-size:15px;margin:0}.card .sub{color:var(--muted);font-size:12px}
.reason{margin:10px 0;padding:8px 10px;border-radius:6px;background:var(--fail-bg);
color:var(--fail);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;overflow-wrap:anywhere}
.failshot img{width:100%;max-width:720px;border:2px solid var(--fail);border-radius:6px;
display:block}
.shots{display:flex;gap:8px;overflow-x:auto;padding-top:10px}
.shots a{flex:0 0 auto;text-align:center;color:var(--muted);font-size:11px;
text-decoration:none}
.shots img{height:120px;border:1px solid var(--border);border-radius:4px;display:block;
margin-bottom:2px}
.manual{margin:8px 0 0;padding-left:18px;color:var(--warn)}
"""

_LABEL = {
    "PASS": "PASS",
    "FAIL": "FAIL",
    "SKIP": "SKIP",
    "MANUAL": "NEEDS A HUMAN",
    "NOT_RUN": "NOT RUN",
}


def _result(run_dir: Path, folder: str) -> dict:
    try:
        return json.loads((run_dir / folder / "result.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _card(run_dir: Path, entry: dict) -> str:
    status = entry.get("status", "NOT_RUN")
    folder = entry["folder"]
    res = _result(run_dir, folder)
    cls = {"PASS": "pass", "FAIL": "fail"}.get(status, "other")
    pill = "pass" if cls == "pass" else "fail" if cls == "fail" else "other"
    sub = []
    if entry.get("attempts", 1) > 1:
        sub.append(
            f"passed on attempt {entry['attempts']}"
            if status == "PASS"
            else f"{entry['attempts']} attempts"
        )
    if "seconds" in res:
        sub.append(f"{res['seconds']} s")
    out = [
        f'<section class="card {cls}">',
        f'<header><span class="pill {pill}">{_LABEL.get(status, status)}</span>'
        f"<h2>{escape(entry['name'])}</h2>"
        f'<span class="sub">{escape(" · ".join(sub))}</span></header>',
    ]
    failure = entry.get("failure") or res.get("failure")
    if status == "FAIL" and failure:
        where = []
        if "phase" in failure:
            where.append(f"phase {failure['phase']}")
        if "step" in failure:
            where.append(f"step {failure['step']}")
        prefix = f"{', '.join(where)}: " if where else ""
        out.append(f'<div class="reason">{escape(prefix + failure.get("reason", ""))}</div>')
        if (run_dir / folder / "fail.png").exists():
            src = escape(f"{folder}/fail.png")
            img = f'<img src="{src}" alt="Screen at the failure">'
            out.append(f'<a class="failshot" href="{src}">{img}</a>')
    for note in res.get("manual", []):
        out.append(f'<ul class="manual"><li>{escape(note)}</li></ul>')
    shots = res.get("screenshots", [])
    if shots:
        out.append('<div class="shots">')
        for i, s in enumerate(shots, 1):
            src = escape(f"{folder}/{s}")
            out.append(
                f'<a href="{src}"><img src="{src}" alt="Phase {i}" loading="lazy">phase {i}</a>'
            )
        out.append("</div>")
    if res.get("video"):
        out.append(f'<p class="sub"><a href="{escape(folder)}/run.mp4">video</a></p>')
    out.append("</section>")
    return "\n".join(out)


def write_report(run_dir: Path | str) -> Path:
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    entries = run.get("scripts", [])
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.get("status", "NOT_RUN")] = counts.get(e.get("status", "NOT_RUN"), 0) + 1
    tally = "".join(
        f'<span class="pill {"pass" if k == "PASS" else "fail" if k == "FAIL" else "other"}">'
        f"{n} {_LABEL.get(k, k).lower()}</span>"
        for k, n in sorted(
            counts.items(),
            key=lambda kv: ["FAIL", "PASS"].index(kv[0]) if kv[0] in ("FAIL", "PASS") else 2,
        )
    )
    # Failures first: they're what a reader came for.
    ordered = sorted(entries, key=lambda e: e.get("status") != "FAIL")
    body = "\n".join(_card(run_dir, e) for e in ordered)
    meta = " · ".join(
        escape(str(x))
        for x in (
            run.get("platform", "?"),
            f"commit {run.get('commit', '?')}",
            run.get("started", ""),
        )
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BAM run report</title><style>{_CSS}</style></head>
<body><main>
<h1>BAM run report</h1>
<div class="meta">{meta}</div>
<div class="tally">{tally}</div>
{body}
</main></body></html>
"""
    path = run_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    return path
