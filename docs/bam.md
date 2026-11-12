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

**A window found by title can be the user's own.** Single-instance apps
(Windows 11 Notepad with its tabs, browsers, editors that reuse a window)
answer a title match with the window the user already has open, and typing
into it replaces their text. In a run, set `app` (or `pid`) so only the
launched process is driven. With the MCP tools directly, check the `pid` from
`list_windows` belongs to a process you started before typing.

Planned before scripts can be shared (BAM strings, `.bam` packages):

- **Untrusted mode**, on by default for anything decoded from a string or
  package: no shell, no launch commands from the script, no `click_at`
  outside the app's window, every window locked to the app's own process,
  strict schema with caps on steps, text length and runtime.
- **The local config is the sandbox.** Which app a script may touch and how
  it starts always comes from the author's `bam.json`, never from the script
  or a package.
- **Preview before run:** decoding prints the steps and runs nothing.

## Validating

Scripts and configs follow a JSON Schema (`src/bad_ass_mcp/bam/schema/`,
published at `https://bam.holdmybeer.gg/schema/v1/script.json` and
`.../config.json`). `run` checks every script and the config before it
launches anything and exits 2 on a problem; `check` does the same without
running:

```
bad-ass-mcp check smoke/ [--config bam.json] [--json]
```

```
emoji.json: phases[0].steps[1]: unknown key 'taget' (did you mean 'target'?)
emoji.json: phases[0].steps[0].action: unknown action 'clik' (did you mean 'click'?)
emoji.json: phases[0].steps[2].target: 'serch' isn't learned in this phase (did you mean 'search'?)
```

Beyond shape it checks that every `target` is learned in its phase and that
`repeat` names an earlier phase's `id`. Add
`"$schema": "https://bam.holdmybeer.gg/schema/v1/script.json"` to a script
(or `config.json` to a `bam.json`) for autocomplete and inline errors in
VS Code and JetBrains.

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
| `window` | (required) | substring of the app's main window, case-insensitive, or a per-platform object. Matched against the listed name (process name on macOS, title on Windows, AT-SPI app name on Linux) and every top-level window title, so the title works on all three |
| `app` | none | launch command, string or list, or per-platform object; the run then drives only that process's windows (or ones that appeared after launch) |
| `helpers` | none | list of other processes whose windows the run may drive, matched like `window` (per-platform allowed). For dialogs owned by a process that was running before the launch, e.g. `["xdg-desktop-portal"]` for file pickers on Linux |
| `pid` | none | attach mode: drive only this process's windows. Without `app` or `pid`, two windows matching `window` is a failure, never a guess (it could be the user's own copy) |
| `ready` | none | an element (`{"name": ..., "role": ...}`, per platform if needed) that exists only once the app is usable; waited for after the window appears, up to `launch_timeout`. Set it: windows often exist before their content (WebView2, WebKitGTK), and keys sent then are lost |
| `launch_timeout` | 20 | seconds to wait for the window, then for `ready` |
| `step_timeout` | 5 | seconds to wait for windows in phases, and to retry elements a phase learns that haven't rendered yet |
| `hang_timeout` | 30 | seconds a window may stay unresponsive (Windows: IsHungAppWindow) before the launched app is killed and the script fails "app not responding"; 0 turns it off. A call into a hung window otherwise blocks forever |
| `yield_to_user` | true | in a script that sends real input, any keyboard or mouse input that isn't the runner's stops the script and the rest of the suite with `Run stopped: user input detected.`; focus stays where the person put it. Background-only scripts never stop for this |
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
- **Teardown:** a top-level `"teardown"` list runs after the last phase and
  after a failure, before the app is closed and focus goes back. Entries are
  shell commands (`{"shell": "..."}`) or steps without learned targets
  (`{"action": "key", "key": "Escape"}`, `click_at`, a key combination). It
  is best-effort: each entry's outcome is recorded in the result's
  `teardown` list, a failing entry doesn't stop the rest, and the run's
  status never changes. Use it in attach mode to close dialogs a failed run
  left open. For dialogs, send keys without `window_id`: they go to the
  front window as real keys (the runner raises the app first). On macOS a
  key with `window_id` is posted to the app's process, which an Open/Save
  panel never sees, and still reports ok.
- **Phase extras:** `"window": "<title>"` runs the phase in another window
  (dialogs); `"window": {"other": "<title>"}` picks a matching window that
  isn't the main one (a second editor window; on Windows each window is
  listed separately); `"id"` + `{"repeat": "<id>"}` reruns a phase with fresh handles;
  a phase may carry its own `checks`.
- **Pointer clicks:** `{"action": "click", "target": "x", "pointer": true}`
  clicks with the real pointer at the element's centre instead of its
  accessibility action. Use it for controls that report the action ok but only
  react to the mouse, such as egui menus that open on hover. It counts as real
  input, so it gets the foreground and point checks.
