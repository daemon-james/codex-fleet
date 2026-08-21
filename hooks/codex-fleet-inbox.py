#!/usr/bin/env python3
"""Deliver orchestrator messages into a running codex-fleet agent's turn.

`codex exec` is a batch process. It reads stdin only for the initial prompt and
there is no way into a turn once it starts, which is why `codex-fleet say`
refuses while an agent runs. This hook is the way in: `codex-fleet tell` appends
a message to the run's inbox, and the next tool call the agent makes carries it
back as additionalContext. No restart, no lost turn.

Silent and fail-open by construction. It emits nothing unless CODEX_FLEET_RUN is
set AND that run has unread messages, so every interactive Codex session on this
machine is unaffected. Any error at all exits 0 with no output: a broken mailbox
must never stop an agent from working.
"""
import json
import os
import sys

TIMEOUT_GUARD_BYTES = 16000


def main() -> int:
    run = os.environ.get("CODEX_FLEET_RUN")
    if not run:
        return 0

    inbox = os.path.expanduser(f"~/.codex-fleet/runs/{run}/inbox.jsonl")
    if not os.path.exists(inbox):
        return 0

    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}
    hook_event = event.get("hook_event_name") or "PostToolUse"

    try:
        with open(inbox, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        return 0

    unread, rewritten, changed = [], [], False
    for ln in lines:
        try:
            msg = json.loads(ln)
        except Exception:
            rewritten.append(ln)
            continue
        if msg.get("read"):
            rewritten.append(ln)
            continue
        unread.append(msg)
        msg["read"] = True
        changed = True
        rewritten.append(json.dumps(msg))

    if not unread:
        return 0

    body = "\n\n".join(m.get("text", "") for m in unread if m.get("text"))
    if not body.strip():
        return 0
    if len(body) > TIMEOUT_GUARD_BYTES:
        body = body[:TIMEOUT_GUARD_BYTES] + "\n\n[truncated by codex-fleet inbox]"

    plural = "message" if len(unread) == 1 else "messages"
    text = (
        f"Live {plural} from the orchestrator who dispatched you. This reached "
        f"you mid-turn, so it is newer than your brief and it overrides the "
        f"brief where the two disagree. Act on it before your next step.\n\n"
        f"{body}"
    )

    if changed:
        try:
            tmp = inbox + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rewritten) + "\n")
            os.replace(tmp, inbox)
        except Exception:
            # Redelivery beats a lost message. Fall through and emit anyway.
            pass

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": text,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
