#!/usr/bin/env python3
"""Push codex-fleet state into the agent's context when, and only when, it changes.

The orchestrating agent gets no stream from its own fleet. The one push signal
is a task notification when a backgrounded `codex-fleet wait` exits, so between
spawn and finish the agent is blind unless it remembers to poll. It does not
always remember. That is how two finished reviewers sat unread for twenty
minutes on 2026-08-21.

This closes the gap the same way Mnemonik closes it for memory: the host pushes,
the agent does not have to ask.

NO-NOISE CONTRACT. Silence is the default. Output happens only on a transition:
a run that was running has stopped, a run has newly appeared, or a running agent
has gone quiet past a stall threshold it has not already been reported at. A
tool call where nothing moved prints nothing at all.

FAIL-OPEN. Every error path exits 0 with no output. An observability hook that
can break a tool call is worse than no observability hook.
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CODEX_FLEET_HOME", Path.home() / ".codex-fleet"))
RUNS = ROOT / "runs"
STATE = ROOT / ".hook-seen.json"

# A run quiet this long is worth a word. Each bucket reports at most once per
# run, so a genuinely stuck agent says "10m" then "30m" rather than repeating
# every tool call.
STALL_BUCKETS_SECONDS = (600, 1800, 3600)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def read_events(path):
    if not path.exists():
        return []
    events = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def status_of(meta, turn_path):
    if alive(meta.get("pid")):
        return "running"
    for ev in reversed(read_events(turn_path)):
        kind = ev.get("type")
        if kind == "turn.completed":
            return "idle"
        if kind in ("turn.failed", "error"):
            return "failed"
    return "stopped"


def last_message(turn_path):
    for ev in reversed(read_events(turn_path)):
        item = ev.get("item") or {}
        if item.get("type") == "agent_message":
            return " ".join((item.get("text") or "").split())
    return ""


def survey():
    out = {}
    if not RUNS.exists():
        return out
    for d in sorted(RUNS.iterdir()):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        turn_path = d / f"turn-{meta.get('turns', 1)}.jsonl"
        quiet = int(time.time() - turn_path.stat().st_mtime) if turn_path.exists() else 0
        out[d.name] = {
            "status": status_of(meta, turn_path),
            "role": meta.get("role", "?"),
            "quiet": quiet,
            "turn": meta.get("turns", 1),
            "path": str(turn_path),
        }
    return out



def drain_outbox(name):
    """Questions the agent raised mid-turn, marked read so they arrive once.

    An agent that hits a wrong brief or a missing decision writes here with
    `codex-fleet ask` and keeps working. This is the half of the loop that gets
    it to the orchestrator while the answer is still worth something.
    """
    path = RUNS / name / "outbox.jsonl"
    if not path.exists():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    unread, rewritten, changed = [], [], False
    for ln in lines:
        try:
            msg = json.loads(ln)
        except ValueError:
            rewritten.append(ln)
            continue
        if not msg.get("read"):
            unread.append(msg)
            msg["read"] = True
            changed = True
        rewritten.append(json.dumps(msg))
    if changed:
        try:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return unread


def stall_bucket(seconds):
    hit = 0
    for bucket in STALL_BUCKETS_SECONDS:
        if seconds >= bucket:
            hit = bucket
    return hit


def main():
    runs = survey()
    if not runs:
        return

    try:
        seen = json.loads(STATE.read_text()) if STATE.exists() else {}
    except (json.JSONDecodeError, OSError):
        seen = {}

    lines, questions = [], []
    for name, run in runs.items():
        for msg in drain_outbox(name):
            mark = "BLOCKING" if msg.get("blocking") else "asks"
            questions.append(f"{name} {mark}: {msg.get('text','').strip()}")

        before = seen.get(name) or {}
        was = before.get("status")
        now = run["status"]

        if was == "running" and now != "running":
            verb = {"idle": "finished", "failed": "FAILED", "stopped": "stopped without completing"}
            msg = last_message(Path(run["path"]))
            tail = f' Last message: "{msg[:200]}"' if msg else ""
            lines.append(
                f"{name} ({run['role']}) {verb.get(now, now)}."
                f" Read it with: codex-fleet result {name}.{tail}"
            )
        elif was is None and now == "running":
            lines.append(f"{name} ({run['role']}) started.")

        bucket = stall_bucket(run["quiet"]) if now == "running" else 0
        if bucket and bucket > (before.get("stall") or 0):
            lines.append(
                f"{name} ({run['role']}) has produced no event for {bucket // 60} minutes."
                f" Check it with: codex-fleet tail {name} -n 15, or kill it if it is wedged."
            )
        run["stall"] = bucket if now == "running" else 0

    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(
            json.dumps({k: {"status": v["status"], "stall": v.get("stall", 0)} for k, v in runs.items()})
        )
    except OSError:
        pass

    if not lines and not questions:
        return

    running = [n for n, r in runs.items() if r["status"] == "running"]
    body = "Codex fleet update (pushed by a hook, you did not ask for it):"
    if questions:
        body += (
            "\n\nAN AGENT IS ASKING YOU SOMETHING. It is still working and will"
            " see your reply on its next tool call. Answer with:"
            ' codex-fleet tell <name> "<answer>"\n- ' + "\n- ".join(questions)
        )
    if lines:
        body += "\n\n- " + "\n- ".join(lines)
    if running:
        body += f"\nStill running: {', '.join(running)}."
    else:
        body += "\nNo fleet agents are running now."

    event = os.environ.get("CLAUDE_HOOK_EVENT_NAME") or "PostToolUse"
    print(
        json.dumps(
            {
                "hookSpecificOutput": {"hookEventName": event, "additionalContext": body},
                "suppressOutput": True,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail open, always. See module docstring.
        pass
    sys.exit(0)
