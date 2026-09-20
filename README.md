# codex-fleet

Run Codex agents as background subagents you can fan out, watch live, steer
mid-turn, and be interrupted by.

**Experimental, Linux-first developer tool.** Run agents from your terminal or
let Claude Code orchestrate them. Fleet uses Python's standard library; there
is no Python dependency installation or frontend build step.

## Install with Claude Code

Paste this into Claude Code on your Linux machine:

```text
Install codex-fleet from https://github.com/daemon-james/codex-fleet.
Read its README.md and follow the agent installation procedure in SETUP.md.
Install the bundled Claude skill, merge the hooks into my existing settings,
and verify the setup with one small agent run. Preserve my existing settings
and files. Use a localhost dashboard. I understand Fleet runs Codex agents
with full filesystem access. Do the setup; ask me only for required login,
hook trust, or a decision you cannot resolve from my machine.
```

Claude handles the clone, installer, skill, hook configuration and checks.
You handle any interactive login or trust prompt. The bundled
[skill](skills/codex-fleet/SKILL.md) teaches Claude how to choose roles, supervise
agents, steer them, use worktrees, and verify their results.

## Requirements and installation

Release checks ran on Linux with Python 3.12.3 and Codex CLI 0.153.4.

- Linux, Python 3.10+, Git, Bash, and `crontab`.
- A working, authenticated Codex CLI on `PATH`.
- Access to the models in the role table below. Roles are currently hardcoded;
  edit `ROLES` and `EFFORTS` in `codex-fleet` if your account uses different models.
- Claude Code is optional, for orchestration and status notifications.

```bash
git clone https://github.com/daemon-james/codex-fleet.git
cd codex-fleet
./install.sh
export PATH="$HOME/.local/bin:$PATH"
codex-fleet --help
codex-fleet spawn "Describe this repository. Do not change files." -n scout -C /path/to/repo
codex-fleet watch scout
codex-fleet result scout
codex-fleet serve --daemon --host 127.0.0.1
# Open http://127.0.0.1:8787
```

Keep the checkout: installation creates symlinks into it. Add the PATH export
to your shell profile if needed. The installer also adds a daily cleanup cron
job; see Housekeeping below for retention and worktree removal behaviour.

**Execution settings:** this version explicitly launches agents with
`danger-full-access`. A working directory or Git worktree is not a filesystem
security boundary. Use Fleet only where you intend to grant that access.
The dashboard exposes agent prompts, logs and results without authentication.
Pass `--host 127.0.0.1` for local access; the current default is `0.0.0.0`.

For live steering with `tell`, register the inbox hook as described in
[SETUP.md](SETUP.md). That guide also covers optional Claude Code hooks.
Without the inbox hook, messages queue but do not reach running agents.

```bash
codex-fleet list
codex-fleet tell scout "Focus on the deployment scripts."
codex-fleet say scout "Explain the main entry point." # follow up after it finishes
codex-fleet spawn "Implement the feature." -n builder -C /path/to/repo -w --base feature/ready
```

`spawn -w` normally branches from the `-C` checkout's `HEAD`; `--base <ref>`
starts it from another local Git ref. Worktrees use `make worktree-init` when
the repository provides that target. Otherwise Fleet runs its npm install and,
for npm workspaces, the workspace build. Provisioning failures leave the
worktree available, print a warning, and appear as `PROVISIONING FAILED` in
`codex-fleet list`.

This first release publishes the existing implementation. Linux-specific
process inspection and socket locking need porting before macOS or native
Windows support. Model access and hook compatibility depend on your Codex
installation.

Run `codex-fleet serve --daemon` for a detached dashboard, inspect it with `serve --status`, and stop it with `serve --stop`; `list` always reports its address first, ask events retain the full question and blocking flag, `inbox --all` includes read questions, latest-turn MCP success/failure totals appear in `list`, `result`, and finished events, and dashboard polling reads only newly appended turn-log bytes.

## Roles and effort

The role selects the model. High is the default effort for every role.

| Role | Model | Available efforts |
| --- | --- | --- |
| Engineer (`engineer`) | Sol (`gpt-5.6-sol`) | High, Extra High |
| Reviewer (`reviewer`) | Terra (`gpt-5.6-terra`) | High, Extra High |
| Advanced Engineer (`advanced-engineer`) | Astra (`gpt-6-astra`) | Medium, High, Extra High |

```bash
codex-fleet spawn "<engineering task>" -r advanced-engineer -C /path/to/repo
codex-fleet spawn "<harder engineering task>" -r advanced-engineer -e xhigh -C /path/to/repo
```

Use `-e medium` for lighter Astra work. `say` keeps a run's model and effort
unless you supply `-e`; the new effort must be available to that run's role.

## Files

The dashboard uses a full-screen run list and a separate activity view on
phones. Tap a run to read its log, **Runs** to return, and **Details & filters**
to expand metadata and event controls. Desktop keeps the side-by-side layout.
Browser tabs and iPhone Home Screen bookmarks use the fleet icon.
The bar beneath the fleet heading shows your weekly subscription allowance
remaining and refreshes once a minute.

| Path | What it is |
| --- | --- |
| `codex-fleet` | the whole CLI, one file, with the dashboard embedded |
| `dashboard.html` | source of truth for the dashboard. Never edit the copy inside the CLI |
| `tools/embed-page.py` | puts `dashboard.html` into the CLI and verifies it survived |
| `hooks/codex-fleet-inbox.py` | Codex `PostToolUse`. Delivers `tell` messages into a running agent |
| `hooks/codex-fleet-status.py` | Claude `PostToolUse`. Surfaces agent state and `ask` questions to the orchestrator |
| `hooks/codex-fleet-stop.py` | Claude `Stop`. Checks for unobserved runs and unread results |
| `skills/codex-fleet/SKILL.md` | Claude Code orchestration skill, installed by `install.sh` |
| `install.sh` | symlinks the CLI, three hooks and skill into place. Idempotent |
| `tests/` | `python3 -m unittest discover -s tests` |

## Two guides, two jobs

**Using it** is covered by this README and [SETUP.md](SETUP.md), including
the agent installation procedure and hook registration. The bundled
[skill](skills/codex-fleet/SKILL.md) covers orchestration.

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

Monitor hosts may stop the stream after 30 minutes; restart `codex-fleet events`
with the same session identity. Its run/turn/stall snapshot survives restarts,
so only changes and still-unanswered questions are shown. `tell` retires both
blocking and nonblocking questions; answered questions never replay. `--all`
keeps a separate snapshot for the machine-wide view.

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
