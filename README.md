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

## Housekeeping

Run state expires on a schedule so the directory cannot grow without limit.
`codex-fleet gc` does three things and prints what it did:

| stage | default | what it touches |
| --- | --- | --- |
| collapse | after 2 days | a finished run loses its event log and keeps its result, token count and thread id, so `say` still resumes it |
| delete | after 14 days | a collapsed run is removed entirely |
| sweep | every run | a worktree is removed only when it has no uncommitted changes and its branch adds nothing to the default branch |

The sweep tests for a squash merge as well as an ordinary one, because this
repository squash-merges and a squashed branch is never an ancestor of `main`
even though every line of it shipped. A worktree it will not remove is named
with the reason, because a worktree deleted by mistake is lost work.

It runs at the end of every `spawn`, and from cron at 06:30 daily for the
weeks nobody spawns anything. Pruning used to run only on spawn, which meant a
fleet that went quiet kept everything forever.

`--dry-run` changes nothing. `--delete-days 0` never deletes. Override the
defaults with `CODEX_FLEET_PRUNE_DAYS` and `CODEX_FLEET_DELETE_DAYS`.

## One stream per session

`codex-fleet events` takes a lock named after the session that owns it. A
second monitor for the same session refuses to start and names the process
already streaming, rather than delivering every notification twice with two
separate memories of what it already reported. A lock left by a killed monitor
is taken over, so a crash cannot disable notifications until someone finds a
file they do not know exists.

A running monitor also checks whether the script it loaded has changed, and
exits when it has. Python reads the whole file once at startup, so without
this a monitor keeps running the version it started with no matter how many
times the tool is rewritten. One ran for two days on code that had been
replaced 21 minutes after it started, which is why it still had no session
filter and reported every run on the machine into every session.

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
