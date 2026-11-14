# crab: a Claude Code session that restarts itself up to date

A crab is a long-running Claude Code session on one machine. When
bad-ass-mcp changes, its MCP server is still the code it started with
until the session restarts, and nobody wants to restart five crabs by
hand. `scripts/crab` is a wrapper that starts the session, and
`scripts/crab-restart` lets the crab bounce itself from inside.

## Start a crab

```
~/code/bad-ass-mcp/scripts/crab rusty-mac-crab
```

The first run picks a session id for that name and starts
`claude --dangerously-skip-permissions --session-id <id>`. Every later run
resumes the same id. Anything after the name is passed to claude.

Before each start the wrapper pulls bad-ass-mcp (fast-forward only) and
runs `~/.claude/crab/update` if that file exists and is executable, for
whatever else the machine needs pulled or rebuilt. What the pull reaches
depends on how the MCP server is launched:

| server command | what updates it |
|---|---|
| `python3 -m bad_ass_mcp.server` with `PYTHONPATH` at the checkout's `src` (Kali) | the pull alone |
| `uv run --directory <checkout> bad-ass-mcp` (the Deck) | the pull alone; uv rebuilds its environment from it |
| `bad-ass-mcp` from an installed package (the Mac) | the pull plus `CRAB_INSTALL=1`, which installs the checkout editable into `CRAB_PYTHON` (default `python3`, the interpreter that server runs from) once, after which the pull is enough |
| `bad-ass-mcp.exe` from `uv tool install --editable` (Windows) | the pull alone for code; `CRAB_INSTALL=1` reinstalls the tool (`uv tool install --editable "<checkout>[windows]" --force`) when dependencies changed. Set `CRAB_EXTRAS` to the extras, e.g. `[windows]` |

`CRAB_REPO` must be the checkout the server runs from. On a machine with
two checkouts, one for the server and one for development, point it at the
server's.

## Restart from inside

In the session, the crab runs:

```
~/code/bad-ass-mcp/scripts/crab-restart
```

That writes `~/.claude/crab/<name>.restart`, finds the claude process above
the shell, and ends it a second later. The wrapper sees the flag, updates,
and resumes the session in the same terminal. `--dry-run` prints the
process it would end and changes nothing. A plain exit, with no flag, ends
the loop.

`CRAB_NAME` and `CRAB_HOME` are exported into the session, so the crab
knows its name and the restart command needs no arguments.

## Windows

`scripts/crab.ps1` and `scripts/crab-restart.ps1` are the same two in
PowerShell. From the Git Bash the hooks run in:

```
powershell -ExecutionPolicy Bypass -File ~/code/bad-ass-mcp/scripts/crab-restart.ps1
```

## Files

| path | holds |
|---|---|
| `~/.claude/crab/<name>.session` | the session id; delete it to start fresh |
| `~/.claude/crab/<name>.restart` | the flag, removed before every start |
| `~/.claude/crab/update` | optional, run before every start |

## Limits

- On Windows `Stop-Process` is a hard kill either way; claude's transcript
  is written per message, so the resume survives it, but nothing graceful
  happens. Do not run `crab-restart.ps1` under Git's `timeout.exe`: its
  parent is gone by the time the walk starts and claude is never found.

- The crab cannot see the restart happen; its last tool call dies with the
  process. Say what is about to happen before running it.
- `--resume` needs the transcript to still exist. If Claude Code has
  cleaned it up (`cleanupPeriodDays`), the resume fails and the loop ends;
  delete the session file and start again.
- The update step is bad-ass-mcp only. Other checkouts belong in
  `~/.claude/crab/update`.