- **Typing and the clipboard:** a `type` step never touches the clipboard
  unless it has `"paste": true`. With it, the text is lent to the clipboard
  (kept out of clipboard history and clipboard managers) and cleared after if
  nothing replaced it. What the user had copied is lost, never restored: a
  restore could bring back a secret a password manager had already cleared.
  Password fields never paste.
- **Password fields:** a `type` step into a password field (Windows
  IsPassword, macOS AXSecureTextField, AT-SPI password text, or a masked
  value) fails with "password field: not allowed by this machine's config"
  unless the runner's config sets `allow_password_fields`: `true` for any app,
  or a list of window names. Only `bam.json` or the command line can set it;
  a script's `configuration` block can't (`check` reports it, and the runner
  ignores it). `check` and `run` also name type targets that look like
  secrets (password, passcode, token, secret, PIN, API key) before a run.
- **Secrets:** a `type` step can take `"secret": "env:NAME"` instead of
  `text`: the value is read from the environment variable `NAME` when the
  phase starts (unset or empty fails the run before anything is typed). It
  is replaced with ••• everywhere a run writes: result.json, timeline.json,
  tree.json, run.json and the report. A password field only takes a secret,
  never literal `text`, and still needs `allow_password_fields`. A secret into
  any other field is refused, because the value would be visible on screen and
  in screenshots, unless the runner's config sets
  `allow_secrets_in_plain_fields`; then, from that step on, the run keeps no
  screenshots (phase proofs, fail.png) and no video. Keychain and Credential
  Manager sources are planned.
- **Codes from setup:** a setup entry `{"shell": "...", "save": "code"}`
  saves the command's trimmed output (a pairing or MFA code) as `$code` for
  later steps. It needs `allow_temp_saving_mfa_codes: true` in this machine's
  config (runner-only; without it the command isn't run). The value is kept
  in memory for the run and shown as ••• in every output; an empty output
  fails setup. Screenshots stay on, since such a code is single-use.
- **Checks:** `present`, `absent` (by name/role), `windows: n` (windows matching
  the config's `window`), `no_window: "<title>"`, `shell` (exit 0),
  `manual` (reported for a human; the script ends `MANUAL`).
  A `present` check retries until `step_timeout`, because web views can show
  an almost empty tree for a moment during a view change. `absent` is read
  once, so an empty moment can make it pass early: follow a transition with a
  `present` check on the new view before an `absent` check on the old one.
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

## Real input and the foreground

Most steps act on elements through the accessibility tree and work with the
app buried under other windows. Three kinds don't: `key` without `window_id`
(a real key to the front window), `click_at` and `drag` (real pointer
events at screen points). Key combinations (`"key": "Cmd+S"`) are always
real input, window or not.

**Key combinations.** A step like
`{"action": "key", "key": "Cmd+Shift+G"}`; also `"Ctrl+A"`, `"Alt+F4"`.
Modifiers: Ctrl, Shift, Alt (or Option), Cmd, Super (or Win). `Cmd` is the
platform's shortcut key, Command on macOS and Ctrl on Windows and Linux, so
`"Cmd+S"` saves everywhere; `Super` is the literal Windows/Super key.
`"Ctrl++"` sends Ctrl and the plus key. On Linux combinations need X11 or an
XWayland app. An unknown modifier fails `bad-ass-mcp check`. Before each of those the runner checks which
process would receive it: the foreground window's top-level owner for keys,
the window under the point for clicks and drags. If it isn't the app, the
runner raises the app (on Windows through the foreground lock: plain, after
an Alt tap, then with attached input) and checks again; if something else
still holds it, the script fails at that step with exactly
`Unexpected app in foreground.` and nothing is sent. The other app is never
named in errors, results or the timeline. At the end the user's previous
foreground app gets its focus back.

Windows are matched by process too: once the app's main window is found,
only that process's windows are driven, so another app's "Save As" (or the
user's own copy of the app) is never touched.

`run` prints a hands-off notice first when any selected script sends real
input on this platform; `check` lists those scripts. If a VM app is open
(VMware, VirtualBox, Hyper-V, Parallels) the notice says so: a VM window
that holds input grabs the keyboard and mouse with a low-level hook and
swallows synthetic input even while the app under test is in front, which
no foreground check can see. Release the grab (Ctrl+Alt) or minimize it. Platforms that can't
tell who is in front (Linux without X focus info, e.g. gamescope) proceed
as before. On Linux the point check isn't implemented yet; keys are checked.

## Platform notes

Element names come from `aria-label` / visible text and are usually the same
on every OS; roles are platform-native, so give them only to disambiguate.
Known backend differences (keys into WebView2, native `<select>` popups, the
macOS menu bar) are listed in MarkTheCrab's `smoke/README.md` until they move
here.

**Video on Linux Wayland:** `--video` (`record_video`) isn't supported on a
Wayland session, even for an XWayland app: ffmpeg's x11grab would record the
XWayland root, which the compositor never draws into, so every frame is black.
The run goes on without video and result.json carries `video_error`.
Screenshots work (they go through the desktop's own tool, e.g. spectacle).
