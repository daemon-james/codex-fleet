# Maintaining codex-fleet

Read this before changing anything here. It is short, and the traps section
below is the part that will save you: each item is something that shipped
broken, or nearly did, in a single afternoon of building this.

## What this is, and is not

A development tool for running Codex agents in the background. **It is not part
of Mnemonik.** Do not commit any of it into the Mnemonik repository, do not put
its docs in Mnemonik's `docs/`, and do not add Mnemonik as a dependency. The
only connection is that agent briefs written for Mnemonik work include a
paragraph authorizing Mnemonik's MCP tools, and that paragraph is a template in
the skill, not code here.

## Where the pieces live

Three files, three destinations, all symlinked by `install.sh`:

| Repo file | Symlinked to | Read by |
| --- | --- | --- |
| `codex-fleet` | `~/.local/bin/codex-fleet` | you, and agents calling `codex-fleet ask` |
| `hooks/codex-fleet-inbox.py` | `~/.codex/hooks/codex-fleet-inbox.py` | Codex, on `PostToolUse` |
| `hooks/codex-fleet-status.py` | `~/.claude/hooks/codex-fleet-status.py` | Claude Code, on `PostToolUse` and `UserPromptSubmit` |

Symlinks, not copies, so an edit here is live immediately and there is no deploy
step to forget.

Registration is separate from installation and `install.sh` deliberately does
not do it, because both hosts gate it:

- **Codex** reads `~/.codex/hooks.json`. The inbox hook is a second group under
  `PostToolUse`, appended beside the Mnemonik hook rather than replacing it.
  Codex asks the owner to trust a new hook once, recording a `trusted_hash` in
  `~/.codex/config.toml` under a key like
  `hooks.state."~/.codex/hooks.json:post_tool_use:1:0"`. **Until they approve
  it, `codex-fleet tell` queues messages that are never delivered, silently.**
- **Claude Code** reads `~/.claude/settings.json`.

Runtime state lives in `~/.codex-fleet/`: `runs/<name>/` per run, and
`worktrees/<name>/` for `spawn -w`. Nothing there is precious except
`meta.json`, which holds the `thread_id` that keeps a run resumable.

## Changing it

```bash
# edit codex-fleet, then:
python3 -m unittest discover -s tests        # the suite
./install.sh --check                         # symlinks still right
codex-fleet list                             # smoke test

# edit dashboard.html, then ALWAYS:
tools/embed-page.py                          # never hand-edit the embedded copy
```

A running `codex-fleet serve` holds its code in memory. **Restart it after any
change** or you will test the old version and conclude your fix did not work:

```bash
kill $(pgrep -f "^python3 .*codex-fleet serve")
codex-fleet serve --port 8787 --host 0.0.0.0 &
```

**Anchor every `pgrep`/`pkill` pattern.** `pkill -f "codex-fleet serve"` matches
the shell running that very command and kills your own session. Use
`pgrep -f "^python3 .*codex-fleet serve"`. This is not a `serve` problem, it is
true of any pattern you match against a process list from inside a shell whose
command line contains the pattern. It has now cost three sessions, the third
being `pgrep -f "codex-fleet wait <names>"` written by someone who had already
documented the rule.

## Traps

**The dashboard HTML cannot be edited through a Python string.** It is one
triple-quoted string inside a single-file CLI. Editing it through a nested
heredoc mangled every quote in the JavaScript: 86 occurrences of
backslash-quote, invalid JS, in a file that still imported and passed
`ast.parse` cleanly. Python cannot catch it. That is why `dashboard.html` is the
source of truth and `tools/embed-page.py` is the only thing that writes the
string. The string is a raw literal (`r"""`), so the page's JavaScript may use
backslashes; the tool refuses a triple quote, a control character, or a
trailing backslash, and reads its own output back to confirm it survived.

**Sort keys must stop moving.** Sorting runs by last-event time reads correctly
and behaves terribly: a live agent rewrites its log every second, so two running
agents swap places continuously and the list jumps under the reader. Running
runs sort by START time, fixed for the life of the run. Everything else sorts by
last activity, fixed once the process exits. If you change ordering, sample it
repeatedly with agents live and prove it does not move.

