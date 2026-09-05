---
name: codex-fleet
description: Run Codex agents as background subagents you can fan out, watch live, and talk to. Use when work should be delegated to one or more Codex agents — parallel investigation across subsystems, an independent code review of your own changes, a long build task you want to keep watching, or a second opinion from a different model family. Also use when the user asks to spawn, watch, resume, or check on Codex agents.
---

# codex-fleet

`codex-fleet` wraps `codex exec --json` so Codex agents run in the background with
a durable event stream, stable names, and resumable sessions. It is installed at
`~/.local/bin/codex-fleet`. Run state lives in `~/.codex-fleet/runs/<name>/`.

Use it when work should leave your context: parallel investigation, an
independent review of code you just wrote, or a long task you want to watch
rather than block on.

## Choose the model, then the effort

Choose the least costly role that can reliably complete the task. Use the
rules below before every `spawn` and `say`. The role selects the model; there is
no `--model` flag. Pass `-r` explicitly when spawning.

| Role | Model | Available efforts | Default |
| --- | --- | --- | --- |
| Engineer (`engineer`) | Sol (`gpt-5.6-sol`) | High, Extra High | High |
| Reviewer (`reviewer`) | Terra (`gpt-5.6-terra`) | High, Extra High | High |
| Advanced Engineer (`advanced-engineer`) | Astra (`gpt-6-astra`) | Medium, High, Extra High | High |

The table describes the bundled defaults. Check the installed `ROLES` and
`EFFORTS` if the owner has adapted them to their account. Do not silently
change models or account configuration.

### 1. Choose the role

Apply these rules in order:

1. **Independent review or verification of a change: Terra.** This includes
   changes written by Astra and checking affected callers elsewhere in the
   system. Give the reviewer the intended behavior and exact change to review.
   Require concrete failure scenarios and file references for findings.
2. **Engineering that requires unresolved causal, architectural or compatibility
   reasoning: Astra.** Identify the specific uncertainty or decision in the
   brief. Select Astra directly when that need is already established; do not
   pay for a token Sol attempt first.
3. **Other implementation, code tracing and test work: Sol.** Use it when the
   expected behavior is clear and the work can be bounded. This is the default
   engineering choice.

| Task | Choice |
| --- | --- |
| Investigating a bug whose cause remains unclear after basic inspection | Astra |
| Refactoring across module boundaries with changes to responsibilities or contracts | Astra |
| Changing architecture or deciding between designs with competing constraints | Astra |
| Database migrations or implementation requiring compatibility across versions or consumers | Astra |
| Independently reviewing correctness, regressions or effects elsewhere in the system | Terra |
| Investigating a specific unresolved architectural or compatibility question raised by review | Astra, as a separate investigation |
| Implementing a clearly specified, isolated change or a fix with an established cause | Sol |
| Repetitive edits, mechanical renames across files, or focused tests for understood behavior | Sol |
| Locating callers, tracing an existing path, or checking a documented configuration | Sol, if delegation is warranted |

File count, prompt length, a long runtime and labels such as "complex" do not
justify Astra on their own. Distinguish a mechanical edit across many files
from a change to how modules interact. Do basic lookups directly when one
search or command answers the question.

Terra remains the reviewer when a change spans the system. Add an Astra
investigation only for a named question that the review cannot resolve; do not
routinely run two reviews. Neither model may independently review work produced
by that same agent. Do not give implementation work to `reviewer`.

### 2. Choose the effort

**Start at High for every role.** The role and effort are separate decisions:
Astra does not automatically mean Extra High.

| Effort | Use when |
| --- | --- |
| High (`high`) | Default for implementation, investigation and review, including Astra work. |
| Extra High (`xhigh`) | The brief identifies an especially difficult reasoning problem: competing explanations surviving investigation, subtle concurrency or compatibility interactions, or critical invariants requiring deeper verification. A High run that remains blocked on reasoning can also justify it. |
| Medium (`medium`) | A short, bounded follow-up in an existing Astra investigation that still depends on its accumulated context, such as extracting the established constraints or explaining an already reached conclusion. |

Do not increase effort for repetitive work, missing access, broken tooling,
slow builds or missing user requirements. Address the actual blocker. Do not
start Astra at Medium for ordinary work that belongs with Sol. Low, Max and
Ultra are unavailable.

### 3. Reassess at a handoff or follow-up

If Sol discovers unresolved causes, design choices across modules or new
compatibility requirements, hand that investigation to Astra with the evidence,
files changed and checks already run. Finish or pause the original assignment
before another agent edits the same files. Increasing Sol to Extra High is not
a substitute for selecting Astra when the task now meets the Astra criteria.

Once Astra establishes the cause or design, assign substantial, clearly
specified remaining implementation to Sol. Let Astra finish a small remaining
step when transferring the context would cost more than completing it.

Reuse a warm agent only when its role still fits. `say` preserves the model and
previous effort: it cannot turn an Astra run into Sol. Pass `-e high` when a
follow-up no longer needs Extra High, or `-e medium` for the bounded Astra
follow-up described above. A new, unrelated routine task belongs with Sol even
when an idle Astra agent is available.

Include one short model/effort reason in the task brief, for example:
"Sol / High: specified validation change with known expected behavior" or
"Astra / High: determine why old and new clients disagree on migration state."
Make this choice without asking the user to select a model for each task.

## Start and supervise a fleet

Check `codex-fleet list` for existing runs before spawning. Reuse a relevant
idle agent with `say`; do a simple lookup yourself when delegation adds no value.

