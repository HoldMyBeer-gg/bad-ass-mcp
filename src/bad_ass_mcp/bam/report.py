"""report.html for a BAM run folder: one card per script, green for a pass
with its proof screenshots, red for a failure with the screenshot taken at
the failing step and the reason. Self-contained apart from the images,
which it links relative to the run folder."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

_LIGHTBOX = """
<div id="lightbox" role="dialog" aria-modal="true" aria-label="Screenshot" hidden>
  <button class="lb-close" type="button" aria-label="Close">✕</button>
  <button class="lb-prev" type="button" aria-label="Previous screenshot">‹</button>
  <figure><img alt=""><figcaption></figcaption></figure>
  <button class="lb-next" type="button" aria-label="Next screenshot">›</button>
</div>
<script>
(() => {
  const btn = document.getElementById("expand");
  if (btn) btn.onclick = () => {
    const all = [...document.querySelectorAll(".phase details")];
    const open = !all.every((d) => d.open);
    all.forEach((d) => (d.open = open));
    btn.textContent = open ? "Collapse all phases" : "Expand all phases";
  };
})();
(() => {
  const links = [...document.querySelectorAll("a[data-shot]")];
  const box = document.getElementById("lightbox");
  if (!links.length || !box) return;
  const img = box.querySelector("img"), cap = box.querySelector("figcaption");
  let i = 0;
  const show = (n) => {
    i = (n + links.length) % links.length;
    img.src = links[i].getAttribute("href");
    cap.textContent = `${links[i].dataset.caption}  (${i + 1} of ${links.length})`;
  };
  const open = (n) => { show(n); box.hidden = false; document.body.style.overflow = "hidden"; };
  const close = () => { box.hidden = true; document.body.style.overflow = ""; links[i].focus(); };
  links.forEach((a, n) => a.addEventListener("click", (e) => { e.preventDefault(); open(n); }));
  box.querySelector(".lb-prev").onclick = () => show(i - 1);
  box.querySelector(".lb-next").onclick = () => show(i + 1);
  box.querySelector(".lb-close").onclick = close;
  box.addEventListener("click", (e) => { if (e.target === box) close(); });
  document.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(i - 1);
    else if (e.key === "ArrowRight") show(i + 1);
  });
})();
</script>
"""

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
.covers{margin:8px 0 0;color:var(--text)}.covers b{color:var(--muted);font-weight:600}
.phases{margin:10px 0 0;padding:0;list-style:none;font-size:13px}
.phase{border-top:1px solid var(--border)}.phase:first-child{border-top:0}
.phase summary,.phase .nodetail{display:flex;gap:8px;align-items:baseline;padding:5px 0;
cursor:pointer}
.phase .nodetail{cursor:default;padding-left:14px}
.phase summary::marker{color:var(--muted)}
.phase .pn{color:var(--muted);font-variant-numeric:tabular-nums;min-width:1.4em;text-align:right}
.phase .pname{font-weight:600}.phase.skip .pname{color:var(--muted);font-weight:400}
.m.ok{color:var(--pass)}.m.bad{color:var(--fail)}.m.skip{color:var(--muted)}
.phase .t{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.steps{margin:0 0 8px;padding:0 0 0 46px;list-style:none;font-size:12.5px}
.final .steps{padding-left:0}
.final h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
margin:12px 0 4px}
.step{padding:1px 0 1px 20px;position:relative;color:var(--muted)}
.step::before{content:"·";position:absolute;left:4px}
.step.ok,.step.bad{color:var(--text)}.step.ok::before{content:"✓";color:var(--pass)}
.step.bad{color:var(--fail);font-weight:600}.step.bad::before{content:"✗";color:var(--fail)}
.step.expect::after{content:"expect";margin-left:8px;font-size:10px;text-transform:uppercase;
letter-spacing:.05em;padding:0 5px;border-radius:4px;border:1px solid var(--border);
color:var(--muted);font-weight:400}
.step.pause{font-style:italic;opacity:.7}
.toolbar{margin:-8px 0 14px}.toolbar button{font:inherit;font-size:12px;color:var(--muted);
background:var(--card);border:1px solid var(--border);border-radius:6px;padding:3px 10px;
cursor:pointer}
#lightbox{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;
justify-content:center;gap:12px;padding:16px;z-index:10}#lightbox[hidden]{display:none}
#lightbox figure{margin:0;max-width:min(1200px,100%);text-align:center}
#lightbox img{max-width:100%;max-height:82vh;border-radius:6px;display:block;margin:0 auto}
#lightbox figcaption{color:#e6e8ec;font-size:13px;margin-top:8px}
#lightbox button{background:rgba(255,255,255,.12);color:#fff;border:0;border-radius:999px;
width:44px;height:44px;font-size:24px;line-height:1;cursor:pointer;flex:0 0 auto}
#lightbox button:hover,#lightbox button:focus-visible{background:rgba(255,255,255,.28);outline:none}
#lightbox .lb-close{position:absolute;top:16px;right:16px;font-size:18px}
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
            cap = escape(f"{entry['name']} · at the failure", quote=True)
            out.append(f'<a class="failshot" href="{src}" data-shot data-caption="{cap}">{img}</a>')
    for note in res.get("manual", []):
        out.append(f'<ul class="manual"><li>{escape(note)}</li></ul>')
    if res.get("covers"):
        out.append(f'<p class="covers"><b>Tests:</b> {escape(res["covers"])}</p>')
    phases = res.get("phases", [])
    if phases:
        out.append(_phases(phases, failure if status == "FAIL" else None, status == "FAIL"))
    if res.get("checks"):
        final_failed = status == "FAIL" and failure and "phase" not in failure
        k = " bad" if final_failed else " ok" if status in ("PASS", "MANUAL") else ""
        items = "".join(f'<li class="step expect{k}">{escape(c)}</li>' for c in res["checks"])
        out.append(f'<div class="final"><h3>Final checks</h3><ol class="steps">{items}</ol></div>')
    shots = res.get("screenshots", [])
    if shots:
        out.append('<div class="shots">')
        names = [p.get("name") for p in res.get("phases", [])]
        for i, s in enumerate(shots, 1):
            src = escape(f"{folder}/{s}")
            label = names[i - 1] if i - 1 < len(names) and names[i - 1] else f"phase {i}"
            cap = escape(f"{entry['name']} · {label}", quote=True)
            img = f'<img src="{src}" alt="{escape(label)}" loading="lazy">'
            out.append(f'<a href="{src}" data-shot data-caption="{cap}">{img}{escape(label)}</a>')
        out.append("</div>")
    if res.get("video"):
        out.append(f'<p class="sub"><a href="{escape(folder)}/run.mp4">video</a></p>')
    out.append("</section>")
    return "\n".join(out)


def _phases(phases: list[dict], failure: dict | None, open_all: bool) -> str:
    """One collapsible block per phase: what it does, step by step, and what it
    expects, each marked done, failed or not reached."""
    out = ['<ol class="phases">']
    for n, p in enumerate(phases, 1):
        st = p.get("status", "NOT_RUN")
        cls = {"PASS": "ok", "FAIL": "bad"}.get(st, "skip")
        mark = {"PASS": "✓", "FAIL": "✗"}.get(st, "·")
        here = failure is not None and failure.get("phase") == n
        bad_step = failure.get("step") if here else None
        # Learning elements or finding the window happens before any step.
        before_steps = here and failure.get("action") in ("learn", "window")
        time_ = (
            "not reached" if st == "NOT_RUN" else f"{p['seconds']:.2f} s" if "seconds" in p else ""
        )
        steps, checks = p.get("steps"), p.get("checks")
        is_open = " open" if open_all or st == "FAIL" else ""
        head = (
            f'<span class="m {cls}">{mark}</span><span class="pn">{n}</span>'
            f'<span class="pname">{escape(p.get("name", ""))}</span>'
            f'<span class="t">{time_}</span>'
        )
        if not steps and not checks and not before_steps:
            out.append(f'<li class="phase {cls}"><div class="nodetail">{head}</div></li>')
            continue
        summary = f"<summary>{head}</summary>"
        body = ['<ol class="steps">']
        if before_steps:
            reason = failure.get("reason", "")
            missing = reason.removeprefix("element not found: ")
            line = f"Couldn’t find {missing}" if missing != reason else reason
            body.append(f'<li class="step bad">{escape(line)}</li>')
        for i, s in enumerate(steps or [], 1):
            kinds = ["step"]
            if s.get("expect"):
                kinds.append("expect")
            if s.get("pause"):
                kinds.append("pause")
            if before_steps:
                pass
            elif st == "PASS" or (st == "FAIL" and bad_step and i < bad_step):
                kinds.append("ok")
            elif st == "FAIL" and bad_step == i:
                kinds.append("bad")
            elif st == "FAIL" and not bad_step:
                kinds.append("ok")  # the steps ran; learn or a check failed
            body.append(f'<li class="{" ".join(kinds)}">{escape(s["text"])}</li>')
        for c in checks or []:
            failed_check = here and not bad_step and not before_steps
            k = " ok" if st == "PASS" else " bad" if failed_check else ""
            body.append(f'<li class="step expect{k}">{escape(c)}</li>')
        body.append("</ol>")
        details = f"<details{is_open}>{summary}{''.join(body)}</details>"
        out.append(f'<li class="phase {cls}">{details}</li>')
    out.append("</ol>")
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
            run.get("started_at", ""),
            f"{run.get('seconds', 0)} s",
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
<div class="toolbar"><button type="button" id="expand">Expand all phases</button></div>
{body}
</main>{_LIGHTBOX}</body></html>
"""
    path = run_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    return path
