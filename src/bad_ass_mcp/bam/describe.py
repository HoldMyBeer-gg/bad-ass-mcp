"""Plain-language descriptions of BAM phases for results and the report:
what each phase does, step by step, and what it expects to be true.

Steps are listed one-to-one with the run_sequence steps the runner sends
(same platform filtering), so a failure's step number points at the right
line."""

from __future__ import annotations

from typing import Any

from .script import applies, learn_descriptors, pick


def _q(name: str) -> str:
    return f"“{name}”"


def element(desc: dict | None) -> str:
    """“Name”, or "the <role>" for an element known only by role."""
    if not desc:
        return "the element"
    if desc.get("name"):
        out = _q(desc["name"])
    elif desc.get("role"):
        out = f"the {desc['role']}"
    else:
        out = "the element"
    if desc.get("state"):
        out += f" ({desc['state']})"
    return out


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def describe_step(step: dict, targets: dict[str, dict], platform: str) -> dict:
    a = step.get("action")
    t = lambda key="target": element(targets.get(step.get(key)))  # noqa: E731
    v = lambda key: pick(step.get(key), platform)  # noqa: E731
    if a == "click":
        verb = "Right-click" if step.get("button") == "right" else "Click"
        if int(step.get("clicks", 1)) == 2:
            verb = "Double-click"
        return {"text": f"{verb} {t()}"}
    if a == "click_at":
        return {"text": f"Click at ({_num(step.get('x'))}, {_num(step.get('y'))})"}
    if a == "type":
        into = f" into {t()}" if "target" in step else ""
        return {"text": f'Type "{v("text")}"{into}'}
    if a == "key":
        return {"text": f"Press {v('key')}"}
    if a == "select":
        return {"text": f'Choose "{v("value")}" in {t()}'}
    if a == "sleep":
        return {"text": f"Pause {_num(step.get('seconds', 0))} s", "pause": True}
    if a == "wait_for_element":
        desc = {k: pick(step[k], platform) for k in ("name", "role", "state") if k in step}
        return {"text": f"{element(desc)} appears", "expect": True}
    if a == "wait_for_window":
        return {"text": f"A {_q(v('pattern'))} window appears", "expect": True}
    if a in ("get_value", "get_bounds"):
        if "expect" in step:
            want = str(step["expect"])
            if want.startswith("$") and want[1:].isidentifier():
                return {"text": f"{t()} still reads the saved {want[1:]}", "expect": True}
            return {"text": f'{t()} reads "{want}"', "expect": True}
        if "expect_not" in step:
            word = "moved" if a == "get_bounds" else "changed"
            return {"text": _cap(f"{t()} {word}"), "expect": True}
        if a == "get_bounds":
            return {"text": f"Note where {t()} is"}
        saved = f" (saved as {step['save']})" if "save" in step else ""
        return {"text": f"Read {t()}{saved}"}
    if a == "drag":
        src = t("from_target") if "from_target" in step else "the pointer"
        if "to_target" in step:
            return {"text": f"Drag {src} onto {t('to_target')}"}
        return {"text": f"Drag {src} by ({_num(step.get('dx', 0))}, {_num(step.get('dy', 0))})"}
    if a == "shell":
        return {"text": f"Run `{step.get('cmd', '')}`"}
    label = f" {t()}" if "target" in step else ""
    return {"text": f"{_cap(str(a))}{label}"}


def describe_check(check: dict, platform: str) -> str:
    for key in ("present", "absent"):
        if key in check:
            desc = learn_descriptors({"x": check[key]}, platform)["x"]
            if key == "absent":
                return f"{element(desc)} is gone"
            if "count" in check[key]:
                return f"Exactly {check[key]['count']} × {element(desc)}"
            return f"{element(desc)} is present"
    if "windows" in check:
        n = int(check["windows"])
        return f"{n} window{'s' if n != 1 else ''} open"
    if "no_window" in check:
        return f"No {_q(pick(check['no_window'], platform))} window"
    if "shell" in check:
        return f"`{check['shell']}` succeeds"
    if "manual" in check:
        return f"A human checks: {check['manual']}"
    return str(check)


def describe_checks(checks: list[dict], platform: str) -> list[str]:
    return [describe_check(c, platform) for c in checks if applies(c, platform)]


def describe_phase(phase: dict, platform: str) -> dict:
    """{"steps": [{"text", "expect"?, "pause"?}], "checks": [str]} for one
    platform, steps aligned with what build_steps sends."""
    targets = learn_descriptors(phase.get("learn", {}), platform)
    steps = []
    for step in phase.get("steps", []):
        if not applies(step, platform):
            continue
        if step.get("action") == "wait_for_window" and pick(step.get("pattern"), platform) is None:
            continue
        steps.append(describe_step(step, targets, platform))
    return {"steps": steps, "checks": describe_checks(phase.get("checks", []), platform)}


def phase_title(phase: dict, described: dict, n: int) -> str:
    """The phase's name or id; else its first action; else "phase N"."""
    if phase.get("name") or phase.get("id"):
        return phase.get("name") or phase["id"]
    for s in described["steps"]:
        if not s.get("pause") and not s.get("expect"):
            return s["text"]
    if described["steps"]:
        return described["steps"][0]["text"]
    return f"phase {n}"
