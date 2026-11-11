"""Validate BAM scripts and suite configs against the JSON Schema in
`schema/`, then check what a schema can't: targets learned in their phase,
`repeat` naming an earlier phase. Every problem comes back with its path
and, for a near-miss key or value, a "did you mean"."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

_DIR = Path(__file__).parent / "schema"
_TYPE_WORDS = {
    "number": "a number",
    "integer": "a whole number",
    "string": "a string",
    "boolean": "true or false",
    "array": "a list",
    "object": "an object",
    "null": "null",
}


@dataclass(frozen=True)
class Issue:
    path: str
    message: str


@cache
def _schema(name: str) -> dict:
    return json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))


@cache
def _validator(name: str) -> Draft202012Validator:
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in (_schema("script"), _schema("config"))
    )
    return Draft202012Validator(_schema(name), registry=registry)


def step_keys() -> dict[str, set[str]]:
    """Each action's allowed step keys, from the schema."""
    out = {}
    for rule in _schema("script")["$defs"]["step"]["allOf"]:
        action = rule["if"]["properties"]["action"]["const"]
        out[action] = set(rule["then"]["properties"])
    return out


def _suggest(word: str, choices) -> str:
    near = difflib.get_close_matches(str(word), [str(c) for c in choices], n=1)
    return f" (did you mean '{near[0]}'?)" if near else ""


def _path(parts, root: str) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or root


def _show(v) -> str:
    return json.dumps(v) if not isinstance(v, str) else f"'{v}'"


def _leaves(err: ValidationError) -> list[ValidationError]:
    """For anyOf: follow the branch the value was aiming at (its type
    matches), so a per-platform object with a typo reports the typo, not
    'should be a string'."""
    if err.validator not in ("anyOf", "oneOf") or not err.context:
        return [err]
    branches: dict[int, list[ValidationError]] = {}
    for sub in err.context:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    if all(any(s.validator == "required" for s in subs) for subs in branches.values()):
        return [err]  # "needs one of ..." reads better than each branch
    aimed = {
        i: subs
        for i, subs in branches.items()
        if not any(s.validator == "type" and len(s.relative_schema_path) == 2 for s in subs)
    }
    if not aimed:
        return [err]
    best = min(aimed.values(), key=len)
    return [leaf for sub in best for leaf in _leaves(sub)]


def _messages(err: ValidationError, root: str) -> list[Issue]:
    where = _path(err.absolute_path, root)
    v, inst = err.validator, err.instance
    if v == "additionalProperties":
        allowed = set(err.schema.get("properties", {}))
        extras = [k for k in inst if k not in allowed]
        return [Issue(where, f"unknown key '{k}'{_suggest(k, allowed)}") for k in extras]
    if v == "required":
        missing = err.validator_value
        have = set(inst) if isinstance(inst, dict) else set()
        return [Issue(where, f"missing '{k}'") for k in missing if k not in have]
    if v == "enum":
        choices = err.validator_value
        if isinstance(inst, str) and all(isinstance(c, str) for c in choices):
            noun = "action" if err.absolute_path and err.absolute_path[-1] == "action" else "value"
            return [Issue(where, f"unknown {noun} '{inst}'{_suggest(inst, choices)}")]
        return [Issue(where, "should be one of " + ", ".join(_show(c) for c in choices))]
    if v == "type":
        t = err.validator_value
        words = [_TYPE_WORDS.get(x, x) for x in ([t] if isinstance(t, str) else t)]
        return [Issue(where, "should be " + " or ".join(words))]
    if v in ("anyOf", "oneOf"):
        subs = err.context or []
        if subs and all(s.validator == "required" for s in subs):
            keys = [k for s in subs for k in s.validator_value]
            return [Issue(where, "needs one of " + ", ".join(f"'{k}'" for k in keys))]
        forms = [f for f in (_form(b) for b in err.validator_value or []) if f]
        if forms:
            return [Issue(where, "should be " + " or ".join(dict.fromkeys(forms)))]
        return [Issue(where, "doesn't match any allowed form")]
    return [Issue(where, err.message)]


def _sort_key(issue: Issue):
    # Whole-file problems first, then by path.
    return (0 if issue.path.startswith("(") else 1, issue.path)


def _schema_issues(data, name: str, root: str) -> list[Issue]:
    issues: list[Issue] = []
    for err in _validator(name).iter_errors(data):
        for leaf in _leaves(err):
            issues.extend(_messages(leaf, root))
    # A typo'd key also makes the real one "missing"; the "did you mean"
    # already says so.
    meant = {
        (i.path, i.message.split("did you mean '")[1].split("'")[0])
        for i in issues
        if "did you mean '" in i.message and i.message.startswith("unknown key")
    }

    def echo(i: Issue) -> bool:
        if i.message.startswith("missing '"):
            return (i.path, i.message.split("'")[1]) in meant
        if i.message.startswith("needs one of"):
            return any(p == i.path and f"'{k}'" in i.message for p, k in meant)
        return False

    seen, out = set(), []
    for i in issues:
        if i not in seen and not echo(i):
            seen.add(i)
            out.append(i)
    return sorted(out, key=_sort_key)


def _meaning_issues(script: dict) -> list[Issue]:
    issues: list[Issue] = []
    ids: list[str] = []
    phases = script.get("phases")
    if not isinstance(phases, list):
        return issues
    for n, phase in enumerate(phases):
        if not isinstance(phase, dict):
            continue
        if "repeat" in phase:
            rid = phase["repeat"]
            if isinstance(rid, str) and rid not in ids:
                issues.append(
                    Issue(
                        f"phases[{n}].repeat",
                        f"no earlier phase with id '{rid}'{_suggest(rid, ids)}",
                    )
                )
            continue
        if isinstance(phase.get("id"), str):
            ids.append(phase["id"])
        learn = phase.get("learn", {})
        labels = list(learn) if isinstance(learn, dict) else []
        for s, step in enumerate(phase.get("steps") or []):
            if not isinstance(step, dict):
                continue
            for key in ("target", "from_target", "to_target"):
                label = step.get(key)
                if isinstance(label, str) and label not in labels:
                    issues.append(
                        Issue(
                            f"phases[{n}].steps[{s}].{key}",
                            f"'{label}' isn't learned in this phase{_suggest(label, labels)}",
                        )
                    )
    return issues


def validate_script(script) -> list[Issue]:
    if not isinstance(script, dict):
        return [Issue("(script)", "should be an object")]
    issues = _schema_issues(script, "script", "(script)")
    return issues + _meaning_issues(script)


def validate_config(config) -> list[Issue]:
    return _schema_issues(config, "config", "(config)")


def _form(branch) -> str | None:
    """A plain-words name for one anyOf branch, for "should be X or Y"."""
    if not isinstance(branch, dict):
        return None
    props = set(branch.get("properties", {}))
    if branch.get("type") == "object" and props and props <= {"macos", "windows", "linux"}:
        return "a per-platform object (keys macos / windows / linux)"
    if "anyOf" in branch:
        inner = [f for f in (_form(b) for b in branch["anyOf"]) if f]
        return " or ".join(dict.fromkeys(inner)) or None
    t = branch.get("type")
    if isinstance(t, str):
        return {
            "string": "a string",
            "array": "a list",
            "object": "an object",
            "integer": "a whole number",
            "number": "a number",
            "boolean": "true or false",
            "null": "null",
        }.get(t)
    return None
