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
import subprocess
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


def foreign(meta, owner):
    """A run another session owns. Unowned runs belong to everyone, and a
    viewer with no identity (a human shell) sees everything."""
    run_owner = meta.get("owner")
    return bool(run_owner) and bool(owner) and run_owner != owner


def survey(owner=None):
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
        if foreign(meta, owner):
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
    """Questions the agent raised mid-turn.

    A NON-blocking question arrives once. It is informational and repeating it
    is noise.

    A BLOCKING question repeats on every fire until `codex-fleet tell` answers
    it. Marking one read used to silence it, which meant looking at a question
    counted as answering it. On 2026-08-22 that lost one: this hook drained a
    blocking question, its output did not reach the orchestrator, and
    `codex-fleet inbox` then printed "nothing raised" while the agent slept in
    30-second polls for twenty minutes. An agent that has stopped does not start
    again because someone glanced at it.
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
        blocking_unanswered = msg.get("blocking") and not msg.get("answered")
        if not msg.get("read") or blocking_unanswered:
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


UNWATCHED_NAG_SECONDS = 300


def observed_pids(verb):
    """PIDs of `codex-fleet <verb>` processes with a live `claude` ancestor."""
    observed = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", rf"codex-fleet {verb}"], capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return observed
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if has_claude_ancestor(pid) and same_fleet_home(pid):
            observed.append(pid)
    return observed


def same_fleet_home(pid):
    """True when `pid` watches the same fleet as this hook.

    A monitor armed for another CODEX_FLEET_HOME (a test harness, a second
    checkout) must not count as coverage for this one. Compares the process's
    CODEX_FLEET_HOME, defaulting to ~/.codex-fleet, against ROOT.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            env = fh.read().split(b"\0")
    except OSError:
        return False
    theirs = None
    their_home = None
    for kv in env:
        if kv.startswith(b"CODEX_FLEET_HOME="):
            theirs = kv.split(b"=", 1)[1].decode("utf-8", "replace")
        elif kv.startswith(b"HOME="):
            their_home = kv.split(b"=", 1)[1].decode("utf-8", "replace")
    if theirs is None:
        theirs = str(Path(their_home or Path.home()) / ".codex-fleet")
    try:
        return Path(theirs).resolve() == Path(ROOT).resolve()
    except OSError:
        return False


def events_monitor_armed():
    """True when a persistent `codex-fleet events` Monitor is running for this
    Claude session. That is the inbound half of the fleet connection: every
    line it prints wakes the orchestrator, idle or not, with nothing to re-arm.
    """
    return bool(observed_pids("events"))


def observed_wait_pids():
    """PIDs of `codex-fleet wait` processes that something will actually observe.

    A wait is only useful if its exit wakes the orchestrator. From Claude Code
    that is true only when it runs as a tracked background Bash task, in which
    case `claude` sits in its ancestor chain. A wait started with a shell `&`
    is reparented to PID 1 the moment its shell exits, returns on the first
    completion, and is observed by nobody. On 2026-08-22 that exact shape
    dropped the thread three times in one day: three agents sat idle for
    seventeen minutes each time, with a wait that "existed" according to pgrep.
    So this walks /proc and counts only waits with a live `claude` ancestor.
    """
    return observed_pids("wait")


def has_claude_ancestor(pid, limit=32):
    """True when a `claude` process is somewhere above `pid`."""
    seen = 0
    while pid and pid != 1 and seen < limit:
        seen += 1
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                stat = fh.read()
            # comm is in parentheses and may contain spaces; ppid follows it.
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if comm == "claude":
            return True
        pid = ppid
    return False


def observed_wait_names():
    """Run names named on the command line of every observed wait."""
    names = set()
    for pid in observed_wait_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in argv if a]
        if "wait" not in args:
            continue
        found = wait_run_names(args[args.index("wait") + 1 :])
        names.update(found if found else {"*"})  # a bare `wait` covers every run
    return names


def wait_run_names(rest):
    """Run names from a `codex-fleet wait` argv tail, skipping flag values."""
    names, skip = set(), False
    for a in rest:
        if skip:
            skip = False
            continue
        if a in ("--timeout", "-t"):
            skip = True
            continue
        if a.startswith("--timeout="):
            continue
        if a.startswith("-"):
            continue
        names.add(a)
    return names


def unwatched(running):
    """Running agents that no OBSERVED `codex-fleet wait` covers.

    `wait` returns on the FIRST completion, by design: a fast agent's result
    must not sit invisible behind a slow one. The cost is that covering a
    fan-out means re-issuing the wait every time, and the orchestrator does not
    reliably remember. This check is on the push side so it cannot be skipped,
    and it counts only waits whose exit will wake someone: see
    observed_wait_pids for why a bare pgrep was not enough.
    """
    if not running:
        return []
    if events_monitor_armed():
        return []
    covered = observed_wait_names()
    if "*" in covered:
        return []
    return [n for n in running if n not in covered]


def main():
    # Two orchestrator sessions can run fleets at once; this hook must speak
    # only about the invoking session's runs. Claude passes session_id on
    # stdin for every hook event.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    owner = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    runs = survey(owner=owner)
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

    running = [n for n, r in runs.items() if r["status"] == "running"]

    # Throttled so it nags rather than chatters, and stored in the same state
    # file so it survives between hook fires.
    nag = []
    loose = unwatched(running)
    if loose:
        last = 0.0
        try:
            last = float(seen.get("_unwatched_at") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last > UNWATCHED_NAG_SECONDS:
            names = " ".join(sorted(loose))
            nag.append(
                f"{len(loose)} agent(s) running and nothing will wake you when they finish: {names}."
                f" Arm the fleet connection once for this session, then forget about it:"
                f' Monitor({{ command: "codex-fleet events", persistent: true, description: "codex fleet" }})'
            )
            seen["_unwatched_at"] = time.time()

    # Persisted AFTER the nag decides, so its throttle stamp survives. Writing
    # first would drop the stamp on every fire that also had news, and the nag
    # would then repeat on every tool call.
    persisted = {k: {"status": v["status"], "stall": v.get("stall", 0)} for k, v in runs.items()}
    if seen.get("_unwatched_at"):
        persisted["_unwatched_at"] = seen["_unwatched_at"]
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(persisted))
    except OSError:
        pass

    if not lines and not questions and not nag:
        return
    body = "Codex fleet update (pushed by a hook, you did not ask for it):"
    if questions:
        body += (
            "\n\nAN AGENT IS ASKING YOU SOMETHING. It is still working and will"
            " see your reply on its next tool call. Answer with:"
            ' codex-fleet tell <name> "<answer>"\n- ' + "\n- ".join(questions)
        )
    if nag:
        body += "\n\n- " + "\n- ".join(nag)
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
