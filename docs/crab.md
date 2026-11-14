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

Before each start the wrapper pulls bad-ass-mcp (fast-forward only), makes
the installed package the checkout (`pip install -e`) if it is not already,
and runs `~/.claude/crab/update` if that file exists and is executable, for
whatever else the machine needs pulled or rebuilt. Set `CRAB_PYTHON` to the
interpreter the MCP server runs from when it is not `python3`.

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

- The crab cannot see the restart happen; its last tool call dies with the
  process. Say what is about to happen before running it.
- `--resume` needs the transcript to still exist. If Claude Code has
  cleaned it up (`cleanupPeriodDays`), the resume fails and the loop ends;
  delete the session file and start again.
- The update step is bad-ass-mcp only. Other checkouts belong in
  `~/.claude/crab/update`.
