# Hook setup

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

## Briefing an orchestrator

Give your orchestrator these instructions alongside the actual task:

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
symlinks created by `install.sh`. Keep `~/.codex-fleet` if you want to retain
run results and resumable thread IDs.