**`read-only` costs an agent its memory and its voice.** Under `-s read-only`
Codex disables MCP approval, so `session_bootstrap` and `checkpoint` both fail,
and the agent cannot write anywhere, so `codex-fleet ask` fails too. A reviewer
once produced two criticals and could record neither. For review work use
`-s workspace-write --worktree`: the throwaway worktree is the isolation and
`-C` confines writes to it. Probed and confirmed: writes to the real repo and to
`$HOME` both return `Read-only file system`, while MCP and `ask` work.

**`workspace-write` needs `--approve-for-me`, not `-s`.** Under a plain `-s
workspace-write` every MCP tool call dies with "MCP tool call requires approval,
but approval policy is never". The tools are listed, the network is fine, and
nothing can approve the call in a non-interactive run. `--approve-for-me`
routes approvals through automatic review and refuses to combine with `-s`,
which is why `build_cmd` branches.

**Codex's own risk layer blocks sending file contents to an MCP server.** It
rejected `mnemonik.file_context({filePaths})` with "This action was rejected due
to unacceptable risk... would transmit potentially sensitive internal
source-file contents". The agent then worked blind for the rest of its turn and
mentioned it once. `memory_search`, `session_bootstrap` and `checkpoint` pass;
only file CONTENTS trigger it. The fix is an explicit authorization paragraph in
the brief, bounded to the repo so it stays truthful. The template is in the
skill.

**`say` cannot reach a running agent, and never will.** `codex exec` reads stdin
only for the initial prompt, runs the turn to completion, and exits. There is no
channel into a turn in flight. `tell` goes around it by writing
`runs/<name>/inbox.jsonl`, which the Codex `PostToolUse` hook delivers as
`additionalContext` on the agent's next tool call. `ask` is the same trick in
reverse, and works because `build_cmd` passes `--add-dir <run_dir>` so the
sandbox lets the agent write there.

**`codex exec resume` takes almost no flags, and fails loudly but late.** It
rejects `-s`, `-C` and `--add-dir`. `build_cmd` therefore branches: the spawn
path uses flags, the resume path passes the same settings through `-c`
(`sandbox_mode`, `sandbox_workspace_write.writable_roots`). Getting this wrong
is invisible until somebody calls `say`, and then the whole turn dies before the
model sees the prompt, with `unexpected argument '--add-dir' found` buried in
`turn-N.err`. **If you add anything to the spawn command, check the resume path
in the same edit.**

**`result` warns when agents are running unwatched, because `wait` returning
on the first completion means you have to re-issue it and you will forget.** It
fires on reading a result, which is always the step just before the mistake:
read, report, end the turn, and the remaining agents finish into silence. It
prints nothing when a live `wait` already names them. Do not remove it without
replacing the habit it stands in for.

**`wait` returns on the FIRST completion, and that default is deliberate.** The
orchestrator's only reliable wake-up is this command exiting. It used to block
until every named run finished, so waiting on a fast agent and a slow one
together hid the fast one's result behind the slow one. A test agent once sat
finished for minutes behind a longer engineer and the owner noticed before the
orchestrator did. `--all` restores the barrier for the rare case that wants it.
The status hook is only a safety net here: it fires on a tool call or a turn
boundary, so it cannot reach an orchestrator that has ended its turn and is
waiting on nothing else.

**Do not sandbox the agents tighter than the user's own config.** This machine's
`~/.codex/config.toml` sets `sandbox_mode = "danger-full-access"` and
`approval_policy = "never"`, and the models are frontier coders on the user's own
hardware. `codex-fleet` used to force `workspace-write` on every spawn and stamp
`-c sandbox_mode` over the user config on every resume. That single override
caused every wall hit on 2026-08-22: `git add` could not write the shared object
database in a worktree, `node net.listen` could not bind, and EVERY MCP call
failed with `MCP tool call requires approval, but approval policy is never`.

Three agents lost turns to it and the orchestrator lost an afternoon, twice
concluding the cause was something else. The default is now `danger-full-access`,
and a resume only narrows the sandbox when that run explicitly asked to be
narrowed. `-C` still confines the working directory, which is what makes a
worktree an isolated place to work; the sandbox on top of it bought nothing.

