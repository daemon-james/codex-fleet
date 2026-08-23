#!/usr/bin/env python3
"""Claude Code Stop hook: do not let the orchestrator end its turn and lose the
fleet.

Two conditions block a stop, each with the exact command that clears it:

1. An agent is running and no OBSERVED `codex-fleet wait` covers it. A wait is
   observed only when it runs as a tracked background Bash task (a `claude`
   process in its ancestry). A shell `&` wait is orphaned to PID 1 and its exit
   wakes nobody, so it does not count.

2. An agent is idle and its latest turn's result has never been read with
   `codex-fleet result`. That is the "finished into silence" case: work done,
   nobody looked.

This exists because the same thread was dropped on 2026-08-21 and three times
on 2026-08-22. A nag on the next tool call did not fix it, because the failing
move is ending the turn, after which there is no next tool call. Blocking the
stop is the only hook that fires at that moment.

Honours `stop_hook_active`: one correction per stop, never a loop. Fails open
on any error, like every hook in this repo.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CODEX_FLEET_HOME", Path.home() / ".codex-fleet"))
RUNS = ROOT / "runs"
HOOKS = Path(__file__).resolve().parent


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    if payload.get("stop_hook_active"):
        return

    sys.path.insert(0, str(HOOKS))
    import importlib.util

    spec = importlib.util.spec_from_file_location("status", HOOKS / "codex-fleet-status.py")
    status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status)

    owner = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    running, unread = [], []
    if RUNS.is_dir():
        for d in sorted(RUNS.iterdir()):
            meta_path = d / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if status.foreign(meta, owner):
                # Another session's run. Blocking this session's stop over it
                # forces the wrong orchestrator to consume the result, which
                # silently clears the owner's own unread reminder.
                continue
            name = d.name
            if alive(meta.get("pid")):
                running.append(name)
            elif "result_read_turn" in meta and meta.get("turns", 0) > meta["result_read_turn"]:
                # Only runs that have ever been read through `result` can be
                # proven unread. A run that predates the field was read or not
                # before anyone recorded it, and blocking on it forever would
                # make the hook unusable: 44 historical runs tripped it on the
                # first dry run.
                unread.append(name)

    loose = status.unwatched(running)
    if not loose and not unread:
        return

    lines = []
    if loose:
        names = " ".join(sorted(loose))
        lines.append(
            f"{len(loose)} fleet agent(s) are running and nothing will wake you when they "
            f"finish: {names}. Arm the fleet connection once for this session: "
            f'Monitor({{ command: "codex-fleet events", persistent: true, description: "codex fleet" }}). '
            f"Every fleet event then reaches you as a notification, idle or not, with nothing to re-arm."
        )
    if unread:
        names = " ".join(sorted(unread))
        lines.append(
            f"{len(unread)} fleet agent(s) have finished and their result has never been "
            f"read: {names}. Read each with: codex-fleet result <name>. Act on it, "
            f"then end the turn."
        )
    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail open. A hook that can stop the orchestrator stopping is worse
        # than a dropped thread.
        pass
