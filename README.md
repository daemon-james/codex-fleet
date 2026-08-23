# codex-fleet

Run Codex agents as background subagents you can fan out, watch live, steer
mid-turn, and be interrupted by.

**This is a development tool. It is not part of Mnemonik and never ships with
it.** It is used to build Mnemonik, which is why it knows how to authorize
Mnemonik's MCP tools in an agent brief, but it has no other relationship to that
product and its code must never be committed there.

```bash
./install.sh          # symlink the CLI and both hooks into place
codex-fleet spawn "Trace how X works. Report file:line." -n scout -r engineer -e high -C /path/to/repo
codex-fleet list      # every run, most recent first
codex-fleet tell scout "actually, start from the handler"   # reaches it mid-turn
codex-fleet result scout
```

## What is here

| Path | What it is |
| --- | --- |
| `codex-fleet` | the whole CLI, one file, with the dashboard embedded |
| `dashboard.html` | source of truth for the dashboard. Never edit the copy inside the CLI |
| `tools/embed-page.py` | puts `dashboard.html` into the CLI and verifies it survived |
| `hooks/codex-fleet-inbox.py` | Codex `PostToolUse`. Delivers `tell` messages into a running agent |
| `hooks/codex-fleet-status.py` | Claude `PostToolUse`. Surfaces agent state and `ask` questions to the orchestrator |
| `install.sh` | symlinks all three into the two host directories. Idempotent |
| `tests/` | `python3 -m unittest discover -s tests` |

## Two guides, two jobs

**Using it** is covered by the Claude Code skill at
`~/.claude/skills/codex-fleet/SKILL.md`: when to use a worktree, how to brief an
agent, how to tell a sandbox artefact from a real test failure, what to do with
idle agents.

**Changing it** is covered by `MAINTAINING.md` in this repo. Read that first.
It carries the traps, and every one of them cost something to find.

## Session ownership

Two orchestrator sessions can run fleets on one machine without talking over
each other. `spawn` stamps the spawning session's identity
(`CLAUDE_CODE_SESSION_ID`, or `CODEX_FLEET_OWNER` to override) into the run's
`meta.json`. The status hook, the stop hook, and `codex-fleet events` then
speak only about the invoking session's runs; `codex-fleet events --all`
restores the machine-wide view. Runs with no owner (spawned from a plain
shell, or predating this field) stay visible to every session, and a viewer
with no identity sees everything, so single-orchestrator behavior is
unchanged. `list` always shows every run and tags foreign ones with
`[other session]`.
