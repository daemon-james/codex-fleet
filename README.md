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
| sweep | every run | a worktree whose work has landed |

A worktree is swept only when git reports it clean and its branch adds nothing
to the default branch. That second test allows for a squash merge as well as
an ordinary one, because this repository squash-merges and a squashed branch
is never an ancestor of `main` even though every line of it shipped.

Ignored files need their own rule, because `git status` does not mention them
and `git worktree remove --force` deletes them anyway. Two kinds:

- **`node_modules` and anything named `.env*`** refuse the removal when they
  have been edited, or when the agent created one that has no counterpart to
  compare against. These cannot be regenerated, and `.env.development.local`
  is exactly as unrecoverable as `.env`.
- **Everything else the project ignores** is copied into the run directory as
  `ignored-files.tar.gz` and then removed with the worktree. A .gitignore says
  a file is untracked, not that it can be rebuilt, so a printed line is not a
  good enough record. Files over 1 MB, and anything past a 5 MB total, are not
  copied and are named individually as gone for good; that is the limit that
  keeps build output from being archived, and a built worktree here holds 414
  ignored paths.

Anything not removed is named with the reason. Refusing on every ignored file
instead would mean no worktree is ever removed, which is the pile-up this
exists to prevent. It runs at the end of every
`spawn`, and `install.sh` adds a 06:30 cron entry for the weeks nobody spawns
anything. Pruning used to run only on spawn, so a fleet that went quiet kept
everything forever.

`--dry-run` changes nothing. `--delete-days 0` never deletes. Override the
defaults with `CODEX_FLEET_PRUNE_DAYS` and `CODEX_FLEET_DELETE_DAYS`.

## One stream per session

`codex-fleet events` claims a name in the kernel, one per session. A second
monitor for the same session refuses to start and names the process already
streaming, rather than delivering every notification twice from two streams
with separate memories of what they had already reported. Different sessions
claim different names and are unaffected.

The name is held by the running process, so the kernel releases it the moment
that process exits, however it exits, and there is no stale lock to clear. It
lives in Linux's abstract socket namespace and has no filesystem entry, which
matters more than it sounds: every disk-based version of this could be
defeated by deleting the file, because the holder locks an inode and the next
monitor simply creates a new file and locks that instead. The pid file beside
it is a breadcrumb for the refusal message. Deleting it costs a helpful
message, not correctness.

The name is a digest of the user id, the fleet home and the session, not the
session spelled out. Abstract names are capped at 107 bytes, and an 89
character session id made the bind fail, which the caller read as "somebody
else holds it", so the first monitor refused itself and no notifications
arrived at all. Hashing also stops two users, or two installations, colliding
on the shared `anon` name. One limit remains and is not defended against: the
namespace is per network namespace, so two containers that do not share one
can each bind the name, and would then both stream if they also share a fleet
home.

A running monitor also hashes the script it loaded on each poll and exits when
the contents change. Python reads the whole file once at startup, so without
this a monitor keeps running the version it started with no matter how many
times the tool is rewritten. One ran for two days on code that had been
replaced 21 minutes after it started, which is why it still had no session
filter and reported every run on the machine into every session. Size and
timestamp are not enough on their own: an edit that keeps the same length and
puts the timestamp back leaves both unchanged.

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
