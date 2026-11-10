# BAM: scripted end-to-end flows

BAM scripts drive a real desktop app through its accessibility tree: learn a
layout by element names, run steps, check what happened. `bad-ass-mcp run`
executes them with no model in the loop and writes a results folder with
proof screenshots and an HTML report.

```
bad-ass-mcp run smoke/ --config bam.json
```

## Security model (read first)

A BAM script drives your desktop. Treat a script as code: it can run shell
commands (`setup`, `shell` checks), launch programs (through the config), and
send clicks and keys to any window it can name. **Only run scripts you wrote
or reviewed.**

Planned before scripts can be shared (BAM strings, `.bam` packages):

- **Untrusted mode**, on by default for anything decoded from a string or
  package: no shell, no launch commands from the script, no `click_at`
  outside the app's window, every window locked to the app's own process,
  strict schema with caps on steps, text length and runtime.
- **The local config is the sandbox.** Which app a script may touch and how
  it starts always comes from the author's `bam.json`, never from the script
  or a package.
- **Preview before run:** decoding prints the steps and runs nothing.

## Running

From source: `uv sync --extra macos` or `uv sync --extra windows` first. Linux
has no extra: the backend uses the system's `gi` (PyGObject) and AT-SPI, so
create the venv with `--system-site-packages`.

```
bad-ass-mcp run PATH... [--config bam.json] [--results DIR] [--platform P]
                [--resume] [--fail-fast [first_script]] [--retries N]
                [--video [on_failure]] [--no-screenshots] [--commit SHA]
```

- `PATH`: script files, or folders of `*.json` scripts (run in name order).
- Exit status 1 if any script failed.
- On a terminal: a progress bar counted in phases (TMS9928A colours, the Coleco
  Adam's palette), then one line per script with its duration and failure
  reason, and a totals line (`3 passed, 1 failed in 2m14s · platform · commit ·
  start time`). No colour when piped or with `NO_COLOR`.
- `--resume`: continue the latest run on the same commit and platform; scripts
  that passed are kept, the rest run again.

### Results

```
bam-results/<time>_<commit>_<platform>/
  run.json              order, status + seconds of each script, totals, started_at,
                        finished_at, seconds, commit, platform, config
  01_<script>/
    result.json         status, started_at/finished_at, seconds, window, covers,
                        phases [{name, status, seconds, steps [{text, expect?}], checks}],
                        checks (final), failure {phase, step, action, reason}
                        (action "learn"/"window": failed before the phase's steps)
    phase-N.png         proof screenshot after each phase (screenshot_proof)
    fail.png, tree.json screen and pruned accessibility tree at a failure
    timeline.json       timestamped phases (and, in demo mode, every step with its
                        target's bounds): the input for captions and highlights
    run.mp4             with --video
    files/              the script's $SMOKE working folder
  report.html
```

Statuses: `PASS`, `FAIL`, `SKIP` (not for this platform), `MANUAL` (has
checks a human must do), `NOT_RUN` (fail-fast stopped first; for a phase, an
earlier phase failed).

### Configuration

Three layers, later wins: the suite file (`--config`), a script's own
`configuration` block, then command-line flags.

| key | default | meaning |
|---|---|---|
| `window` | (required) | title substring of the app's main window |
| `app` | none | launch command, string or list, or per-platform object; the run then drives only that process's windows (or ones that appeared after launch) |
| `pid` | none | attach mode: drive only this process's windows. Without `app` or `pid`, two windows matching `window` is a failure, never a guess (it could be the user's own copy) |
| `ready` | none | an element (`{"name": ..., "role": ...}`, per platform if needed) that exists only once the app is usable; waited for after the window appears, up to `launch_timeout`. Set it: windows often exist before their content (WebView2, WebKitGTK), and keys sent then are lost |
| `launch_timeout` | 20 | seconds to wait for the window, then for `ready` |
| `step_timeout` | 5 | seconds to wait for windows in phases, and to retry elements a phase learns that haven't rendered yet |
| `timeout_scale` | 1 | multiplies every `wait_for_*` timeout and `sleep` in the scripts; 2–3 for slow machines and small VMs, without editing scripts |
| `screenshot_proof` | true | screenshot after every phase |
| `demo` | false | demo mode (first pieces): steps run one at a time with a pause, clicks go through the real pointer at the element's centre so they show on video, and each step is logged with its target's bounds in `timeline.json` |
| `demo_pace` | 0.8 | seconds to pause after each step in demo mode |
| `record_video` | false | `true`, or `"on_failure"` to keep only failing runs |
| `fail_fast` | false | `true`, or `"first_script"` (stop only if the first script fails) |
| `retries` | 0 | rerun a failing script; a later pass is marked "passed on attempt N" |