`read-only` and `workspace-write` remain, for when narrowing is a deliberate
choice, such as a reviewer that must not be able to edit what it reviews. Know
what each costs before choosing it: under `read-only` an agent cannot write to
its own run directory, so `codex-fleet ask` cannot reach you either.

**Read the event log, not the agent's prose, when a capability looks broken.**
The failures above were plain in `turn-*.jsonl` the whole time: ten resumed turns
each carrying `mcp_tool_call ... status: failed ... "requires approval, but
approval policy is never"`. The orchestrator instead believed an agent that
reported `TypeError: ... is not a function`, which was Code Mode failing on a
script the agent wrote rather than an MCP call it made, and spent hours on the
wrong diagnosis. An agent reporting a capability as absent is reporting what it
tried.

**A worktree is branched at spawn and never moves on its own.** Resume a
reviewer three commits later and it reads the files it was born with while being
asked about a commit that is not in them. It then reports what it sees, which
looks like a confused review and is actually correct reporting of a tree nobody
advanced. `say` now merges the repo's current branch into the worktree first,
and prints what it did. It refuses to touch a worktree holding uncommitted work
or one that cannot merge cleanly, because discarding an agent's work to tidy a
branch is worse than a stale read.

**A run's env var is set at spawn and cannot be retrofitted.** `CODEX_FLEET_RUN`
is the only thing that tells the inbox hook which mailbox to read. An agent
started before that wiring existed can never receive a `tell`. If you add
another per-run channel, set it in `launch()` at the same time.

**Every reader must survive a pruned run.** Pruning deletes the event log and
keeps `meta.json` plus `result.txt`. Four separate readers broke on that,
one at a time, after it shipped: `status_of` returned "stopped", `last_message`
returned empty, `usage_of` returned zero, and `age`/`mtime` returned nothing so
the run sank to the bottom forever. Each now takes an optional `meta` and falls
back to a recorded value. **If you add a reader that touches `turn-*.jsonl`,
give it the same fallback and a test.**

**Agents report sandbox failures as test failures.** `listen EPERM` on
`127.0.0.1`, `spawnSync` on git or bash, abstract Unix sockets, and database
permission errors are all sandbox artefacts, not broken code. Re-run those
suites yourself outside the sandbox before believing a red report. Tell agents
in the brief to name which failures they believe are artefacts rather than
chasing them.

**A green report is not a green tree.** An agent reported 73 passing tests
truthfully, then reverted generated build output to tidy its diff, which broke
the very tests it had watched pass. "I ran it and it passed" and "it passes now"
are different claims. Re-run the suite yourself in the tree as the agent left
it.

## Design decisions worth not relitigating

**One file for the CLI.** It lives on `PATH` and agents invoke it. A package
with imports would need installing inside every sandbox. The dashboard is the
only part big enough to hurt, and `embed-page.py` handles that.

**Two roles, two efforts, no `--model` flag.** `engineer` is `gpt-5.6-sol`,
`reviewer` is `gpt-5.6-terra`. Constraining it stops the orchestrator from
fiddling with model choice per task and stops a reviewer being handed build
work.

**Prune collapses, it does not delete.** 99.4% of a run's disk is its event log.
The `thread_id` is what makes a finished agent worth keeping, because `say`
resumes a warm context for one turn instead of paying a fresh agent to re-read
everything. So old runs lose their transcript and keep their conclusion. `rm` is
the only irreversible operation here, and it exists for closed workstreams.

**Auto-prune runs on `spawn`.** That is the moment new disk is allocated, and a
workflow you have to remember is not a workflow. It is silent unless it frees
something, skips running agents and live worktrees, and `CODEX_FLEET_PRUNE_DAYS=0`
turns it off.

**Hooks fail open, always.** Both exit 0 with no output on any error. An
observability hook that can break a tool call is worse than no hook. Keep the
bare `except` at the bottom of each one.

**The in-flight token figure is an estimate, and the `~` says so.** Codex reports
usage only on `turn.completed`, so a running agent used to read as zero output
tokens. That is backwards: a long fan-out looks free at exactly the moment it is
spending the most. `inflight_output_estimate` prices the reasoning items that
have arrived since the last completed turn at `TOKENS_PER_REASONING_ITEM`, and
`render_output_tokens` prefixes the total with `~`. Keep the tilde. A bare number
would be a claim the event log cannot support, and the figure exists to help
decide whether to let a fan-out keep running.

