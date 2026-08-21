import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
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

    def test_status_hook_surfaces_each_outbox_message_once_and_marks_it_read(self):
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

        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("worker BLOCKING: need a decision", context)
        stored = json.loads(outbox.read_text(encoding="utf-8"))
        self.assertTrue(stored["read"])
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertEqual(second.stderr, "")

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
