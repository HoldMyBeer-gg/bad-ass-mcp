"""Plain-language descriptions of BAM phases for results and the report:
what each phase does, step by step, and what it expects to be true.

Steps are listed one-to-one with the run_sequence steps the runner sends
(same platform filtering), so a failure's step number points at the right
line."""

from __future__ import annotations

import re
from typing import Any

from .script import applies, expand, learn_descriptors, pick


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


def _saved(value) -> str | None:
    """ "$name" -> "name" for a reference to a saved value."""
    s = str(value)
    return s[1:] if s.startswith("$") and s[1:].isidentifier() else None


def describe_step(
    step: dict, targets: dict[str, dict], platform: str, variables: dict | None = None
) -> dict:
    a = step.get("action")
    variables = variables or {}
    t = lambda key="target": element(targets.get(step.get(key)))  # noqa: E731
    v = lambda key: expand(pick(step.get(key), platform), variables)  # noqa: E731
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
        if "window_id" in step:
            return {"text": f"Press {v('key')}"}
        return {"text": f"Press {v('key')} (real key, to the front window)"}
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
            want = step["expect"]
            name = _saved(want)
            if name and name not in variables:
                return {"text": f"{t()} still reads the saved {name}", "expect": True}
            return {"text": f'{t()} reads "{expand(want, variables)}"', "expect": True}
        if "expect_not" in step:
            name = _saved(step["expect_not"])
            if name and name not in variables:
                if a == "get_bounds":
                    text = f"{t()} is not where it was (saved {name})"
                else:
                    text = f"{t()} no longer reads the saved {name}"
                return {"text": _cap(text), "expect": True}
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
        return {"text": f"Run `{expand(step.get('cmd', ''), variables)}`"}
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


def describe_phase(phase: dict, platform: str, variables: dict | None = None) -> dict:
    """{"steps": [{"text", "expect"?, "pause"?}], "checks": [str]} for one
    platform, steps aligned with what build_steps sends. `variables` are the
    script's own (per-platform, resolved), shown by value; saved values
    aren't known yet and are shown by name."""
    targets = learn_descriptors(phase.get("learn", {}), platform)
    steps = []
    for step in phase.get("steps", []):
        if not applies(step, platform):
            continue
        if step.get("action") == "wait_for_window" and pick(step.get("pattern"), platform) is None:
            continue
        steps.append(describe_step(step, targets, platform, variables))
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


def is_real_input(step: dict) -> bool:
    """Steps that go to whatever is in front or under the pointer rather than
    to an element: keys without a window, key combinations, click_at, drag,
    and clicks marked `pointer`."""
    a = step.get("action")
    if a in ("click_at", "drag"):
        return True
    if a == "click" and step.get("pointer"):
        return True
    if a != "key":
        return False
    if "window_id" not in step:
        return True
    # A combination is sent as real keystrokes (the target is raised first),
    # with or without a window.
    from ..backend.base import parse_combo

    try:
        return parse_combo(str(step.get("key", ""))) is not None
    except ValueError:
        return False


_SECRET_WORDS = re.compile(r"pass(word|code|phrase)|\bpass\b|token|secret|\bpin\b|api.?key", re.I)


def secret_hints(script: dict, platform: str) -> list[str]:
    """Learn labels that type steps write into and whose label or name looks
    like a secret (password, passcode, token, secret, PIN, API key). Only an
    early warning: the real check is at run time, on the field itself."""
    hits: list[str] = []
    for phase in script.get("phases", []):
        if not applies(phase, platform):
            continue
        learn = phase.get("learn", {})
        for step in phase.get("steps", []):
            if step.get("action") != "type" or not applies(step, platform):
                continue
            label = step.get("target")
            desc = learn.get(label, {}) if isinstance(learn, dict) else {}
            name = desc.get("name") if isinstance(desc, dict) else None
            if isinstance(name, dict):
                name = name.get(platform)
            words = f"{label} {name or ''}"
            if label and _SECRET_WORDS.search(words) and label not in hits:
                hits.append(label)
    return hits


def needs_foreground(script: dict, platform: str) -> bool:
    """Whether a run of this script on `platform` sends real input, so the
    user has to keep hands off the keyboard and mouse."""
    for phase in script.get("phases", []):
        if not applies(phase, platform):
            continue
        for step in phase.get("steps", []):
            if applies(step, platform) and is_real_input(step):
                return True
    for entry in script.get("teardown", []):
        if "shell" not in entry and applies(entry, platform) and is_real_input(entry):
            return True
    return False