### Recalibrating the in-flight estimate

`TOKENS_PER_REASONING_ITEM = 480` was measured on 2026-08-22 across 52 completed
turns: 326 to 600 per item, median 480, and both models agreed (sol 484, terra
478). Redo it when the models change:

```bash
python3 - <<'PY'
import json, glob, statistics
rows = []
for p in glob.glob('/home/dev/.codex-fleet/runs/*/turn-*.jsonl'):
    items, exact = 0, None
    for line in open(p, errors='ignore'):
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get('type') == 'turn.completed':
            exact = (ev.get('usage') or {}).get('output_tokens')
        elif ev.get('type') == 'item.completed' and (ev.get('item') or {}).get('type') == 'reasoning':
            items += 1
    if exact and items:
        rows.append(exact / items)
print(f"n={len(rows)} median={statistics.median(rows):.0f} min={min(rows):.0f} max={max(rows):.0f}")
PY
```

Two predictors were tried and rejected, so do not reach for them again. Visible
text is not usable: reasoning arrives summarised, and the text-to-token ratio
over the same 52 turns spanned 2.3x to 8.8x. Command count is not usable either,
at 2.8x across its middle half against 1.2x for reasoning items.

**Reading a question is not answering it.** An agent that ran `codex-fleet ask
--blocking` has stopped and is waiting. A NON-blocking question is delivered once,
because repeating an FYI is noise. A blocking one repeats on every hook fire and
keeps showing in `inbox` until `codex-fleet tell` answers it, and `list` renders
that agent as `ASKING` rather than `running`. Only `tell` clears it, because only
`tell` reaches the agent.

This cost a real incident on 2026-08-22. The hook drained a blocking question and
marked it read as a side effect of looking, its own output never reached the
orchestrator, and `codex-fleet inbox` then printed "nothing raised" while the
agent slept in 30-second polls for twenty minutes and the owner waited on both of
us. A surface that reports "nothing" when it means "I already looked once" is
worse than one that repeats itself.

**The unwatched nag is on the push side, and that is the point.** `wait` returns
on the FIRST completion by design, so covering a fan-out means re-issuing it
every time. `codex-fleet result` has warned about this since 2026-08-21 and it
did not help, because a warning you only see when you go looking is invisible to
the habit that fails: read a result, report it, end the turn. The status hook now
runs the same check and pushes it, throttled to once per `UNWATCHED_NAG_SECONDS`.
Keep it on the hook. Moving it back to a command reintroduces the bug.

**A worktree agent needs the main repo's git dir, or it cannot commit.** A
linked worktree's `.git` is a FILE holding `gitdir: <path>`, and that path lives
under the MAIN repo, outside the `-C` the sandbox grants. Without
`linked_worktree_gitdir` in the writable roots, `git add` cannot create
`index.lock` and the agent hits a read-only filesystem on a path it never chose
and cannot diagnose. Three agents lost turns to this on 2026-08-22, each burning
a blocking question and a hand-commit by the orchestrator. It applies to both the
`--add-dir` path on a fresh spawn and the `writable_roots` config on a resume,
because `codex exec resume` accepts neither `-C` nor `--add-dir`.

An ordinary checkout returns `None` here: its `.git` is a directory already
inside `-C`, and widening the sandbox on a guess would be worse than the bug.

**Give agents the real Mnemonik call shape, not Code Mode syntax.** Writing
`mnemonik.checkpoint({...})` in a brief is wrong. That syntax only works INSIDE
the tool, so an agent runs it as raw JavaScript and gets
`TypeError: Cannot read properties of undefined`. Two agents on 2026-08-22 then
reported that Mnemonik was unreachable and that the connector needed approval,
and the orchestrator spent a chunk of the afternoon chasing a block that did not
exist. Probes from both a worktree and the main repo showed
`mnemonik__memory_discover`, `mnemonik__memory_tools` and
`mnemonik__session_bootstrap` all visible and the call succeeding.

The real call is the MCP tool on server `metamcp`, named
`mnemonik__memory_tools`, with one `code` argument holding an async arrow
function that returns a `mnemonik.*` call.

An agent reporting a capability as absent is reporting what it tried, not what
exists. Probe before believing it.
