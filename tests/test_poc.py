import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from h2bridge.demo import demo
from h2bridge.runner import Runner, Task, check_run, files, fingerprint, harness_files, parse_events, run_process, safe_relative

PROJECT = Path(__file__).resolve().parents[1]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = Task.load(PROJECT / "examples/signed-total/task.json")
        self.base = PROJECT / "examples/base-harness"

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, source=None, max_runs=4):
        script = self.root / "executor.py"
        if source is None:
            script = PROJECT / "h2bridge/fixture_executor.py"
        else:
            script.write_text(source)
        return Runner(self.task, self.root / "runs", [sys.executable, str(script)],
                      "fixture/test", fixture=True, timeout=2, max_runs=max_runs)

    def test_real_harness2_recursion_and_shared_entry_replay(self):
        report = demo(self.root / "demo")
        self.assertEqual((report["rollouts"], report["improvement_stages"]), (4, 3))
        self.assertFalse(report["base_passed"])
        self.assertTrue(report["candidate_passed"])
        self.assertTrue(report["shared_runner_replay_passed"])
        self.assertFalse(report["real_model_used"])

    def test_solver_cannot_modify_original_snapshot(self):
        before = fingerprint(files(self.task.input_dir))
        runner = self.runner()
        candidate = self.root / "harness"
        candidate.mkdir()
        (candidate / "systemprompt.md").write_text("Include both positive and negative amounts")
        rd = runner.execute(candidate, "one")
        self.assertTrue(check_run(rd, PROJECT / "examples/signed-total/check.py")["passed"])
        self.assertEqual(before, fingerprint(files(self.task.input_dir)))
        self.assertNotIn("check.py", files(rd / "workspace"))

    def test_cli_success_with_error_event_is_failure(self):
        runner = self.runner('print(\'{"type":"error","error":{"message":"provider failed"}}\')')
        rd = runner.execute(self.base, "one")
        result = json.loads((rd / "result.json").read_text())
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["status"], "failed")

    def test_partial_output_without_terminal_event_is_failure(self):
        runner = self.runner('print(\'{"type":"text","part":{"text":"Looks done"}}\')')
        rd = runner.execute(self.base, "one")
        self.assertEqual(json.loads((rd / "result.json").read_text())["status"], "failed")

    def test_missing_binary_is_recorded_as_failure(self):
        runner = Runner(self.task, self.root / "runs", ["/no/such/opencode"], "fixture/test")
        rd = runner.execute(self.base, "one")
        self.assertEqual(json.loads((rd / "result.json").read_text())["status"], "failed")

    def test_opencode_pwd_points_at_the_isolated_workspace(self):
        runner = self.runner("import os\nfrom pathlib import Path\nassert Path(os.environ['PWD']).resolve() == Path.cwd()\n"
                             "print('{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}')")
        rd = runner.execute(self.base, "one")
        self.assertEqual(json.loads((rd / "result.json").read_text())["status"], "completed")

    def test_uncollectable_artifacts_are_not_left_running(self):
        runner = self.runner("from pathlib import Path\nPath('unexpected').symlink_to('/tmp')\n"
                             "print('{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}')")
        rd = runner.execute(self.base, "one")
        result = json.loads((rd / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("Cannot collect artifacts", result["error"])

    def test_worker_requires_a_ticket_and_enforces_its_directory(self):
        env = dict(os.environ)
        env.pop("DSH_SUPERAGENT_TICKET", None)
        env.pop("DSH_SUPERAGENT_DIR", None)
        argv = [sys.executable, "-m", "h2bridge", "worker", "--task",
                str(PROJECT / "examples/signed-total/task.json"), "--harness", str(self.base),
                "--output", str(self.root / "worker"), "--model", "fixture/test"]
        result = subprocess.run(argv, cwd=PROJECT, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires DSH_SUPERAGENT_TICKET", result.stderr)
        env.update(DSH_SUPERAGENT_TICKET="test-ticket", DSH_SUPERAGENT_DIR=str(self.root))
        result = subprocess.run(argv, cwd=PROJECT, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside this Ticket", result.stderr)

    def test_worker_cli_executes_with_ticket_identity(self):
        wrapper = self.root / "fixture-opencode"
        wrapper.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(PROJECT / 'h2bridge/fixture_executor.py')!r})\n")
        wrapper.chmod(0o755)
        env = dict(os.environ, DSH_SUPERAGENT_TICKET="T-001", DSH_SUPERAGENT_DIR=str(self.task.input_dir))
        output = self.root / "worker"
        result = subprocess.run([sys.executable, "-m", "h2bridge", "worker", "--task",
                str(PROJECT / "examples/signed-total/task.json"), "--harness", str(self.base),
                "--output", str(output), "--model", "fixture/test", "--opencode", str(wrapper)],
                cwd=PROJECT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ticket_id"], "T-001")
        self.assertEqual(json.loads((output / "task.json").read_text())["ticket_id"], "T-001")

    def test_timeout_terminates_grandchildren(self):
        marker = self.root / "escaped"
        child = self.root / "child.py"
        child.write_text(f"import time\nfrom pathlib import Path\ntime.sleep(2)\nPath({str(marker)!r}).write_text('bad')")
        parent = self.root / "parent.py"
        parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(10)")
        outcome = run_process([sys.executable, str(parent)], self.root, self.root / "logs", 0.2, dict(os.environ))
        self.assertEqual(outcome["status"], "timed_out")
        time.sleep(2.1)
        self.assertFalse(marker.exists())

    def test_sigterm_cancels_cli_and_records_result(self):
        slow = self.root / "slow-opencode"
        slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
        slow.chmod(0o755)
        output = self.root / "cancel"
        proc = subprocess.Popen([sys.executable, "-m", "h2bridge", "run", "--task",
            str(PROJECT / "examples/signed-total/task.json"), "--harness", str(self.base),
            "--output", str(output), "--model", "fixture/test", "--opencode", str(slow)],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            state = output / "executions/task/result.json"
            deadline = time.monotonic() + 5
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(state.exists())
            proc.terminate()
            proc.communicate(timeout=5)
            self.assertEqual(json.loads(state.read_text())["status"], "cancelled")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_paths_and_symlink_snapshots_are_rejected(self):
        for p in (".", "../escape", "/absolute", "a/../../escape", "a\\b"):
            with self.subTest(path=p), self.assertRaises(ValueError):
                safe_relative(p)
        (self.root / "link").symlink_to(self.task.input_dir / "ledger.py")
        with self.assertRaises(ValueError):
            files(self.root)

    def test_execution_budget_and_duplicate_run_do_not_reexecute(self):
        runner = self.runner(max_runs=1)
        runner.execute(self.base, "one")
        with self.assertRaises(ValueError):
            runner.execute(self.base, "one")
        with self.assertRaises(RuntimeError):
            runner.execute(self.base, "two")

    def test_executable_harness_components_are_rejected(self):
        candidate = self.root / "harness"
        candidate.mkdir()
        (candidate / "systemprompt.md").write_text("Instructions")
        (candidate / "scripts").mkdir()
        (candidate / "scripts/unsafe.py").write_text("raise Exception('unsafe')")
        with self.assertRaises(ValueError):
            harness_files(candidate)

    def test_malformed_events_cannot_claim_completion(self):
        event = self.root / "events.jsonl"
        event.write_text('not json\n{"type":"step_finish","part":{"reason":"stop"}}\n')
        self.assertFalse(parse_events(event)["terminal_complete"])

    def test_existing_output_directory_is_not_reused(self):
        runner = self.runner()
        with self.assertRaises(FileExistsError):
            Runner(self.task, runner.root, runner.command, runner.model)


if __name__ == "__main__":
    unittest.main()