Keep completion notifications connected to this Claude session. If the host
provides a persistent Monitor tool, arm `codex-fleet events` once through it.
Otherwise use Claude Code's Bash tool with `run_in_background: true` to run
`codex-fleet wait <names> --timeout 60` after spawning. On each completion or
timeout, collect finished results and re-arm the wait for remaining agents.
A shell `&` alone does not arrange a completion notification for Claude.
Do not assume a Monitor tool exists on every installation.

The status hook reports transitions and questions on tool calls and prompt
submission. The Stop hook catches unobserved work and unread results. These
hooks supplement the notification channel; read and act on their messages.
Use `list` and `tail` for quick inspection while doing independent work.

```bash
codex-fleet spawn "Trace request validation. Report file:line. Do not edit files." \
  -n scout -r engineer -e high -C /path/to/repo
codex-fleet list
codex-fleet tail scout -n 15
codex-fleet result scout
```

`spawn` returns once it finds the thread ID or its startup wait expires. That
is not proof of success: inspect failures and missing thread IDs in the log.
`wait` returns on the first finished run, so collect it and keep supervising the
rest. Run blocking `wait` and `watch` through the host's background task facility.

## Brief agents and separate writers

Give each agent the intended outcome, relevant files, ownership boundaries,
contract-derived checks, and what to report. Permit a finding that no change
is needed. Name decisions that require escalation instead of speculative work.
An agent inherits the process environment and Codex configuration; Fleet does
not install or authorize external services for it.

Concurrent writers need separate `spawn --worktree` checkouts. Tell each writer
to commit its changes on its Fleet branch before finishing so you can inspect
and merge or cherry-pick the result. Keep your own uncommitted changes out of
their ownership. Do not edit a brief or file an agent is actively relying on.

```bash
codex-fleet spawn "Implement the agreed validation fix. Own only src/validation.py \
and its tests. Commit the change and report checks run." \
  -n validation -r engineer -e high -C /path/to/repo --worktree
```

A new worktree starts from committed history. It does not contain the main
checkout's uncommitted diff. To review uncommitted changes, point the reviewer
at that checkout with an explicit no-edit brief; to review a committed change,
give a separate reviewer the exact commit and base. Worktrees separate Git
changes, not filesystem permissions: Fleet currently forces full access.

Worktrees may share symlinked dependencies and receive copied environment
files. Inspect before running commands that modify shared dependencies.
`say` updates an existing worktree from its source checkout's current branch
and refuses dirty or conflicting worktrees; account for this when resuming a
review of a specific commit.

## Steer and answer questions

```bash
codex-fleet tell scout "Focus on the API handler, not the CLI."
codex-fleet inbox --peek
codex-fleet inbox --all
codex-fleet say scout "Explain the callers affected by your findings."
```

`tell` queues a message for the inbox hook to deliver on a subsequent tool
call. It works during a running turn; `say` starts a new turn only after the
agent is idle. Reading a blocking question does not answer it. Use `tell` to
answer; if the agent has already ended its turn, use `say` to resume it.
If delivery fails, inspect hook registration and trust before repeating messages.
The inbox hook requires the default `~/.codex-fleet` state directory.

Include this in briefs when agents need a decision channel:

> If a decision is missing, run `codex-fleet ask "your question"` and continue
> independent work. Add `--blocking` if you cannot finish without the answer.
> A reply can arrive through the inbox hook on a subsequent tool call. If no
> independent work remains, report the blocker and end the turn rather than
> sleeping indefinitely; the orchestrator can resume you.

## Dashboard

Check `codex-fleet serve --status` before starting a server. For a new local
instance use `codex-fleet serve --daemon --host 127.0.0.1` and give the user
`http://127.0.0.1:8787`. Reuse an existing server. For a remote machine, use
an SSH tunnel or an access method the owner has chosen. The dashboard has no
authentication and contains prompts and logs; do not assume LAN exposure is wanted.

## Collect, verify and reuse

Read `codex-fleet result <name>` for each completed turn; `--all` searches older
turns and must not disguise a failed follow-up. Inspect the actual diff and run
checks against the tree as the agent left it. Review tests against the requested
contract, including failure scenarios, rather than only their implementation.
If an MCP call or tool fails, inspect the recorded event and stderr before
accepting the agent's explanation. Spawn and resume can behave differently.

Use an independent reviewer for substantive changes. Send actionable findings
back to their author with `say` when idle, or `tell` when running. Escalate a
new unresolved design question according to the role rules above.

Idle runs retain resumable thread IDs without a running agent process. Keep
relevant context for follow-ups. `rm` deletes run state; `gc` applies retention
and may remove landed worktrees. Do not run destructive cleanup merely to tidy
the list. Use `gc --dry-run` to inspect its effects.

## Command reference

```bash
codex-fleet spawn "<brief>" -n <name> -r <role> -e high -C <repo> [-w]
codex-fleet list
codex-fleet tail <name> [-n 30] [-v] [--all]
codex-fleet result <name> [--all]
codex-fleet tell <name> "<live message>"
codex-fleet say <name> "<follow-up>" [-e high|xhigh]
codex-fleet ask "<question>" [--blocking]
codex-fleet inbox [names...] [--peek] [--all]
codex-fleet events
codex-fleet wait [names...] [--timeout 60]
codex-fleet watch <name> [-v]
codex-fleet serve --status
codex-fleet kill <name>
codex-fleet gc --dry-run
```

Advanced Engineer also accepts `-e medium`. `codex-fleet --help` and each
subcommand's `--help` describe the installed version.