## Script format

```json
{
  "name": "font-picker-escape",
  "platforms": ["macos", "windows", "linux"],
  "configuration": { "screenshot_proof": true },
  "setup": [{ "shell": "printf 'hello' > \"$SMOKE/a.md\"" }],
  "launch": ["a.md"],
  "phases": [
    {
      "learn": { "settings": { "name": "Settings" } },
      "steps": [
        { "action": "click", "target": "settings" },
        { "action": "wait_for_element", "window_id": "$WINDOW", "name": "Done", "timeout": 5 }
      ]
    }
  ],
  "checks": [{ "present": { "name": "Done" } }]
}
```

- **Phases** run in order; an optional `"name"` labels one in results and the
  report (default: its `id`, else its first action, e.g. `Click “Settings”`).
  Results and the report list every phase in plain language: each step
  (`Type "crab" into “Search emoji”`), each expectation (`“Search emoji”
  appears`, `“Name” reads "crab"`, phase and final checks), marked done,
  failed or not reached. A script's `covers` text heads its card. Each learns its elements (`learn_layout`) right
  before its steps, since handles go stale when the UI changes. A missing
  element fails the phase.
- **Steps** are `run_sequence` actions; `"target": "<label>"` becomes the
  learned handle, `$WINDOW` the current window id, `$SMOKE` the working folder.
- **Values:** a `get_value` step may `"save": "x"`; a later `"expect": "$x"` (or a
  literal) must match, or the script fails at that step. `"expect_not": "$x"`
  is the opposite: the value must have changed.
- **Drags:** `{"action": "drag", "from_target": "a", "to_target": "b"}` drags
  between two learned elements (their centers); `"dx"` / `"dy"` instead of
  `to_target` drags from the element by that many pixels (a splitter).
  `{"action": "get_bounds", "target": "a"}` reads `[x, y, w, h]`, so
  `save` + `expect_not` around a drag proves something moved.
- **Per-platform values:** any `name`, `role`, step field or window pattern may
  be `{"macos": ..., "windows": ..., "linux": ...}`. `only: [...]` limits a
  step, learn entry, phase or check to platforms.
- **Phase extras:** `"window": "<title>"` runs the phase in another window
  (dialogs); `"window": {"other": "<title>"}` picks a matching window that
  isn't the main one (a second editor window; on Windows each window is
  listed separately); `"id"` + `{"repeat": "<id>"}` reruns a phase with fresh handles;
  a phase may carry its own `checks`.
- **Checks:** `present`, `absent` (by name/role), `windows: n` (windows matching
  the config's `window`), `no_window: "<title>"`, `shell` (exit 0),
  `manual` (reported for a human; the script ends `MANUAL`).
- **Null windows:** a per-platform `window` (or a `wait_for_window` step's
  `pattern`) that is `null` for this platform runs the phase in the main window
  and skips the wait. GTK dialogs, for example, live inside the app's window.
- **State:** a learn entry or a `present`/`absent` check may add `"state"`
  (e.g. `"focused"`, `"selected"`, per platform if needed): the first match
  carrying that state wins. For widgets with no accessible name.
- **Count:** `"count": n` on `present` asserts exactly n matches.
- **Variables:** `"variables": {"target": {"windows": "$SMOKE\\A.MD", "linux":
  "$SMOKE/a.md"}}` sets per-platform literals (`$SMOKE` expands inside them);
  `$target` then works in any step field and shell command.
- **Shell steps:** `{"action": "shell", "cmd": "..."}` runs a command between
  steps (for `only`-gated platform glue like xdotool); a non-zero exit fails
  the script at that step.
- **Runner-only keys** (`note`, `confirm`, `verified`, `covers`, `notes`,
  `variables_note`) are ignored
  by the runner and kept for people.

## Platform notes

Element names come from `aria-label` / visible text and are usually the same
on every OS; roles are platform-native, so give them only to disambiguate.
Known backend differences (keys into WebView2, native `<select>` popups, the
macOS menu bar) are listed in MarkTheCrab's `smoke/README.md` until they move
here.
