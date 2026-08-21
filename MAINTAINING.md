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

**`wait` returns on the FIRST completion, and that default is deliberate.** The
orchestrator's only reliable wake-up is this command exiting. It used to block
until every named run finished, so waiting on a fast agent and a slow one
together hid the fast one's result behind the slow one. A test agent once sat
finished for minutes behind a longer engineer and the owner noticed before the
orchestrator did. `--all` restores the barrier for the rare case that wants it.
The status hook is only a safety net here: it fires on a tool call or a turn
boundary, so it cannot reach an orchestrator that has ended its turn and is
waiting on nothing else.

**A resumed agent loses its Mnemonik.** `codex exec resume` rejects
`--approve-for-me`, so it falls back to the user's `approval_policy` (`never`)
and every MCP call dies with "MCP tool call requires approval, but approval
policy is never". Proven by running one agent twice: it answered "Succeeded."
on its spawn turn and hit that error on the resume turn.

`approval_policy="granular"` is NOT the fix. It parses on spawn but resume
rejects it with "invalid type: unit variant, expected newtype variant", meaning
it wants a value rather than a bare string. That was tried and reverted rather
than shipped broken; do not re-try it without testing an actual resume.

Until the right key is found, tell a resumed agent to put findings in its final
message and checkpoint on its behalf. The turn still works, it just cannot
remember anything itself.

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
