import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader(
    "codex_fleet",
    importlib.machinery.SourceFileLoader("codex_fleet", str(ROOT / "codex-fleet")),
)
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)

# The status hook is the PUSH side of the loop and carries its own copy of the
# outbox rules, so it gets tested rather than trusted.
hook_spec = importlib.util.spec_from_loader(
    "codex_fleet_status",
    importlib.machinery.SourceFileLoader(
        "codex_fleet_status", str(ROOT / "hooks" / "codex-fleet-status.py")
    ),
)
hook = importlib.util.module_from_spec(hook_spec)
hook_spec.loader.exec_module(hook)


class CodexFleetTests(unittest.TestCase):
    def setUp(self):
        self._old_root = cf.ROOT
        self._old_runs = cf.RUNS
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.fleet_home = self.temp_path / "fleet-home"
        self.runs = self.fleet_home / "runs"
        self.runs.mkdir(parents=True)
        self.home = self.temp_path / "home"
        self.home.mkdir()
        cf.ROOT = self.fleet_home
        cf.RUNS = self.runs
        self.clock = time.time()
        self.addCleanup(self._restore_fleet_paths)

    def _restore_fleet_paths(self):
        cf.ROOT = self._old_root
        cf.RUNS = self._old_runs
        self._temp.cleanup()

    def test_role_selection_reaches_codex_with_requested_or_default_effort(self):
        cases = [
            ([], "engineer", "gpt-5.6-sol", "high"),
            (["-r", "reviewer"], "reviewer", "gpt-5.6-terra", "high"),
            (["-r", "advanced-engineer"], "advanced-engineer", "gpt-6-astra", "high"),
            (["-r", "advanced-engineer", "-e", "medium"], "advanced-engineer", "gpt-6-astra", "medium"),
            (["-r", "advanced-engineer", "-e", "xhigh"], "advanced-engineer", "gpt-6-astra", "xhigh"),
        ]
        for i, (flags, role, model, effort) in enumerate(cases):
            with self.subTest(flags=flags):
                name = f"selection-{i}"
                def launch(run_name, meta, prompt):
                    meta["pid"] = 999999
                    cf.save_meta(run_name, meta)
                    return 999999, self.runs / run_name / "turn-1.jsonl"
                argv = ["codex-fleet", "spawn", "task", "-n", name, "-C", str(self.temp_path), "--no-prune", *flags]
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(cf.shutil, "which", return_value="codex"), \
                     mock.patch.object(cf, "launch", side_effect=launch), \
                     mock.patch.object(cf, "await_thread_id", return_value="thread-test"), \
                     contextlib.redirect_stdout(io.StringIO()):
                    cf.main()
                meta = cf.load_meta(name)
                self.assertEqual((meta["role"], meta["model"], meta["effort"]), (role, model, effort))
                for resume_id in (None, "thread-test"):
                    cmd = cf.build_cmd(meta, "task", resume_id=resume_id)
                    self.assertEqual(cmd[cmd.index("-m") + 1], model)
                    self.assertIn(f"model_reasoning_effort={effort}", cmd)

    def test_invalid_role_efforts_do_not_replace_an_existing_run(self):
        run, _ = self._make_run("keep")
        before = (run / "meta.json").read_bytes()
        for role, effort in [("engineer", "medium"), ("reviewer", "medium"),
                             ("advanced-engineer", "low"), ("advanced-engineer", "max"),
                             ("advanced-engineer", "ultra")]:
            with self.subTest(role=role, effort=effort), \
                 mock.patch.object(sys, "argv", ["codex-fleet", "spawn", "task", "-n", "keep", "--force", "-r", role, "-e", effort]), \
                 mock.patch.object(cf, "launch") as launch, \
                 contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as raised:
                cf.main()
            self.assertEqual(raised.exception.code, 2)
            launch.assert_not_called()
            self.assertEqual((run / "meta.json").read_bytes(), before)

    def test_astra_resume_keeps_or_changes_effort(self):
        for effort in (None, "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                name = f"resume-{effort}"
                self._make_run(name, meta_updates={"role": "advanced-engineer", "model": "gpt-6-astra", "effort": "xhigh"})
                flags = ["-e", effort] if effort else []
                with mock.patch.object(sys, "argv", ["codex-fleet", "say", name, "continue", *flags]), \
                     mock.patch.object(cf, "refresh_worktree", return_value=None), \
                     mock.patch.object(cf, "launch", return_value=(999999, None)) as launch, \
                     contextlib.redirect_stdout(io.StringIO()):
                    cf.main()
                meta = launch.call_args.args[1]
                self.assertEqual(meta["model"], "gpt-6-astra")
                self.assertEqual(meta["effort"], effort or "xhigh")
                self.assertEqual(launch.call_args.kwargs["resume_id"], f"thread-{name}")

    def test_resume_rejects_effort_outside_saved_role(self):
        run, _ = self._make_run("engineer")
        before = (run / "meta.json").read_bytes()
        with mock.patch.object(sys, "argv", ["codex-fleet", "say", "engineer", "continue", "-e", "medium"]), \
             mock.patch.object(cf, "launch") as launch, \
             mock.patch.object(cf, "refresh_worktree") as refresh, \
             contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as raised:
            cf.main()
        self.assertEqual(raised.exception.code, 2)
        launch.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual((run / "meta.json").read_bytes(), before)

    def test_models_reports_each_roles_efforts_and_default(self):
        catalog = self.home / ".codex" / "models_cache.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"models": [
            {"slug": model, "supported_reasoning_levels": [{"effort": e} for e in cf.EFFORTS[role]]}
            for role, model in cf.ROLES.items()
        ]}))
        output = io.StringIO()
        with mock.patch.object(cf.Path, "home", return_value=self.home), contextlib.redirect_stdout(output):
            cf.cmd_models(argparse.Namespace())
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        astra = next(line for line in lines if line.startswith("advanced-engineer"))
        self.assertIn("gpt-6-astra", astra)
        self.assertIn("efforts=medium,high,xhigh", astra)
        for line in lines:
            self.assertIn("default=high", line)
            self.assertTrue(line.endswith("ok"))

    def test_weekly_allowance_uses_duration_and_main_bucket(self):
        weekly = {"usedPercent": 56, "windowDurationMins": 10080}
        short = {"usedPercent": 12, "windowDurationMins": 300}
        for primary, secondary in [(weekly, short), (short, weekly)]:
            with self.subTest(primary=primary):
                result = {"rateLimits": {"limitId": "codex", "primary": short},
                          "rateLimitsByLimitId": {
                              "codex": {"limitId": "codex", "primary": primary, "secondary": secondary},
                              "spark": {"limitId": "spark", "primary": {"usedPercent": 0, "windowDurationMins": 10080}}}}
                self.assertEqual(cf.weekly_allowance(result), 44)
        self.assertIsNone(cf.weekly_allowance({"rateLimits": {"primary": short}}))
        self.assertIsNone(cf.weekly_allowance({"rateLimits": {"limitId": "spark", "primary": weekly}}))
        self.assertEqual(cf.weekly_allowance({"rateLimits": {"primary": weekly}}), 44)
        self.assertEqual(cf.weekly_allowance({"rateLimits": {"primary": dict(weekly, usedPercent=100)}}), 0)
        self.assertIsNone(cf.weekly_allowance({}))

    def test_allowance_cache_refreshes_once_a_minute_and_marks_failed_reads(self):
        result = {"rateLimits": {"primary": {"usedPercent": 56, "windowDurationMins": 10080}}}
        reader = cf.SubscriptionAllowance()
        with mock.patch.object(cf, "read_subscription_limits", return_value=result) as read, \
             mock.patch.object(cf.time, "monotonic", return_value=100) as clock:
            first = reader.read()
            self.assertEqual(first["remaining"], 44)
            self.assertFalse(first["stale"])
            self.assertEqual(reader.read(), first)
            read.assert_called_once()
            clock.return_value = 161
            read.side_effect = TimeoutError()
            stale = reader.read()
            self.assertEqual(stale["remaining"], 44)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["updated_at"], first["updated_at"])
            reader.read()
            self.assertEqual(read.call_count, 2)

    def test_allowance_first_failure_is_unknown_not_zero(self):
        with mock.patch.object(cf, "read_subscription_limits", side_effect=OSError()):
            snapshot = cf.SubscriptionAllowance().read()
        self.assertIsNone(snapshot["remaining"])
        self.assertTrue(snapshot["stale"])

    def test_subscription_reader_initializes_then_reads_limits_and_exits(self):
        script = '''import json,sys,time
init=json.loads(sys.stdin.readline())
assert init['method']=='initialize'
print(json.dumps({'id':1,'result':{}}),flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
read=json.loads(sys.stdin.readline())
assert read['method']=='account/rateLimits/read'
print(json.dumps({'method':'notification','params':{}}),flush=True)
print(json.dumps({'id':2,'result':{'rateLimits':{'primary':{'usedPercent':56,'windowDurationMins':10080}}}}),flush=True)
time.sleep(30)
'''
        popen = subprocess.Popen
        children = []
        def fake_server(command, **kwargs):
            self.assertEqual(command, ["codex", "app-server", "--stdio"])
            proc = popen([sys.executable, "-u", "-c", script], **kwargs)
            children.append(proc)
            return proc
        with mock.patch.object(cf.subprocess, "Popen", side_effect=fake_server):
            self.assertEqual(cf.weekly_allowance(cf.read_subscription_limits()), 44)
        self.assertIsNotNone(children[0].poll())

    def test_subscription_reader_timeout_reaps_its_process(self):
        popen = subprocess.Popen
        children = []
        def fake_server(command, **kwargs):
            proc = popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
            children.append(proc)
            return proc
        with mock.patch.object(cf.subprocess, "Popen", side_effect=fake_server), self.assertRaises(TimeoutError):
            cf.read_subscription_limits(timeout=.05)
        self.assertIsNotNone(children[0].poll())

    def _write_jsonl(self, path, events):
        text = "".join(json.dumps(event) + "\n" for event in events)
        path.write_text(text, encoding="utf-8")

    def _write_meta(self, name, meta):
        path = self.runs / name / "meta.json"
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _make_run(
        self,
        name,
        *,
        pid=None,
        created_at=None,
        turns=None,
        log_mtimes=None,
        meta_updates=None,
    ):
        run = self.runs / name
        run.mkdir()
        turns = turns if turns is not None else [[]]
        meta = {
            "created_at": created_at
            or datetime.fromtimestamp(self.clock, timezone.utc).isoformat(),
            "pid": pid,
            "turns": len(turns),
            "thread_id": f"thread-{name}",
            "role": "engineer",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "sandbox": "workspace-write",
            "cwd": str(self.temp_path),
        }
        if meta_updates:
            meta.update(meta_updates)
        self._write_meta(name, meta)
        for index, events in enumerate(turns, 1):
            path = run / f"turn-{index}.jsonl"
            self._write_jsonl(path, events)
            if log_mtimes:
                os.utime(path, (log_mtimes[index - 1], log_mtimes[index - 1]))
        return run, meta

    def _make_prunable_run(self, name="old-run", *, final_status="idle", meta_updates=None):
        first = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                },
            },
        ]
        second = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "final answer"},
            }
        ]
        if final_status == "failed":
            second.append({"type": "turn.failed", "error": {"message": "failed"}})
        else:
            second.append(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                    },
                }
            )
        last_activity = self.clock - 10 * 86400
        run, meta = self._make_run(
            name,
            turns=[first, second],
            log_mtimes=[last_activity - 3, last_activity],
            meta_updates=meta_updates,
        )
        for index, stamp in ((1, last_activity - 2), (2, last_activity - 1)):
            err = run / f"turn-{index}.err"
            err.write_text(f"stderr {index}\n", encoding="utf-8")
            os.utime(err, (stamp, stamp))
        return run, meta, last_activity

    def _prune(self, *, older_than=3, dry_run=False):
        args = argparse.Namespace(older_than=older_than, dry_run=dry_run)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cf.cmd_prune(args)
        return output.getvalue()

    def _snapshot(self, root):
        snapshot = {}
        for path in [root, *sorted(root.rglob("*"))]:
            info = path.stat()
            body = path.read_bytes() if path.is_file() else None
            snapshot[str(path.relative_to(root))] = (
                stat.S_IMODE(info.st_mode),
                info.st_mtime_ns,
                body,
            )
        return snapshot

    def _hook_env(self, run_name=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CODEX_FLEET_HOME"] = str(self.fleet_home)
        env.pop("CODEX_FLEET_RUN", None)
        env.pop("CLAUDE_HOOK_EVENT_NAME", None)
        # Ownership identity must come only from the stdin payload a test
        # chooses to send, not from whichever session runs the suite.
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CODEX_FLEET_OWNER", None)
        if run_name is not None:
            env["CODEX_FLEET_RUN"] = run_name
        return env

    def _run_hook(self, script, stdin="{}", *, run_name=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "hooks" / script)],
            input=stdin,
            text=True,
            capture_output=True,
            cwd=ROOT,
            env=self._hook_env(run_name),
            timeout=3,
            check=False,
        )

    def _inbox_path(self, name):
        run = self.home / ".codex-fleet" / "runs" / name
        run.mkdir(parents=True)
        return run / "inbox.jsonl"

    def test_live_runs_stay_above_finished_runs_without_swapping_as_logs_change(self):
        live_pid = os.getpid()
        newer_start = datetime.fromtimestamp(self.clock - 10, timezone.utc).isoformat()
        older_start = datetime.fromtimestamp(self.clock - 20, timezone.utc).isoformat()
        _, _ = self._make_run(
            "live-newer",
            pid=live_pid,
            created_at=newer_start,
            log_mtimes=[self.clock - 100],
        )
        old_run, _ = self._make_run(
            "live-older",
            pid=live_pid,
            created_at=older_start,
            log_mtimes=[self.clock - 200],
        )
        self._make_run("finished-newer", log_mtimes=[self.clock - 5])
        self._make_run("finished-older", log_mtimes=[self.clock - 50])

        expected = ["live-newer", "live-older", "finished-newer", "finished-older"]
        self.assertEqual([name for name, _ in cf.all_runs()], expected)
        for offset in range(1, 5):
            stamp = self.clock + offset
            os.utime(old_run / "turn-1.jsonl", (stamp, stamp))
            self.assertEqual([name for name, _ in cf.all_runs()], expected)

    def test_pruning_keeps_the_conclusion_and_resume_metadata_but_removes_all_turn_files(self):
        run, original_meta, last_activity = self._make_prunable_run()

        output = self._prune()

        self.assertIn("old-run: pruned", output)
        self.assertEqual({path.name for path in run.iterdir()}, {"meta.json", "result.txt"})
        self.assertEqual((run / "result.txt").read_text(encoding="utf-8"), "final answer")
        meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["thread_id"], original_meta["thread_id"])
        self.assertEqual(meta["final_status"], "idle")
        self.assertEqual(
            meta["final_usage"],
            {"input_tokens": 11, "cached_input_tokens": 22, "output_tokens": 33},
        )
        self.assertEqual(meta["final_mcp"], {"ok": 0, "failed": 0})
        self.assertEqual(cf.mcp_text([], meta), "mcp=0/0")
        self.assertAlmostEqual(meta["last_activity"], last_activity, places=3)
        datetime.fromisoformat(meta["pruned_at"])

    def test_pruning_skips_a_run_whose_pid_is_alive(self):
        run, _, _ = self._make_prunable_run(meta_updates={"pid": os.getpid()})
        before = self._snapshot(run)

        self._prune()

        self.assertEqual(self._snapshot(run), before)
        self.assertTrue((run / "turn-2.jsonl").exists())

    def test_pruning_skips_a_run_whose_worktree_still_exists(self):
        worktree = self.temp_path / "active-worktree"
        worktree.mkdir()
        run, _, _ = self._make_prunable_run(meta_updates={"worktree": str(worktree)})
        before = self._snapshot(run)

        output = self._prune()

        self.assertIn("skipped, worktree still on disk", output)
        self.assertEqual(self._snapshot(run), before)
        self.assertTrue((run / "turn-2.jsonl").exists())

    def test_prune_dry_run_changes_no_files_or_metadata(self):
        run, _, _ = self._make_prunable_run()
        before = self._snapshot(run)

        output = self._prune(dry_run=True)

        self.assertIn("would free", output)
        self.assertEqual(self._snapshot(run), before)
        self.assertFalse((run / "result.txt").exists())

    def test_pruned_status_uses_the_recorded_failure_instead_of_stopped(self):
        _, meta, _ = self._make_prunable_run(final_status="failed")
        self.assertEqual(cf.status_of("old-run", meta), "failed")

        self._prune()

        pruned_meta = cf.load_meta("old-run")
        self.assertEqual(pruned_meta["final_status"], "failed")
        self.assertEqual(cf.status_of("old-run", pruned_meta), "failed")

    def test_final_message_reads_events_before_pruning_and_result_file_afterward(self):
        _, meta, _ = self._make_prunable_run()
        events = cf.all_events("old-run", meta)
        self.assertEqual(cf.last_message(events), "final answer")
        self.assertEqual(cf.last_message(events, "old-run"), "final answer")

        self._prune()

        pruned_meta = cf.load_meta("old-run")
        self.assertEqual(cf.all_events("old-run", pruned_meta), [])
        self.assertEqual(cf.last_message([], None), "")
        self.assertEqual(cf.last_message([], "old-run"), "final answer")

    def test_usage_reads_events_before_pruning_and_final_usage_afterward(self):
        _, meta, _ = self._make_prunable_run()
        events = cf.all_events("old-run", meta)
        expected = {"input_tokens": 11, "cached_input_tokens": 22, "output_tokens": 33}
        self.assertEqual(cf.usage_of(events), expected)
        self.assertEqual(cf.usage_of(events, meta), expected)

        self._prune()

        pruned_meta = cf.load_meta("old-run")
        empty = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        self.assertEqual(cf.usage_of([]), empty)
        self.assertEqual(cf.usage_of([], pruned_meta), expected)

    def test_inflight_estimate_counts_only_reasoning_since_the_last_settled_turn(self):
        def reasoning():
            return {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}}

        # Nothing in flight: the run has settled and usage_of holds the truth.
        settled = [reasoning(), reasoning(), {"type": "turn.completed", "usage": {}}]
        self.assertEqual(cf.inflight_output_estimate(settled), 0)

        # Two reasoning items arrived after the last completed turn.
        running = settled + [reasoning(), reasoning()]
        self.assertEqual(
            cf.inflight_output_estimate(running), 2 * cf.TOKENS_PER_REASONING_ITEM
        )

        # Only reasoning is priced. Commands and messages are not generation.
        noise = running + [
            {"type": "item.completed", "item": {"type": "command_execution"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        ]
        self.assertEqual(
            cf.inflight_output_estimate(noise), 2 * cf.TOKENS_PER_REASONING_ITEM
        )

    def test_output_cell_marks_an_estimate_and_never_marks_a_measurement(self):
        def reasoning():
            return {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}}

        done = [{"type": "turn.completed", "usage": {"output_tokens": 1000}}]
        # Settled: a bare number, because the log measured it.
        self.assertEqual(cf.render_output_tokens(done), "1000")

        # In flight: the tilde says the total is partly inferred. Printing this
        # as a bare number would be a claim the log cannot support.
        self.assertEqual(
            cf.render_output_tokens(done + [reasoning()]),
            f"~{1000 + cf.TOKENS_PER_REASONING_ITEM}",
        )

        # A run that has never completed a turn still reports something, which
        # is the whole point: a long first turn used to read as zero cost.
        self.assertEqual(
            cf.render_output_tokens([reasoning(), reasoning()]),
            f"~{2 * cf.TOKENS_PER_REASONING_ITEM}",
        )

    def test_mcp_health_reaches_list_result_and_finished_event(self):
        calls = [
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": status}}
            for status in ("completed", "completed", "failed")
        ]
        events = calls + [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}},
        ]
        _run, meta = self._make_run("mcp-run", pid=os.getpid(), turns=[events])
        self.assertEqual(cf.mcp_text([]), "mcp=0/0")

        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            cf.cmd_list(argparse.Namespace())
        self.assertIn("mcp=2/1", listed.getvalue())

        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            cf.cmd_result(argparse.Namespace(name="mcp-run", all=False))
        self.assertEqual(result.getvalue().splitlines()[0], "mcp=2/1")

        sleeps = 0

        def finish_then_stop(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                meta["pid"] = None
                self._write_meta("mcp-run", meta)
                return
            raise RuntimeError("stop")

        stream = io.StringIO()
        with mock.patch.object(cf, "claim_events_lock", return_value=None), \
             mock.patch.object(cf, "release_events_lock"), \
             mock.patch.object(cf, "script_fingerprint", return_value=None), \
             mock.patch.object(cf.time, "sleep", side_effect=finish_then_stop), \
             contextlib.redirect_stdout(stream), contextlib.suppress(RuntimeError):
            cf.cmd_events(argparse.Namespace(all=True))
        self.assertIn("finished mcp-run (engineer) mcp=2/1", stream.getvalue())

    def test_serve_turn_reader_reads_only_bytes_appended_since_the_last_poll(self):
        run, _meta = self._make_run("growing", pid=os.getpid())
        path = run / "turn-1.jsonl"
        first = (json.dumps({"type": "turn.started"}) + "\n").encode()
        path.write_bytes(first)
        logs = cf.TurnLogReader()

        initial = cf.run_events("growing", 0, False, logs)
        self.assertEqual(logs.bytes_read, len(first))
        appended = (
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "new"}})
            + "\n"
        ).encode()
        with path.open("ab") as fh:
            fh.write(appended)

        before = logs.bytes_read
        polled = cf.run_events("growing", initial["total"], False, logs)
        self.assertEqual(logs.bytes_read - before, len(appended))
        self.assertEqual([event["text"] for event in polled["events"]], ["~ new"])

        path.write_bytes(first)
        before = logs.bytes_read
        reread = cf.run_events("growing", 0, False, logs)
        self.assertEqual(logs.bytes_read - before, len(first))
        self.assertEqual(reread["total"], 1)

    def _write_outbox(self, name, messages):
        run = self.runs / name
        run.mkdir(parents=True, exist_ok=True)
        (run / "meta.json").write_text(json.dumps({"model": "m", "effort": "high", "turns": 1}))
        (run / "outbox.jsonl").write_text(
            "\n".join(json.dumps(m) for m in messages) + "\n", encoding="utf-8"
        )
        return run / "outbox.jsonl"

    def test_a_blocking_question_survives_being_read_and_only_an_answer_clears_it(self):
        # Reading is not answering. The agent has stopped and is waiting, so a
        # surface that goes quiet because someone glanced at it is lying.
        self._write_outbox(
            "blocked",
            [
                {"text": "informational", "at": "t0", "blocking": False, "read": False},
                {"text": "I am stuck", "at": "t1", "blocking": True, "read": False},
            ],
        )

        first = cf.read_outbox("blocked", include_unanswered_blocking=True)
        self.assertEqual([m["text"] for m in first], ["informational", "I am stuck"])

        # Second look: the informational one is spent, the blocking one is not.
        second = cf.read_outbox("blocked", include_unanswered_blocking=True)
        self.assertEqual([m["text"] for m in second], ["I am stuck"])

        self.assertEqual(cf.mark_blocking_answered("blocked"), 1)
        self.assertEqual(cf.read_outbox("blocked", include_unanswered_blocking=True), [])
        self.assertEqual(cf.unanswered_blocking("blocked"), [])

    def test_long_ask_reaches_events_and_inbox_all_after_delivery(self):
        question = "a" * 299 + "\n" + "b" * 300
        self._make_run("asker", pid=os.getpid())
        with contextlib.redirect_stdout(io.StringIO()):
            cf.cmd_ask(argparse.Namespace(name="asker", message=question, blocking=False))

        # Delivery spends the ordinary inbox entry, but neither the stream nor
        # the explicit history view may lose it.
        self.assertEqual(cf.read_outbox("asker")[0]["text"], question)
        events = io.StringIO()
        with mock.patch.object(cf, "claim_events_lock", return_value=None), \
             mock.patch.object(cf, "release_events_lock"), \
             mock.patch.object(cf, "script_fingerprint", return_value=None), \
             mock.patch.object(cf.time, "sleep", side_effect=RuntimeError("stop")), \
             contextlib.redirect_stdout(events), contextlib.suppress(RuntimeError):
            cf.cmd_events(argparse.Namespace(all=True))
        self.assertIn(question, events.getvalue())
        self.assertIn("blocking=false", events.getvalue())

        history = io.StringIO()
        with contextlib.redirect_stdout(history):
            cf.cmd_inbox(argparse.Namespace(names=["asker"], peek=False, all=True))
        self.assertIn(question, history.getvalue())
        self.assertIn("read", history.getvalue())

        ordinary = io.StringIO()
        with contextlib.redirect_stdout(ordinary):
            cf.cmd_inbox(argparse.Namespace(names=["asker"], peek=False, all=False))
        self.assertEqual(ordinary.getvalue(), "nothing raised\n")

    def test_the_hook_repeats_a_blocking_question_on_every_fire(self):
        # The hook's drain is a separate implementation from read_outbox, and it
        # is the one that lost a question for twenty minutes.
        self._write_outbox(
            "blocked-hook",
            [
                {"text": "one-shot", "at": "t0", "blocking": False, "read": False},
                {"text": "still stuck", "at": "t1", "blocking": True, "read": False},
            ],
        )
        with mock.patch.object(hook, "RUNS", self.runs):
            first = [m["text"] for m in hook.drain_outbox("blocked-hook")]
            second = [m["text"] for m in hook.drain_outbox("blocked-hook")]
            cf.mark_blocking_answered("blocked-hook")
            third = [m["text"] for m in hook.drain_outbox("blocked-hook")]
        self.assertEqual(first, ["one-shot", "still stuck"])
        self.assertEqual(second, ["still stuck"])
        self.assertEqual(third, [])

    def test_a_linked_worktree_gets_its_git_dir_made_writable(self):
        # A linked worktree keeps its metadata under the MAIN repo, outside -C.
        # The sandbox grants write to -C only, so without this the agent can edit
        # files it can never commit, and the error it sees is a read-only
        # filesystem on a path it never chose. Two agents lost turns to this.
        main_git = self.temp_path / "repo" / ".git" / "worktrees" / "feature"
        main_git.mkdir(parents=True)
        wt = self.temp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main_git}\n", encoding="utf-8")

        self.assertEqual(cf.linked_worktree_gitdir(str(wt)), str(main_git))

        meta = {
            "sandbox": "workspace-write",
            "cwd": str(wt),
            "model": "m",
            "effort": "high",
            "run_dir": str(self.runs / "w"),
        }
        fresh = cf.build_cmd(meta, "go")
        self.assertIn("--add-dir", fresh)
        self.assertIn(str(main_git), fresh)

        resumed = cf.build_cmd(meta, "go", resume_id="thread-1")
        roots = [a for a in resumed if a.startswith("sandbox_workspace_write.writable_roots=")]
        self.assertEqual(len(roots), 1)
        self.assertIn(str(main_git), roots[0])

    def test_an_ordinary_checkout_grants_no_extra_git_root(self):
        # `.git` is a directory already inside -C. Nothing to add, and adding a
        # guess would widen the sandbox for no reason.
        repo = self.temp_path / "plain"
        (repo / ".git").mkdir(parents=True)
        self.assertIsNone(cf.linked_worktree_gitdir(str(repo)))

        # A .git file pointing nowhere real is also not a root.
        broken = self.temp_path / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nope/does/not/exist\n", encoding="utf-8")
        self.assertIsNone(cf.linked_worktree_gitdir(str(broken)))

        # Neither is a .git file that is not a gitdir pointer at all.
        odd = self.temp_path / "odd"
        odd.mkdir()
        (odd / ".git").write_text("not a pointer\n", encoding="utf-8")
        self.assertIsNone(cf.linked_worktree_gitdir(str(odd)))

        self.assertIsNone(cf.linked_worktree_gitdir(""))
        self.assertIsNone(cf.linked_worktree_gitdir(str(self.temp_path / "absent")))

    def test_coverage_counts_only_observed_processes(self):
        # A wait or events monitor counts only when a `claude` process is in
        # its ancestry, because only then does its exit or output wake anyone.
        # Three dropped threads on 2026-08-22 had a wait that pgrep could see
        # and nobody could hear.
        with mock.patch.object(hook, "observed_pids", return_value=[]):
            self.assertEqual(hook.unwatched(["a", "b"]), ["a", "b"])
        with mock.patch.object(hook, "events_monitor_armed", return_value=True):
            self.assertEqual(hook.unwatched(["a", "b"]), [])
        with mock.patch.object(hook, "events_monitor_armed", return_value=False), mock.patch.object(
            hook, "observed_wait_names", return_value={"a"}
        ):
            self.assertEqual(hook.unwatched(["a", "b"]), ["b"])
        with mock.patch.object(hook, "events_monitor_armed", return_value=False), mock.patch.object(
            hook, "observed_wait_names", return_value={"*"}
        ):
            self.assertEqual(hook.unwatched(["a", "b"]), [])

    def test_wait_run_names_skips_flag_values(self):
        self.assertEqual(hook.wait_run_names(["a", "--timeout", "2700", "b"]), {"a", "b"})
        self.assertEqual(hook.wait_run_names(["--timeout=5", "x"]), {"x"})
        self.assertEqual(hook.wait_run_names(["--timeout", "5"]), set())

    def test_has_claude_ancestor_walks_proc(self):
        # A fake /proc: 300 -> 200 (bash) -> 100 (claude) -> 1.
        stats = {
            300: "300 (python3) S 200 1 1",
            200: "200 (bash) S 100 1 1",
            100: "100 (claude) S 1 1 1",
        }
        orphan = {300: "300 (python3) S 1 1 1"}
        real_open = open

        def fake_open(tree):
            def _open(path, *a, **k):
                if str(path).startswith("/proc/"):
                    pid = int(str(path).split("/")[2])
                    if pid in tree:
                        return io.StringIO(tree[pid])
                    raise OSError("no such pid")
                return real_open(path, *a, **k)
            return _open

        with mock.patch("builtins.open", fake_open(stats)):
            self.assertTrue(hook.has_claude_ancestor(300))
        with mock.patch("builtins.open", fake_open(orphan)):
            self.assertFalse(hook.has_claude_ancestor(300))

    def _stop_hook(self, stdin="{}"):
        return self._run_hook("codex-fleet-stop.py", stdin=stdin)

    def test_stop_hook_blocks_while_an_agent_runs_unobserved(self):
        run = self.runs / "busy"
        run.mkdir(parents=True)
        (run / "meta.json").write_text(
            json.dumps({"model": "m", "effort": "high", "role": "engineer", "turns": 1,
                        "pid": os.getpid(), "result_read_turn": 0})
        )
        (run / "turn-1.jsonl").write_text("")
        out = self._stop_hook()
        self.assertEqual(out.returncode, 0)
        body = json.loads(out.stdout)
        self.assertEqual(body["decision"], "block")
        self.assertIn("busy", body["reason"])
        self.assertIn("codex-fleet events", body["reason"])

    def test_stop_hook_blocks_on_an_unread_result_and_allows_once_read(self):
        run = self.runs / "done"
        run.mkdir(parents=True)
        (run / "meta.json").write_text(
            json.dumps({"model": "m", "effort": "high", "role": "engineer", "turns": 2,
                        "pid": 999999, "result_read_turn": 1})
        )
        (run / "turn-2.jsonl").write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}) + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}}) + "\n"
        )
        body = json.loads(self._stop_hook().stdout)
        self.assertEqual(body["decision"], "block")
        self.assertIn("done", body["reason"])

        # Reading it through `result` is what marks it read.
        with mock.patch.object(cf, "alive", return_value=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cf.cmd_result(argparse.Namespace(name="done", all=False))
        self.assertEqual(cf.load_meta("done")["result_read_turn"], 2)
        self.assertEqual(self._stop_hook().stdout, "")

    def test_stop_hook_exempts_runs_that_predate_read_tracking(self):
        # 44 historical runs tripped the first dry run. A run with no field was
        # read or not before anyone recorded it; blocking on it forever would
        # make the hook unusable.
        run = self.runs / "old"
        run.mkdir(parents=True)
        (run / "meta.json").write_text(
            json.dumps({"model": "m", "effort": "high", "role": "engineer", "turns": 3, "pid": 999999})
        )
        self.assertEqual(self._stop_hook().stdout, "")

    def test_stop_hook_honours_stop_hook_active(self):
        run = self.runs / "busy"
        run.mkdir(parents=True)
        (run / "meta.json").write_text(
            json.dumps({"model": "m", "effort": "high", "role": "engineer", "turns": 1,
                        "pid": os.getpid(), "result_read_turn": 0})
        )
        self.assertEqual(self._stop_hook(stdin=json.dumps({"stop_hook_active": True})).stdout, "")

    def test_tell_is_what_answers_a_blocking_question(self):
        # `tell` is the only call that actually reaches the agent, so it is the
        # only one that should retire what the agent is waiting on.
        self._write_outbox(
            "stuck",
            [{"text": "which one?", "at": "t0", "blocking": True, "read": True}],
        )
        self.assertEqual(len(cf.unanswered_blocking("stuck")), 1)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cf.cmd_tell(argparse.Namespace(name="stuck", message="this one"))

        self.assertEqual(cf.unanswered_blocking("stuck"), [])
        self.assertIn("1 blocking question(s) marked answered", out.getvalue())
        # And the answer actually reached the agent's inbox.
        inbox = (self.runs / "stuck" / "inbox.jsonl").read_text(encoding="utf-8")
        self.assertIn("this one", inbox)

    def test_a_pgrep_visible_wait_without_a_claude_ancestor_is_not_coverage(self):
        # This test used to assert bare-pgrep semantics. That was the bug: a
        # wait started with a shell & is visible to pgrep and observed by
        # nobody. Coverage now requires a live `claude` ancestor.
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="4242\n"
        )
        with mock.patch.object(hook.subprocess, "run", return_value=completed), mock.patch.object(
            hook, "has_claude_ancestor", return_value=False
        ):
            self.assertEqual(hook.observed_pids("wait"), [])
            self.assertEqual(hook.unwatched(["alpha"]), ["alpha"])
        with mock.patch.object(hook.subprocess, "run", return_value=completed), mock.patch.object(
            hook, "has_claude_ancestor", return_value=True
        ), mock.patch.object(hook, "same_fleet_home", return_value=True):
            self.assertEqual(hook.observed_pids("wait"), [4242])
        # A pgrep that cannot run must not invent coverage either way.
        with mock.patch.object(hook.subprocess, "run", side_effect=OSError("no pgrep")):
            self.assertEqual(hook.observed_pids("wait"), [])

    def test_list_shows_an_agent_that_is_waiting_on_an_answer(self):
        # A blocked agent used to render as "running", indistinguishable from one
        # that is thinking.
        self._write_outbox(
            "asker", [{"text": "well?", "at": "t0", "blocking": True, "read": True}]
        )
        (self.runs / "asker" / "turn-1.jsonl").write_text("")
        with mock.patch.object(cf, "alive", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cf.cmd_list(argparse.Namespace())
        self.assertIn("ASKING", out.getvalue())

    def test_daemon_status_list_and_stop_use_the_pidfile(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "CODEX_FLEET_HOME": str(self.fleet_home)}
        command = [sys.executable, str(ROOT / "codex-fleet"), "serve"]
        self.addCleanup(lambda: subprocess.run(
            [*command, "--stop"], env=env, capture_output=True, timeout=3
        ))

        started = subprocess.run(
            [*command, "--daemon", "--host", "127.0.0.1", "--port", str(port)],
            env=env, text=True, capture_output=True, timeout=8,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertTrue((self.fleet_home / "serve.pid").exists())
        status = subprocess.run(
            [*command, "--status"], env=env, text=True, capture_output=True, timeout=3
        )
        self.assertRegex(status.stdout, rf"pid=\d+ port={port}")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cf.cmd_list(argparse.Namespace())
        self.assertTrue(output.getvalue().startswith(f"dashboard: http://127.0.0.1:{port}\n"))

        stopped = subprocess.run(
            [*command, "--stop"], env=env, text=True, capture_output=True, timeout=3
        )
        self.assertIn("dashboard stopped", stopped.stdout)
        self.assertFalse((self.fleet_home / "serve.pid").exists())

    def test_status_hook_reminds_once_per_session_only_without_an_events_monitor(self):
        self._make_run("done", turns=[[{"type": "turn.completed", "usage": {}}]])
        prompt = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "one"})
        expected = (
            "codex-fleet: no events monitor armed; arm with "
            "Monitor({command:'codex-fleet events', persistent:true})"
        )
        state = self.fleet_home / ".hook-seen.json"

        def fire(payload, armed):
            output = io.StringIO()
            with mock.patch.object(hook, "RUNS", self.runs), \
                 mock.patch.object(hook, "STATE", state), \
                 mock.patch.object(hook, "events_monitor_armed", return_value=armed), \
                 mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                 contextlib.redirect_stdout(output):
                hook.main()
            return output.getvalue()

        first = fire(prompt, False)
        second = fire(prompt, False)
        self.assertEqual(
            json.loads(first)["hookSpecificOutput"]["additionalContext"], expected
        )
        self.assertEqual(second, "")

        armed_prompt = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "two"})
        self.assertEqual(fire(armed_prompt, True), "")

    def test_activity_reads_the_log_before_pruning_and_last_activity_afterward(self):
        run, meta, last_activity = self._make_prunable_run()
        turn = run / "turn-2.jsonl"
        meta["last_activity"] = self.clock - 60
        self._write_meta("old-run", meta)

        with mock.patch.object(cf.time, "time", return_value=self.clock):
            self.assertAlmostEqual(cf.mtime(turn), last_activity, places=3)
            self.assertAlmostEqual(cf.mtime(turn, meta), last_activity, places=3)
            self.assertEqual(cf.age(turn), "240h")
            self.assertEqual(cf.age(turn, meta), "240h")

        self._prune()
        pruned_meta = cf.load_meta("old-run")

        with mock.patch.object(cf.time, "time", return_value=self.clock):
            self.assertEqual(cf.mtime(turn), 0.0)
            self.assertAlmostEqual(cf.mtime(turn, pruned_meta), last_activity, places=3)
            self.assertEqual(cf.age(turn), "-")
            self.assertEqual(cf.age(turn, pruned_meta), "240h")

    def test_inbox_hook_is_silent_without_a_run_name(self):
        result = self._run_hook("codex-fleet-inbox.py")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_inbox_hook_is_silent_when_the_run_has_no_inbox(self):
        result = self._run_hook("codex-fleet-inbox.py", run_name="missing")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_inbox_messages_are_emitted_once_and_marked_read(self):
        inbox = self._inbox_path("worker")
        messages = [
            {"text": "first instruction", "read": False},
            {"text": "second instruction", "read": False},
            {"text": "old instruction", "read": True},
        ]
        self._write_jsonl(inbox, messages)
        event = json.dumps({"hook_event_name": "PostToolUse"})

        first = self._run_hook("codex-fleet-inbox.py", event, run_name="worker")
        second = self._run_hook("codex-fleet-inbox.py", event, run_name="worker")

        self.assertEqual(first.returncode, 0)
        payload = json.loads(first.stdout)
        hook_output = payload["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PostToolUse")
        self.assertIn("first instruction", hook_output["additionalContext"])
        self.assertIn("second instruction", hook_output["additionalContext"])
        self.assertNotIn("old instruction", hook_output["additionalContext"])
        stored = [json.loads(line) for line in inbox.read_text().splitlines()]
        self.assertTrue(all(message["read"] for message in stored))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertEqual(second.stderr, "")

    def test_inbox_hook_fails_open_on_malformed_stdin(self):
        self._inbox_path("worker").write_text("", encoding="utf-8")

        result = self._run_hook("codex-fleet-inbox.py", "{broken", run_name="worker")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_inbox_hook_fails_open_when_the_inbox_cannot_be_read(self):
        inbox = self._inbox_path("worker")
        inbox.mkdir()

        result = self._run_hook("codex-fleet-inbox.py", run_name="worker")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_status_hook_says_nothing_when_fleet_state_has_not_changed(self):
        completed = [[{"type": "turn.completed", "usage": {}}]]
        self._make_run("done", turns=completed)

        first = self._run_hook("codex-fleet-status.py")
        second = self._run_hook("codex-fleet-status.py")

        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, "")
        self.assertEqual(first.stderr, "")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertEqual(second.stderr, "")

    def test_status_hook_reports_when_a_running_agent_stops(self):
        run, meta = self._make_run(
            "worker",
            pid=os.getpid(),
            turns=[[{"type": "turn.started"}]],
        )
        started = self._run_hook("codex-fleet-status.py")
        self.assertIn("worker (engineer) started", started.stdout)

        meta["pid"] = None
        self._write_meta("worker", meta)
        self._write_jsonl(
            run / "turn-1.jsonl",
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "work complete"},
                },
                {"type": "turn.completed", "usage": {}},
            ],
        )

        stopped = self._run_hook("codex-fleet-status.py")

        self.assertEqual(stopped.returncode, 0)
        context = json.loads(stopped.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worker (engineer) finished", context)
        self.assertIn("codex-fleet result worker", context)
        self.assertIn("work complete", context)

    def test_status_hook_surfaces_a_NON_blocking_message_once(self):
        run, _ = self._make_run(
            "worker",
            turns=[[{"type": "turn.completed", "usage": {}}]],
        )
        outbox = run / "outbox.jsonl"
        self._write_jsonl(
            outbox,
            [{"text": "for your information", "blocking": False, "read": False}],
        )

        first = self._run_hook("codex-fleet-status.py")
        second = self._run_hook("codex-fleet-status.py")

        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worker asks: for your information", context)
        stored = json.loads(outbox.read_text(encoding="utf-8"))
        self.assertTrue(stored["read"])
        # Spent. Repeating an FYI is noise.
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertEqual(second.stderr, "")

    def test_status_hook_repeats_a_BLOCKING_message_until_it_is_answered(self):
        # This test used to assert the opposite, that a blocking question is
        # delivered once and marked read. That rule lost one on 2026-08-22: the
        # hook drained it, its output did not reach the orchestrator, and
        # `codex-fleet inbox` then printed "nothing raised" while the agent slept
        # in 30-second polls for twenty minutes. Reading is not answering.
        run, _ = self._make_run(
            "worker",
            turns=[[{"type": "turn.completed", "usage": {}}]],
        )
        outbox = run / "outbox.jsonl"
        self._write_jsonl(
            outbox,
            [{"text": "need a decision", "blocking": True, "read": False}],
        )

        first = self._run_hook("codex-fleet-status.py")
        second = self._run_hook("codex-fleet-status.py")

        for result in (first, second):
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("worker BLOCKING: need a decision", context)

        # Answering is what stops it, and only `tell` answers.
        cf.mark_blocking_answered("worker")
        third = self._run_hook("codex-fleet-status.py")
        self.assertEqual(third.returncode, 0)
        self.assertEqual(third.stdout, "")

    def test_status_hook_fails_open_when_fleet_state_cannot_be_read(self):
        self.runs.rmdir()
        self.runs.write_text("not a directory", encoding="utf-8")

        result = self._run_hook("codex-fleet-status.py")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_embedded_dashboard_matches_dashboard_source(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "embed-page.py"), "--check"],
            text=True,
            capture_output=True,
            cwd=ROOT,
            env=self._hook_env(),
            timeout=3,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_embedded_dashboard_is_byte_identical_to_source(self):
        page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(cf.SERVE_PAGE, page)
        self.assertNotIn('"""', page)

    def test_command_events_use_distinct_started_and_completed_classes(self):
        started = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "true"},
        }
        completed = {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "true", "exit_code": 0},
        }

        self.assertEqual(cf.event_kind(started), "cmd-start")
        self.assertEqual(cf.event_kind(completed), "cmd")


    # ------------------------------------------------------------------ wait
    #
    # `wait` exiting is the orchestrator's only reliable wake-up. It used to
    # block until EVERY named run finished, which hid a fast agent's result
    # behind a slow one until somebody noticed by eye. Both directions are
    # pinned here because the barrier is still wanted under --all.

    def _wait_args(self, names, **kw):
        return argparse.Namespace(
            names=names, timeout=kw.get("timeout", 5), all=kw.get("all", False)
        )

    def test_wait_returns_as_soon_as_one_run_finishes(self):
        self._make_run("finished", pid=None)
        self._make_run("still-going", pid=os.getpid())

        buf = io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(buf):
            cf.cmd_wait(self._wait_args(["finished", "still-going"]))
        elapsed = time.monotonic() - started

        out = buf.getvalue()
        self.assertIn("finished", out)
        self.assertIn("still running: still-going", out)
        self.assertIn("codex-fleet wait still-going", out)
        # It must not have waited on the live run at all.
        self.assertLess(elapsed, 1.0)

    def test_wait_all_keeps_waiting_while_any_run_is_alive(self):
        self._make_run("finished", pid=None)
        self._make_run("still-going", pid=os.getpid())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as raised:
                cf.cmd_wait(self._wait_args(["finished", "still-going"], all=True, timeout=1))

        # Exit 3 is the timeout path, which proves --all did not return early.
        self.assertEqual(raised.exception.code, 3)
        self.assertIn("finished", buf.getvalue())

    def test_wait_reports_a_failed_run_rather_than_calling_it_done(self):
        run, _ = self._make_run("broken", pid=None, turns=[[]])
        (run / "turn-1.err").write_text("boom: the agent died\n", encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cf.cmd_wait(self._wait_args(["broken"]))

        out = buf.getvalue()
        self.assertIn("broken", out)
        self.assertIn("boom: the agent died", out)


    # ------------------------------------------------- unwatched-agent warning
    #
    # `wait` returns on the first completion, so covering a fan-out means
    # re-issuing it. Forgetting that is how finished agents go unread. The
    # warning fires where the mistake actually happens, on reading a result.

    def test_result_warns_when_a_running_agent_has_no_wait(self):
        self._make_run("still-going", pid=os.getpid())
        buf = io.StringIO()
        with mock.patch.object(cf.subprocess, "run",
                               return_value=mock.Mock(stdout="")) as ran:
            with contextlib.redirect_stderr(buf):
                cf.warn_if_unwatched()
        self.assertIn("still-going", buf.getvalue())
        self.assertIn("codex-fleet wait still-going", buf.getvalue())
        self.assertTrue(ran.called)

    def test_result_stays_quiet_when_a_wait_already_names_the_run(self):
        self._make_run("still-going", pid=os.getpid())
        buf = io.StringIO()
        with mock.patch.object(
            cf.subprocess, "run",
            return_value=mock.Mock(stdout="4321 codex-fleet wait still-going\n"),
        ):
            with contextlib.redirect_stderr(buf):
                cf.warn_if_unwatched()
        self.assertEqual(buf.getvalue(), "")

    def test_result_stays_quiet_when_nothing_is_running(self):
        self._make_run("finished", pid=None)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cf.warn_if_unwatched()
        self.assertEqual(buf.getvalue(), "")

    def test_the_run_being_read_does_not_warn_about_itself(self):
        self._make_run("just-read", pid=os.getpid())
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cf.warn_if_unwatched(exclude="just-read")
        self.assertEqual(buf.getvalue(), "")


    def test_reading_a_result_actually_runs_the_unwatched_check(self):
        # The four tests above call warn_if_unwatched directly, so they pass
        # even if cmd_result never calls it. This one pins the wiring, which is
        # the part that failed in practice: the check existing is worth nothing
        # if reading a result does not reach it.
        self._make_run(
            "answered",
            pid=None,
            turns=[[
                {"type": "item.completed",
                 "item": {"type": "agent_message", "text": "done"}},
                {"type": "turn.completed", "usage": {}},
            ]],
        )
        with mock.patch.object(cf, "warn_if_unwatched") as checked:
            with contextlib.redirect_stdout(io.StringIO()):
                cf.cmd_result(argparse.Namespace(name="answered", all=False))
        checked.assert_called_once_with(exclude="answered")


if __name__ == "__main__":
    unittest.main()


class OwnershipTests(CodexFleetTests):
    """Two orchestrator sessions ran fleets at once on 2026-08-23 and each
    one's hooks nagged about the other's runs. Ownership scopes the noise."""

    def _unread_meta(self, owner=None):
        meta = {"model": "m", "effort": "high", "role": "engineer", "turns": 2,
                "pid": 999999, "result_read_turn": 1}
        if owner is not None:
            meta["owner"] = owner
        return meta

    def _unread_run(self, name, owner=None):
        run = self.runs / name
        run.mkdir(parents=True)
        (run / "meta.json").write_text(json.dumps(self._unread_meta(owner)))
        (run / "turn-2.jsonl").write_text(
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "ok"}}) + "\n"
        )

    def test_owned_here_matrix(self):
        self.assertTrue(cf.owned_here({}, owner="sess-a"))
        self.assertTrue(cf.owned_here({"owner": "sess-a"}, owner="sess-a"))
        self.assertFalse(cf.owned_here({"owner": "sess-b"}, owner="sess-a"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            os.environ.pop("CODEX_FLEET_OWNER", None)
            self.assertTrue(cf.owned_here({"owner": "sess-b"}))

    def test_current_owner_prefers_the_explicit_override(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env",
                                          "CODEX_FLEET_OWNER": "sess-override"}):
            self.assertEqual(cf.current_owner(), "sess-override")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}, clear=False):
            os.environ.pop("CODEX_FLEET_OWNER", None)
            self.assertEqual(cf.current_owner(), "sess-env")

    def test_stop_hook_ignores_other_sessions_unread_results(self):
        self._unread_run("mine", owner="sess-a")
        self._unread_run("theirs", owner="sess-b")
        out = self._run_hook("codex-fleet-stop.py",
                             stdin=json.dumps({"session_id": "sess-a"}))
        body = json.loads(out.stdout)
        self.assertEqual(body["decision"], "block")
        self.assertIn("mine", body["reason"])
        self.assertNotIn("theirs", body["reason"])

    def test_stop_hook_still_blocks_on_legacy_unowned_runs(self):
        self._unread_run("legacy")
        body = json.loads(self._run_hook(
            "codex-fleet-stop.py",
            stdin=json.dumps({"session_id": "sess-a"})).stdout)
        self.assertEqual(body["decision"], "block")
        self.assertIn("legacy", body["reason"])

    def test_stop_hook_with_no_identity_sees_every_run(self):
        self._unread_run("mine", owner="sess-a")
        self._unread_run("theirs", owner="sess-b")
        body = json.loads(self._run_hook("codex-fleet-stop.py", stdin="{}").stdout)
        self.assertEqual(body["decision"], "block")
        self.assertIn("mine", body["reason"])
        self.assertIn("theirs", body["reason"])

    def test_status_survey_filters_foreign_runs(self):
        self._unread_run("mine", owner="sess-a")
        self._unread_run("theirs", owner="sess-b")
        self._unread_run("legacy")
        old_runs = hook.RUNS
        hook.RUNS = self.runs
        try:
            self.assertEqual(sorted(hook.survey(owner="sess-a")), ["legacy", "mine"])
            self.assertEqual(sorted(hook.survey()), ["legacy", "mine", "theirs"])
        finally:
            hook.RUNS = old_runs


