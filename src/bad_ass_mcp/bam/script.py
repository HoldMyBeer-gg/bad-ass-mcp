"""Resolving a BAM script for one platform: per-platform values, `only`,
variables, and turning script steps into run_sequence steps."""

from __future__ import annotations

import re
from typing import Any

PLATFORMS = ("macos", "windows", "linux")

# Keys the runner reads and run_sequence must never see.
_RUNNER_KEYS = {
    "target",
    "from_target",
    "to_target",
    "only",
    "expect",
    "expect_not",
    "save",
    "confirm",
    "note",
}
_VAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def pick(value: Any, platform: str) -> Any:
    """A per-platform object ({"windows": .., "linux": ..}) resolves to this
    platform's entry, or None if it has none; anything else passes through."""
    if isinstance(value, dict) and value and set(value) <= set(PLATFORMS):
        return value.get(platform)
    return value


def applies(entry: dict, platform: str) -> bool:
    only = entry.get("only")
    return only is None or platform in only


def expand(value: Any, variables: dict[str, str]) -> Any:
    """Replace $NAME with variables[NAME] in strings; unknown names stay."""
    if not isinstance(value, str):
        return value
    return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), value)


def learn_descriptors(learn: dict, platform: str) -> dict[str, dict]:
    """learn_layout descriptors for this platform: role/name resolved, entries
    for other platforms skipped, runner-only keys dropped."""
    out: dict[str, dict] = {}
    for label, desc in learn.items():
        if not applies(desc, platform):
            continue
        d = {}
        for key in ("name", "role", "state"):
            v = pick(desc.get(key), platform)
            if v is not None:
                d[key] = v
        out[label] = d
    return out


def build_steps(
    steps: list[dict],
    platform: str,
    handles: dict[str, str],
    window_id: str,
    variables: dict[str, str],
    *,
    timeout_scale: float = 1.0,
    secrets: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """run_sequence steps for this platform, plus per-step runner metadata
    ({"expect", "save"}) aligned by index. `timeout_scale` stretches every
    wait timeout and sleep, for slow machines."""
    sent: list[dict] = []
    meta: list[dict] = []
    env = {**variables, "WINDOW": window_id}
    for step in steps:
        if not applies(step, platform):
            continue
        # A per-platform wait_for_window pattern that's null for this
        # platform means "no separate window here": skip the wait.
        if step.get("action") == "wait_for_window" and pick(step.get("pattern"), platform) is None:
            continue
        s = {}
        for k, v in step.items():
            if k in _RUNNER_KEYS:
                continue
            if k == "secret":
                # Typed as the text; the reference stays for the password
                # gate. Never $-expanded.
                s["text"] = (secrets or {})[v]
                s["secret"] = v
                continue
            v = pick(v, platform)
            s[k] = expand(v, env)
        for key, field in (
            ("target", "handle"),
            ("from_target", "from_handle"),
            ("to_target", "to_handle"),
        ):
            if key in step:
                s[field] = handles[step[key]]
        if timeout_scale != 1:
            if s.get("action") in ("wait_for_element", "wait_for_window") and "timeout" in s:
                s["timeout"] = s["timeout"] * timeout_scale
            if s.get("action") == "sleep" and "seconds" in s:
                s["seconds"] = s["seconds"] * timeout_scale
        sent.append(s)
        meta.append({k: step[k] for k in ("expect", "expect_not", "save") if k in step})
    return sent, meta
