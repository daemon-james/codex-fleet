# Setup

## Agent installation procedure

When the user asks you to install Fleet, carry out this procedure. The shell
installer installs files and the skill; you merge the hook JSON using the
examples below. Do not hand configuration editing back to the user.

1. **Inspect the machine.** Confirm Linux, Python 3.10+, Bash, Git, `crontab`,
   and a working Codex CLI. Check Codex authentication without displaying
   credentials. If a prerequisite is missing, use its official installation
   instructions within the user's authorization; let the user complete login.
   Stop on unsupported platforms rather than reporting a successful install.
2. **Choose a permanent checkout.** Reuse an existing clean Fleet checkout if
   available; otherwise clone `https://github.com/daemon-james/codex-fleet.git`
   under the user's usual projects directory. Preserve local changes. Read
   `install.sh`, this guide and `skills/codex-fleet/SKILL.md` before executing.
   Check `ROLES` and `EFFORTS` against models available to this account. If
   they are unavailable, report the mismatch and agree on replacements rather
   than guessing model identifiers or claiming the installation works.
3. **Install.** Run `./install.sh` from the checkout and resolve any nonzero
   exit. Ensure `~/.local/bin` is available to the current agent's shell and
   future sessions; add it to the appropriate shell profile only if missing.
   Verify `~/.claude/skills/codex-fleet/SKILL.md` resolves to the bundled skill.
   The installer preserves an existing real file in a timestamped backup.
   It also schedules daily cleanup as described in the README.
4. **Register hooks.** Back up existing `~/.codex/hooks.json` and
   `~/.claude/settings.json` before editing. Parse their JSON, preserve unrelated
   keys and hook groups, and add only missing Fleet hooks from the examples
   below. Use actual absolute paths, shell-quoted if the home path contains
   spaces. Detect an existing Fleet hook by its script path so rerunning setup
   does not duplicate it. If JSON is invalid, resolve that without replacing
   the user's settings with an empty object. Parse the saved files again.
   Do not manufacture trust hashes or bypass a host's approval prompt. Ask the
   user to complete hook trust when required. If the host requires a new
   session to discover the skill or hooks, say exactly what must be restarted.
5. **Verify the installation.** Run `./install.sh --check`,
   `python3 -m unittest discover -s tests`, and `codex-fleet --help`.
   Inspect the four hook registrations and confirm the skill file is readable.
   `--check` checks symlinks and cron; it does not prove registration or trust.
   Then run the small live check below. Read logs on failure; make no repeated
   paid model attempts if login, model access or hook support is missing.
6. **Report the result.** Give the checkout path, skill path, dashboard URL,
   and what passed. If any user interaction or compatibility issue prevents
   completion, name that step explicitly. Do not call a queued message a
   verified delivery or a file on disk a verified hook activation.

### Small live check

Use a temporary empty working directory and a unique Fleet run name. The
user's installation prompt authorizes this small model run. Tell the agent:

> This is a Fleet setup check. Do not change project files. Run
> `codex-fleet ask "Setup check: please send the verification phrase"`.
> Make up to ten short shell tool calls (at most two seconds each) to allow
> the inbox hook to deliver a reply. If it arrives, include the exact phrase
> in your final response. Otherwise report that no reply arrived and finish.

Start the run with the configured engineer role and `--no-prune`. Monitor it
using the bundled skill. Read the question with `inbox`, then send a newly
chosen phrase with `tell` while the agent is running. Read the final result
and verify it contains that phrase. This checks agent launch, question return,
message delivery and result collection without modifying the user's project.
If the agent finished before delivery, report the timing outcome; do not keep
spawning retries. A second attempt needs a specific resolved cause.

Check `serve --status` before starting a dashboard. If none is running, use
`codex-fleet serve --daemon --host 127.0.0.1` and verify `/api/runs` responds.
Do not restart an existing server or change its exposure during installation.
On a remote machine, report that localhost refers to that machine and provide
an SSH tunnel command with the actual host. Keep the smoke run available for
inspection; remove only the temporary empty working directory once it ends.

## Hook configuration reference

The installer puts hook scripts in place. Registration is a separate step.
Merge the entries below into your existing JSON files, preserving other hooks
and settings. Replace `/absolute/path/to/home` with your home directory.
These examples match the hook registrations used by this project.

## Codex: deliver messages during a turn

In `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/home/.codex/hooks/codex-fleet-inbox.py",
        "timeout": 5
      }]
    }]
  }
}
```

Approve the hook when Codex requests trust. Start a new Fleet run after setup.
`tell` is delivered at a subsequent tool call, not immediately on receipt.
The inbox hook currently uses `~/.codex-fleet`, so retain the default Fleet
state directory when using live steering.

## Claude Code: status and questions

Optional entries for `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": ".*",
      "hooks": [{"type": "command", "command": "/absolute/path/to/home/.claude/hooks/codex-fleet-status.py", "timeout": 5}]
    }],
    "UserPromptSubmit": [{
      "hooks": [{"type": "command", "command": "/absolute/path/to/home/.claude/hooks/codex-fleet-status.py", "timeout": 5}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "/absolute/path/to/home/.claude/hooks/codex-fleet-stop.py", "timeout": 5}]
    }]
  }
}
```

The status hook surfaces run state and agent questions. The stop hook prompts
the orchestrator to collect unread results or monitor running agents before
ending its turn. `codex-fleet events` streams changes for a supervising process;
`codex-fleet wait` is available for terminal and script workflows.

## Using the installed skill

`install.sh` installs the complete skill at
`~/.claude/skills/codex-fleet/SKILL.md`. In a Claude Code session that has loaded
it, ask Claude to use codex-fleet for your task. If the current session has not
discovered the skill, read that file explicitly or open a new session.

For a short brief alongside the actual task:

> Use codex-fleet to delegate bounded tasks. Check `codex-fleet list` before
> spawning so you can reuse an agent with relevant context. Give concurrent
> writers separate worktrees with `spawn --worktree`, and name the files each
> agent owns. Monitor their progress and collect each result. Use `tell` for
> running agents and `say` for follow-ups after a turn finishes. Verify the
> resulting diff and run the project's checks before reporting completion.

Include this in an agent's task when you want questions during execution:

> If you need a decision, run `codex-fleet ask "your question"` and continue
> independent work. Add `--blocking` if the task cannot finish without an
> answer. A reply can arrive through the inbox hook on a subsequent tool call.

## Updating and removing the installation

Update the checkout with `git pull --ff-only`. Symlinked commands and hooks
use the updated files. Update between agent runs: event monitors exit when
the CLI changes. Restart your dashboard when ready using `serve --stop`, then
`serve --daemon --host 127.0.0.1`.

To uninstall, remove the Fleet entries from both hook configuration files,
remove its daily `codex-fleet gc` line with `crontab -e`, then remove the
symlinks created by `install.sh`, including
`~/.claude/skills/codex-fleet/SKILL.md`. Keep `~/.codex-fleet` if you want to retain
run results and resumable thread IDs.