class FleetTempHome(unittest.TestCase):
    """Temp fleet home only. Deliberately holds no tests of its own: a test
    class that inherits from one that has tests re-runs every one of them."""

    def setUp(self):
        self._old_root = cf.ROOT
        self._old_runs = cf.RUNS
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.fleet_home = self.temp_path / "fleet-home"
        self.runs = self.fleet_home / "runs"
        self.runs.mkdir(parents=True)
        cf.ROOT = self.fleet_home
        cf.RUNS = self.runs
        self.addCleanup(self._restore)

    def _restore(self):
        cf.ROOT = self._old_root
        cf.RUNS = self._old_runs
        self._temp.cleanup()


class GcRetentionTests(FleetTempHome):
    """Retention has to delete things, so every guard gets a test that would
    fail if the guard were removed."""

    def _collapsed_run(self, name, *, pruned_days_ago, **meta_updates):
        run = self.runs / name
        run.mkdir()
        (run / "result.txt").write_text("the conclusion", encoding="utf-8")
        when = datetime.fromtimestamp(
            time.time() - pruned_days_ago * 86400, tz=timezone.utc
        ).isoformat()
        meta = {
            "name": name,
            "role": "engineer",
            "turns": 1,
            "thread_id": "t-1",
            "pruned_at": when,
            "final_status": "idle",
            "last_activity": time.time() - pruned_days_ago * 86400,
        }
        meta.update(meta_updates)
        (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return run

    def _gc(self, *, prune_days=2, delete_days=14, dry_run=False, quiet=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cf.cmd_gc(argparse.Namespace(
                prune_days=prune_days, delete_days=delete_days,
                dry_run=dry_run, quiet=quiet,
            ))
        return buf.getvalue()

    def test_a_collapsed_run_past_the_limit_is_deleted(self):
        run = self._collapsed_run("ancient", pruned_days_ago=20)
        self._gc(delete_days=14)
        self.assertFalse(run.exists())

    def test_a_collapsed_run_inside_the_limit_survives(self):
        run = self._collapsed_run("recent", pruned_days_ago=3)
        self._gc(delete_days=14)
        self.assertTrue(run.exists())
        self.assertTrue((run / "result.txt").exists())

    def test_a_run_that_was_never_collapsed_is_never_deleted(self):
        run = self._collapsed_run("uncollapsed", pruned_days_ago=99)
        meta = json.loads((run / "meta.json").read_text())
        del meta["pruned_at"]
        (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self._gc(delete_days=1)
        self.assertTrue(run.exists())

    def test_a_running_agent_is_never_deleted_however_old_its_record(self):
        run = self._collapsed_run("busy", pruned_days_ago=99, pid=os.getpid())
        self._gc(delete_days=1)
        self.assertTrue(run.exists())

    def test_delete_days_zero_deletes_nothing(self):
        run = self._collapsed_run("ancient", pruned_days_ago=999)
        self._gc(delete_days=0)
        self.assertTrue(run.exists())

    def test_dry_run_deletes_nothing_and_says_what_it_would_do(self):
        run = self._collapsed_run("ancient", pruned_days_ago=20)
        out = self._gc(delete_days=14, dry_run=True)
        self.assertTrue(run.exists())
        self.assertIn("would delete", out)
        self.assertIn("ancient", out)

    def test_a_delete_that_did_not_happen_is_not_reported_as_done(self):
        """shutil.rmtree(ignore_errors=True) hides a permission or filesystem
        failure, so the directory can survive while gc says it went."""
        run = self._collapsed_run("stubborn", pruned_days_ago=20)
        with mock.patch.object(cf.shutil, "rmtree", lambda *a, **k: None):
            out = self._gc(delete_days=14)

        self.assertTrue(run.exists())
        self.assertIn("COULD NOT delete stubborn", out)
        self.assertIn("deleted 0 collapsed run(s)", out)

    def test_freed_space_is_measured_in_bytes_not_rounded_display_values(self):
        """Summing the printed per-run figures added up numbers already rounded
        to one decimal megabyte: four 60 KB logs were reported as 0.4 MB when
        0.23 MB actually went."""
        old = time.time() - 10 * 86400
        for i in range(4):
            run = self.runs / f"r{i}"
            run.mkdir()
            (run / "meta.json").write_text(json.dumps(
                {"name": f"r{i}", "turns": 1, "pid": None, "thread_id": "t"}))
            log = run / "turn-1.jsonl"
            log.write_bytes(b'{"type":"turn.completed"}\n' + b"x" * (60000 - 26))
            os.utime(log, (old, old))

        out = self._gc(prune_days=2)

        self.assertIn("collapsed 4 run(s), 0.2 MB", out)

    def test_quiet_says_nothing_when_there_was_nothing_to_do(self):
        self.assertEqual(self._gc(quiet=True).strip(), "")


class WorktreeSweepTests(FleetTempHome):
    """A worktree is deleted work if the sweep gets it wrong, so the merged
    check is tested against a squash merge, which is what this repo actually
    does and what an ancestry test alone would miss."""

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=False,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    def _repo(self):
        root = self.temp_path / "repo"
        root.mkdir()
        self._git("init", "-q", "-b", "main", cwd=root)
        (root / "a.txt").write_text("one\n")
        self._git("add", "-A", cwd=root)
        self._git("commit", "-qm", "first", cwd=root)
        return root

    def _run_with_worktree(self, name, root, branch, *, pid=None):
        wt = self.temp_path / f"wt-{name}"
        self._git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=root)
        run = self.runs / name
        run.mkdir()
        (run / "meta.json").write_text(json.dumps({
            "name": name, "role": "engineer", "turns": 1, "pid": pid,
            "worktree": str(wt), "worktree_root": str(root), "worktree_branch": branch,
            "pruned_at": None,
        }), encoding="utf-8")
        return wt

    def test_a_squash_merged_branch_is_recognised_as_landed_and_swept(self):
        root = self._repo()
        wt = self._run_with_worktree("squashed", root, "fleet/squashed")
        (wt / "b.txt").write_text("work\n")
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-qm", "the work", cwd=wt)
        # Squash merge: the content lands on main under a different commit, so
        # the branch is NOT an ancestor of main.
        self._git("merge", "--squash", "fleet/squashed", cwd=root)
        self._git("commit", "-qm", "squashed in", cwd=root)
        self.assertNotEqual(
            0,
            self._git("merge-base", "--is-ancestor", "fleet/squashed", "main", cwd=root).returncode,
            "fixture is wrong: a squash merge must not be an ancestor",
        )

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["squashed"])
        self.assertEqual(refused, [])
        self.assertFalse(wt.exists())

    def test_an_ordinary_merge_is_also_swept(self):
        root = self._repo()
        wt = self._run_with_worktree("merged", root, "fleet/merged")
        (wt / "b.txt").write_text("work\n")
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-qm", "the work", cwd=wt)
        self._git("merge", "--no-ff", "-q", "-m", "merge", "fleet/merged", cwd=root)

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["merged"])
        self.assertFalse(wt.exists())

    def test_a_branch_holding_unmerged_work_is_refused_and_kept(self):
        root = self._repo()
        wt = self._run_with_worktree("unmerged", root, "fleet/unmerged")
        (wt / "b.txt").write_text("work nobody merged\n")
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-qm", "the work", cwd=wt)

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertEqual([n for n, _ in refused], ["unmerged"])
        self.assertIn("not in main", refused[0][1])
        self.assertTrue(wt.exists())

    def test_uncommitted_changes_are_refused_and_kept(self):
        root = self._repo()
        wt = self._run_with_worktree("dirty", root, "fleet/dirty")
        (wt / "scratch.txt").write_text("half a thought\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertEqual(refused, [("dirty", "uncommitted changes")])
        self.assertTrue(wt.exists())

    def test_an_edited_env_file_is_refused_even_though_git_ignores_it(self):
        """`git status --porcelain` says nothing about ignored files, and
        `git worktree remove --force` deletes them anyway. spawn copies .env
        into every worktree, so an agent that edited one had that edit
        destroyed with no warning."""
        root = self._repo()
        (root / ".gitignore").write_text(".env\n")
        (root / ".env").write_text("the original\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore env", cwd=root)
        wt = self._run_with_worktree("envy", root, "fleet/envy")
        (wt / ".env").write_text("the agent changed this\n")
        self.assertEqual(
            "", self._git("status", "--porcelain", cwd=wt).stdout.strip(),
            "fixture is wrong: git must consider this worktree clean",
        )

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertEqual([n for n, _ in refused], ["envy"])
        self.assertIn(".env", refused[0][1])
        self.assertTrue(wt.exists())
        self.assertEqual((wt / ".env").read_text(), "the agent changed this\n")

    def test_an_untouched_env_copy_does_not_block_the_sweep(self):
        """The refusal has to be about the agent's work, not about the file
        spawn put there. Refusing on an unmodified copy would mean no worktree
        is ever swept in any project that ignores .env."""
        root = self._repo()
        (root / ".gitignore").write_text(".env\n")
        (root / ".env").write_text("the original\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore env", cwd=root)
        wt = self._run_with_worktree("copied", root, "fleet/copied")
        (wt / ".env").write_text("the original\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["copied"])
        self.assertFalse(wt.exists())

    def test_other_ignored_files_are_destroyed_but_never_silently(self):
        """A project's .gitignore is its own statement that these files are
        reproducible, and a worktree holds hundreds of them after a build: 414
        in the main checkout here. Refusing on all of them would mean no
        worktree is ever removed. So they go, and they are named."""
        root = self._repo()
        (root / ".gitignore").write_text("build/\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore build", cwd=root)
        wt = self._run_with_worktree("built", root, "fleet/built")
        (wt / "build").mkdir()
        (wt / "build" / "out.js").write_text("compiled\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["built"], refused)
        self.assertEqual(removed[0][2], ["build"])
        self.assertFalse(wt.exists())

    def test_the_report_names_what_the_removal_took_with_it(self):
        root = self._repo()
        (root / ".gitignore").write_text("build/\nlogs/\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore", cwd=root)
        wt = self._run_with_worktree("noisy", root, "fleet/noisy")
        for d in ("build", "logs"):
            (wt / d).mkdir()
            (wt / d / "f").write_text("x")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cf.cmd_gc(argparse.Namespace(
                prune_days=2, delete_days=14, dry_run=False, quiet=False))
        out = buf.getvalue()

        self.assertIn("noisy also took 2 ignored path(s)", out)
        self.assertIn("build", out)
        self.assertIn("logs", out)
        self.assertIn("kept in", out)
        self.assertIn("ignored-files.tar.gz", out)

    def test_the_whole_env_family_is_protected_not_two_literal_names(self):
        """A review named .env.development.local: just as unrecoverable as
        .env, and it was being treated as disposable. One the agent CREATED
        has no copy to compare against, so it refuses."""
        root = self._repo()
        (root / ".gitignore").write_text(".env*\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore env family", cwd=root)
        wt = self._run_with_worktree("family", root, "fleet/family")
        (wt / ".env.development.local").write_text("agent wrote this\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertIn(".env.development.local", refused[0][1])
        self.assertTrue((wt / ".env.development.local").exists())

    def test_an_edited_env_local_is_refused_like_env(self):
        root = self._repo()
        (root / ".gitignore").write_text(".env*\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore env family", cwd=root)
        (root / ".env.local").write_text("original\n")
        wt = self._run_with_worktree("locally", root, "fleet/locally")
        (wt / ".env.local").write_text("edited by the agent\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertIn(".env.local", refused[0][1])

    def test_ignored_files_are_archived_before_the_worktree_goes(self):
        """A printed line is a report, not a record. Small ignored files are
        copied into the run directory so a removal can be undone."""
        root = self._repo()
        (root / ".gitignore").write_text("notes/\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore notes", cwd=root)
        wt = self._run_with_worktree("noted", root, "fleet/noted")
        (wt / "notes").mkdir()
        (wt / "notes" / "thinking.md").write_text("the agent's reasoning\n")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["noted"], refused)
        self.assertFalse(wt.exists())
        archive = self.runs / "noted" / "ignored-files.tar.gz"
        self.assertTrue(archive.exists(), "the ignored file was not kept anywhere")
        import tarfile
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.getnames(), ["notes/thinking.md"])
            self.assertEqual(
                tar.extractfile("notes/thinking.md").read(),
                b"the agent's reasoning\n",
            )

    def test_a_file_too_large_to_archive_is_named_as_gone_for_good(self):
        """The budget exists so build output does not get copied. Whatever it
        excludes has to be said out loud."""
        root = self._repo()
        (root / ".gitignore").write_text("build/\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore build", cwd=root)
        wt = self._run_with_worktree("heavy", root, "fleet/heavy")
        (wt / "build").mkdir()
        (wt / "build" / "big.bundle").write_bytes(b"x" * (cf.GC_ARCHIVE_MAX_FILE_BYTES + 1))
        (wt / "build" / "small.json").write_text("{}")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["heavy"], refused)
        archived, skipped = removed[0][3], removed[0][4]
        self.assertEqual(archived, 1)
        self.assertEqual(skipped, ["build/big.bundle"])

    def test_the_node_modules_symlink_does_not_block_the_sweep(self):
        """spawn links node_modules into every worktree and git ignores it.
        Treating that as the agent's work would mean no worktree is ever swept
        in any project with dependencies installed."""
        root = self._repo()
        (root / ".gitignore").write_text("node_modules\n")
        self._git("add", ".gitignore", cwd=root)
        self._git("commit", "-qm", "ignore modules", cwd=root)
        (root / "node_modules").mkdir()
        (root / "node_modules" / "big.js").write_text("x" * 100)
        wt = self._run_with_worktree("linked", root, "fleet/linked")
        os.symlink(root / "node_modules", wt / "node_modules")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual([e[0] for e in removed], ["linked"], refused)
        self.assertTrue((root / "node_modules" / "big.js").exists(),
                        "the real node_modules must survive")

    def test_a_running_agents_worktree_is_never_swept(self):
        root = self._repo()
        wt = self._run_with_worktree("busy", root, "fleet/busy", pid=os.getpid())

        removed, refused = cf.sweep_worktrees()

        self.assertEqual(removed, [])
        self.assertEqual(refused, [("busy", "agent still running")])
        self.assertTrue(wt.exists())

    def test_a_dangling_worktree_reference_is_cleared_not_reported(self):
        run = self.runs / "gone"
        run.mkdir()
        (run / "meta.json").write_text(json.dumps({
            "name": "gone", "turns": 1, "pid": None,
            "worktree": str(self.temp_path / "not-here"),
            "worktree_root": str(self.temp_path / "repo"),
            "worktree_branch": "fleet/gone",
        }), encoding="utf-8")

        removed, refused = cf.sweep_worktrees()

        self.assertEqual((removed, refused), ([], []))
        self.assertNotIn("worktree", json.loads((run / "meta.json").read_text()))

    def test_dry_run_sweeps_nothing(self):
        root = self._repo()
        wt = self._run_with_worktree("squashed", root, "fleet/squashed")
        # Real committed work, squash-merged and committed, so the branch is
        # genuinely NOT an ancestor of main. Without this the test passed even
        # with the squash-merge check deleted.
        (wt / "b.txt").write_text("work\n")
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-qm", "the work", cwd=wt)
        self._git("merge", "--squash", "fleet/squashed", cwd=root)
        self._git("commit", "-qm", "squashed in", cwd=root)
        self.assertNotEqual(
            0,
            self._git("merge-base", "--is-ancestor", "fleet/squashed", "main",
                      cwd=root).returncode,
        )

        removed, refused = cf.sweep_worktrees(dry_run=True)

        self.assertEqual([e[0] for e in removed], ["squashed"])
        self.assertTrue(wt.exists())


class SpawnWorktreeTests(FleetTempHome):
    """The sweep protects the files spawn copies in. These tests create those
    files by hand, so this one checks spawn actually creates them: without it
    the protection could be guarding something that no longer happens."""

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    def test_spawn_copies_the_env_files_and_links_node_modules(self):
        root = self.temp_path / "repo"
        root.mkdir()
        self._git("init", "-q", "-b", "main", cwd=root)
        (root / "a.txt").write_text("x")
        self._git("add", "-A", cwd=root)
        self._git("commit", "-qm", "first", cwd=root)
        (root / ".env").write_text("SECRET=1\n")
        (root / ".env.local").write_text("LOCAL=1\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.js").write_text("//")

        with mock.patch.object(cf, "ROOT", self.fleet_home):
            wt, branch, resolved_root = cf.make_worktree("wtest", str(root))
        wt = Path(wt)
        self.addCleanup(lambda: self._git("worktree", "remove", "--force", str(wt), cwd=root))

        self.assertEqual((wt / ".env").read_text(), "SECRET=1\n")
        self.assertEqual((wt / ".env.local").read_text(), "LOCAL=1\n")
        self.assertTrue((wt / "node_modules").is_symlink())
        self.assertEqual(resolved_root, str(root))
        for name in (".env", ".env.local", "node_modules"):
            self.assertTrue(
                cf.is_spawn_placed(name),
                f"spawn creates {name} but the sweep does not protect it",
            )

    def test_spawn_installs_husky_hook_shims_so_commit_hooks_fire(self):
        """core.hooksPath=.husky/_ is gitignored and only husky creates it; a
        worktree without it commits with NO hooks and reports green on lint
        errors (2026-09-06). A stub husky bin stands in for the real one."""
        root = self.temp_path / "repo"
        root.mkdir()
        self._git("init", "-q", "-b", "main", cwd=root)
        (root / ".husky").mkdir()
        (root / ".husky" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        (root / ".gitignore").write_text("node_modules\n.husky/_\n")
        (root / "a.txt").write_text("x")
        self._git("add", "-A", cwd=root)
        self._git("commit", "-qm", "first", cwd=root)
        husky_dir = root / "node_modules" / "husky"
        husky_dir.mkdir(parents=True)
        (husky_dir / "bin.js").write_text(
            "require('fs').mkdirSync('.husky/_', {recursive: true});"
            "require('fs').writeFileSync('.husky/_/h', '#!/bin/sh\\n');"
        )

        with mock.patch.object(cf, "ROOT", self.fleet_home):
            wt, _branch, _root = cf.make_worktree("wthooks", str(root))
        wt = Path(wt)
        self.addCleanup(lambda: self._git("worktree", "remove", "--force", str(wt), cwd=root))

        self.assertTrue((wt / ".husky" / "_" / "h").is_file(),
                        "spawn must run husky so git finds the hook shims")

    def test_install_git_hooks_is_a_no_op_without_husky(self):
        wt = self.temp_path / "plain"
        wt.mkdir()
        cf.install_git_hooks(wt)  # no .husky, no node_modules: must not raise
        self.assertFalse((wt / ".husky").exists())


class CronInstallTests(unittest.TestCase):
    """install.sh writes to the user's crontab, so its failure modes are
    somebody's lost scheduled jobs. A stub crontab stands in for the real one."""

    STUB = '#!/usr/bin/env bash\nmode=$(cat MODEFILE)\nif [[ "$1" == "-l" ]]; then\n  if [[ "$mode" == "broken" ]]; then echo "some other listing"; exit 2; fi\n  [[ -f TABLEFILE ]] || exit 1\n  cat TABLEFILE\n  exit 0\nfi\ncat > TABLEFILE\n'

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        base = Path(self._temp.name)
        self.home = base / "home"; self.home.mkdir()
        self.bin = base / "bin"; self.bin.mkdir()
        self.table = base / "crontab.txt"
        self.mode = base / "mode"; self.mode.write_text("normal")
        stub = self.bin / "crontab"
        stub.write_text(
            self.STUB.replace("MODEFILE", str(self.mode)).replace("TABLEFILE", str(self.table))
        )
        stub.chmod(0o755)

    def _install(self, *args):
        return subprocess.run(
            ["bash", str(ROOT / "install.sh"), *args],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.home),
                 "PATH": f"{self.bin}:{os.environ['PATH']}"},
        )

    def test_a_fresh_install_adds_the_job_and_creates_its_log_directory(self):
        """cron sets up the output redirection BEFORE running the command, so
        a missing directory means the job fails without ever starting gc."""
        result = self._install()

        self.assertIn("added    daily gc cron", result.stdout)
        self.assertIn("codex-fleet gc", self.table.read_text())
        self.assertTrue((self.home / ".codex-fleet").is_dir(),
                        "the directory the cron line redirects into is missing")

    def test_skill_install_preserves_existing_file_and_is_repeatable(self):
        skill = self.home / ".claude/skills/codex-fleet/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("local custom skill")
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(skill.is_symlink())
        self.assertEqual(skill.resolve(), ROOT / "skills/codex-fleet/SKILL.md")
        backups = list(skill.parent.glob("SKILL.md.replaced-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "local custom skill")
        self.assertEqual(self._install().returncode, 0)
        self.assertEqual(list(skill.parent.glob("SKILL.md.replaced-*")), backups)
        self.assertEqual(self._install("--check").returncode, 0)
        skill.unlink()
        self.assertNotEqual(self._install("--check").returncode, 0)

    def test_check_on_fresh_home_creates_nothing(self):
        result = self._install("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(self.table.exists())

    def test_existing_crontab_entries_survive(self):
        self.table.write_text("MAILTO=me\n0 3 * * * /usr/bin/payroll\n")

        self._install()

        text = self.table.read_text()
        self.assertIn("MAILTO=me", text)
        self.assertIn("/usr/bin/payroll", text)
        self.assertIn("codex-fleet gc", text)

    def test_running_it_twice_does_not_add_the_job_twice(self):
        self._install()
        first = self.table.read_text()

        second = self._install()

        self.assertIn("ok       daily gc cron", second.stdout)
        self.assertEqual(self.table.read_text(), first)

    def test_a_failed_crontab_read_changes_nothing(self):
        """`crontab -l` exits non-zero both when there is no crontab and when
        the read genuinely failed. Piping a failed read into `crontab -` would
        install a crontab holding only our line and discard the rest."""
        self.table.write_text("0 3 * * * /usr/bin/payroll\n")
        self.mode.write_text("broken")

        result = self._install()

        self.assertIn("SKIPPED  daily gc cron", result.stdout)
        self.assertEqual(self.table.read_text(), "0 3 * * * /usr/bin/payroll\n")

    def test_a_commented_out_entry_is_not_a_scheduled_job(self):
        self.table.write_text("#30 6 * * * codex-fleet gc --quiet\n")

        result = self._install("--check")

        self.assertIn("MISSING  daily gc cron", result.stdout)


class EventsMonitorTests(FleetTempHome):
    """The two failures that let a monitor run for two days on deleted code
    while a second one duplicated every notification."""

    def _events(self, owner="session-a"):
        """Run the stream with a tripwire on its sleep.

        The monitor loops forever by design, so a test that only relies on the
        exit condition working will HANG rather than fail when that condition
        breaks. Reaching the sleep means the loop did not exit, so make that an
        immediate failure with a readable message.
        """
        buf = io.StringIO()
        tripwire = AssertionError("the monitor entered its poll loop instead of exiting")
        with mock.patch.object(cf, "current_owner", return_value=owner):
            with mock.patch.object(cf.time, "sleep", side_effect=tripwire):
                with contextlib.redirect_stdout(buf):
                    with contextlib.suppress(AssertionError):
                        cf.cmd_events(argparse.Namespace(all=False))
        return buf.getvalue()

    def _hold_lock_in_a_child(self, owner):
        """Fork a child that really holds the lock and stays alive.

        The lock is a property of a live process now, so a test cannot fake a
        holder by writing a pid into a file. Returns the child's pid; the
        caller gets it killed and reaped on cleanup.
        """
        ready_r, ready_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                got = cf.claim_events_lock(owner) is None
                os.write(ready_w, b"y" if got else b"n")
                time.sleep(30)
            except Exception:
                pass
            os._exit(0)
        os.close(ready_w)
        self.addCleanup(lambda: os.waitpid(pid, 0))
        self.addCleanup(lambda: os.kill(pid, 9))
        self.assertEqual(os.read(ready_r, 1), b"y", "the child failed to take the lock")
        os.close(ready_r)
        return pid

    def test_a_second_monitor_for_the_same_session_refuses_to_start(self):
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        holder = self._hold_lock_in_a_child("session-a")

        # side_effect, not return_value: if the refusal ever stops working,
        # this fails fast instead of streaming forever.
        with mock.patch.object(cf, "script_fingerprint", side_effect=["a", "b"]):
            out = self._events(owner="session-a")

        self.assertIn("already streaming", out)
        self.assertIn(str(holder), out)

    def test_the_lock_a_dead_monitor_held_is_free_for_the_next_one(self):
        """A crashed monitor must not disable notifications until somebody
        finds a file they do not know exists. The kernel drops the lock when
        the process dies, so there is nothing to clean up."""
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        pid = os.fork()
        if pid == 0:
            cf.claim_events_lock("session-a")
            os._exit(0)  # dies while "holding" it
        os.waitpid(pid, 0)
        time.sleep(0.2)

        self.assertIsNone(cf.claim_events_lock("session-a"))
        cf.release_events_lock("session-a")

    def test_two_different_sessions_each_get_their_own_stream(self):
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        self._hold_lock_in_a_child("session-a")

        self.assertIsNone(cf.claim_events_lock("session-b"))
        cf.release_events_lock("session-b")
        self.assertNotEqual(
            cf.events_lock_path("session-a"), cf.events_lock_path("session-b")
        )

    def test_only_one_of_many_simultaneous_starters_gets_the_lock(self):
        """Read-then-write let every starter see a free lock and proceed, which
        is how you get duplicate notifications from two streams that disagree
        about what is new.

        Real forked processes, not threads, and a barrier so they all claim at
        the same instant. Each child stays alive while the others try.
        """
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        self._assert_exactly_one_winner("race")

    def test_only_one_starter_wins_when_the_previous_holder_has_died(self):
        """The other half: the lock file already exists and its last holder is
        gone, so every starter is entitled to it and they must still not all
        take it."""
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        pid = os.fork()
        if pid == 0:
            cf.claim_events_lock("dead-race")
            os._exit(0)
        os.waitpid(pid, 0)
        time.sleep(0.2)
        self.assertTrue(cf.events_lock_path("dead-race").exists())

        self._assert_exactly_one_winner("dead-race")

    def _assert_exactly_one_winner(self, owner, workers=8):
        import multiprocessing

        barrier = multiprocessing.get_context("fork").Barrier(workers)
        children = []
        for _ in range(workers):
            pid = os.fork()
            if pid == 0:
                try:
                    barrier.wait(timeout=10)
                    won = cf.claim_events_lock(owner) is None
                    # Hold well past the slowest sibling's claim. A winner that
                    # exits early frees the lock and a late child becomes a
                    # second legitimate winner, which is a flaky test, not a bug.
                    time.sleep(2.0)
                except Exception:
                    os._exit(2)
                os._exit(0 if won else 1)
            children.append(pid)

        codes = [os.waitpid(pid, 0)[1] >> 8 for pid in children]
        self.assertNotIn(2, codes, "a child failed before it could claim")
        self.assertEqual(
            codes.count(0), 1,
            f"exactly one starter may hold the lock, {codes.count(0)} did",
        )

    def test_a_real_edit_to_the_script_changes_its_fingerprint(self):
        """The loop test mocks script_fingerprint, so on its own it proves the
        loop reacts to a changed value and nothing about whether a changed file
        produces one. An earlier version recorded only whole-second mtime and
        size, so a same-size edit with the timestamp restored was invisible.
        """
        script = self.temp_path / "fake-codex-fleet"
        script.write_bytes(b"a" * 500)
        before_stat = script.stat()

        with mock.patch.object(cf, "__file__", str(script)):
            before = cf.script_fingerprint()
            # Same size, timestamp put back exactly, which is what an atomic
            # replacement or a careless restore looks like.
            script.write_bytes(b"b" * 500)
            os.utime(script, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            after = cf.script_fingerprint()

        self.assertNotEqual(before, after)

    def test_the_monitor_exits_when_the_script_it_loaded_has_changed(self):
        with mock.patch.object(cf, "script_fingerprint", side_effect=["a", "b"]):
            out = self._events()

        self.assertIn("codex-fleet was updated", out)

    def test_only_one_starter_wins_when_they_all_take_over_a_dead_lock(self):
        """The other half of the race, and the one the first test misses.

        When the lock is free, exactly one process can create it. When the lock
        exists but its holder is DEAD, every starter is entitled to take it,
        so they all overwrite it and without a confirmation step they all
        believe they hold it. The winner is whoever wrote last, and everyone
        else has to discover that by reading the file back.
        """
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        # Above pid_max, so it can never be a live process.
        cf.events_lock_path("dead-race").write_text("999999999")
        import multiprocessing

        workers = 8
        barrier = multiprocessing.get_context("fork").Barrier(workers)
        children = []
        for _ in range(workers):
            pid = os.fork()
            if pid == 0:
                try:
                    barrier.wait(timeout=10)
                    won = cf.claim_events_lock("dead-race") is None
                    time.sleep(0.4)
                except Exception:
                    os._exit(2)
                os._exit(0 if won else 1)
            children.append(pid)

        codes = [os.waitpid(pid, 0)[1] >> 8 for pid in children]
        self.assertNotIn(2, codes, "a child failed before it could claim")
        self.assertEqual(
            codes.count(0), 1,
            f"exactly one starter may take over a dead lock, {codes.count(0)} did",
        )
        self.assertNotEqual(
            cf.events_lock_path("dead-race").read_text().strip(), "999999999",
            "the dead holder must actually be displaced",
        )

    def test_deleting_the_lock_file_cannot_start_a_second_stream(self):
        """Every disk-based lock I tried could be defeated by deleting the
        file: the holder locks an inode, somebody removes the name, and the
        next monitor creates a fresh file, locks that, and both stream. The
        lock is a kernel-held name now, so the file is only a breadcrumb."""
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        self._hold_lock_in_a_child("session-a")
        self.assertTrue(cf.events_lock_path("session-a").exists())

        cf.events_lock_path("session-a").unlink()

        self.assertIsNotNone(
            cf.claim_events_lock("session-a"),
            "a second stream started because someone deleted a file",
        )

    def test_a_lock_whose_breadcrumb_is_gone_still_refuses_clearly(self):
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        self._hold_lock_in_a_child("session-a")
        cf.events_lock_path("session-a").unlink()

        with mock.patch.object(cf, "script_fingerprint", side_effect=["a", "b"]):
            out = self._events(owner="session-a")

        self.assertIn("already streaming", out)
        self.assertIn("another process", out)

    def test_a_long_session_name_does_not_make_the_first_monitor_refuse(self):
        """Abstract socket names are capped at 107 bytes. Spelling the owner
        into the name made an 89-character one fail to bind, and the caller
        read that failure as 'somebody else holds it', so the FIRST monitor
        refused itself and no notifications arrived at all."""
        cf.ROOT.mkdir(parents=True, exist_ok=True)
        for length in (36, 89, 400):
            owner = "x" * length
            self.assertIsNone(
                cf.claim_events_lock(owner),
                f"a {length}-character session name refused its own monitor",
            )
            cf.release_events_lock(owner)

    def test_the_lock_name_separates_users_and_installations(self):
        """Two installations sharing the 'anon' name would silence one of
        them."""
        first = cf.events_lock_name(None)
        with mock.patch.object(cf, "ROOT", self.temp_path / "another-home"):
            second = cf.events_lock_name(None)
        self.assertNotEqual(first, second)
        self.assertLess(len(first), 108)

    def test_the_monitor_frees_the_lock_when_it_exits(self):
        """A monitor that exits must leave the stream claimable, or re-arming
        after an update would refuse forever."""
        with mock.patch.object(cf, "script_fingerprint", side_effect=["a", "b"]):
            self._events(owner="session-a")

        self.assertIsNone(cf.claim_events_lock("session-a"))
        cf.release_events_lock("session-a")
