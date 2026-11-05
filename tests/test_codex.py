import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from h2bridge.codex import parse_events, prepare
from h2bridge.demo import demo
from h2bridge.optimize import Adapter
from h2bridge.runner import Runner, Task, files, fingerprint

PROJECT = Path(__file__).resolve().parents[1]
START = [{"type": "thread.started", "thread_id": "test-thread"}, {"type": "turn.started"}]
FINISH = {"type": "turn.completed", "usage": {
    "input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 7, "reasoning_output_tokens": 3}}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.task = Task.load(PROJECT / "examples/signed-total/task.json")
        self.base = PROJECT / "examples/base-harness"

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, events):
        log = self.root / "events.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in events))
        return parse_events(log)

    def test_tokens_final_messages_and_recoverable_tool_failure(self):
        events = START + [
            {"type": "item.started", "item": {"type": "agent_message", "text": "partial"}},
            {"type": "item.completed", "item": {"type": "command_execution", "status": "failed", "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Recovered and finished"}}, FINISH]
        result = self.parse(events)
        self.assertTrue(result["terminal_complete"])
        self.assertTrue(result["usage_reported"])
        self.assertEqual(result["answer"], "Recovered and finished")
        self.assertEqual(result["session_ids"], ["test-thread"])
        self.assertEqual(result["tokens_reported"], {"input": 12, "cached_input": 4, "output": 7, "reasoning": 3})
        self.assertIsNone(result["cost_usd_reported"])

    def test_partial_failed_and_invalid_streams_never_pass(self):
        for events in (
            START, [FINISH], START + [FINISH, {"type": "turn.started"}],
            START + [{"type": "error", "message": "authentication failed"}, FINISH],
            START + [{"type": "turn.failed", "error": {"message": "provider failed"}}, FINISH],
            START + [None, FINISH], START + [{"type": "item.completed", "item": None}, FINISH],
            START + [{"type": "turn.completed", "usage": {"input_tokens": -1}}],
            START + [{"type": "turn.completed", "usage": {"output_tokens": "7"}}],
        ):
            with self.subTest(events=events):
                self.assertFalse(self.parse(events)["terminal_complete"])
        log = self.root / "invalid.jsonl"
        log.write_text("not json\n")
        self.assertFalse(parse_events(log)["terminal_complete"])

    def test_missing_usage_is_unknown_instead_of_a_free_run(self):
        result = self.parse(START + [{"type": "turn.completed"}])
        self.assertTrue(result["terminal_complete"])
        self.assertFalse(result["usage_reported"])
        self.assertIsNone(result["cost_usd_reported"])

    def test_launch_does_not_inherit_personal_profile_or_other_credentials(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        profile = self.root / "personal-profile"
        profile.mkdir()
        (profile / "auth.json").write_text("sentinel: never read or copied")
        original = fingerprint(files(profile))
        with patch.dict(os.environ, {
            "CODEX_HOME": str(profile), "HOME": str(profile), "OPENAI_BASE_URL": "https://unexpected.invalid",
            "CODEX_API_KEY": "test-only-placeholder", "OPENAI_API_KEY": "must-not-inherit",
            "CODEX_THREAD_ID": "personal-thread", "GH_TOKEN": "must-not-inherit",
        }):
            argv, env = prepare(["codex"], "explicit-model", workspace, self.root / "runtime", "--literal-prompt")
        self.assertEqual(argv[-2:], ["--", "--literal-prompt"])
        self.assertEqual(env["CODEX_API_KEY"], "test-only-placeholder")
        self.assertEqual(set(k for k in env if k.startswith("CODEX_")), {"CODEX_HOME", "CODEX_API_KEY"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotEqual(Path(env["HOME"]), profile)
        self.assertFalse((Path(env["CODEX_HOME"]) / "auth.json").exists())
        self.assertEqual(original, fingerprint(files(profile)))

    def test_codex_runs_the_real_harness2_solver_and_editor_flow(self):
        before = fingerprint(files(self.task.input_dir))
        report = demo(self.root / "demo", executor="codex")
        self.assertEqual((report["rollouts"], report["improvement_stages"]), (4, 3))
        self.assertTrue(report["shared_runner_replay_passed"])
        self.assertFalse(report["real_agent_binary_used"])
        self.assertEqual(before, fingerprint(files(self.task.input_dir)))
        outcome = json.loads((self.root / "demo/optimization.json").read_text())
        self.assertEqual(outcome["executor"], "codex")
        self.assertEqual(outcome["editor_invocations"], 3)
        for group, count in (("executions", 4), ("editors", 3)):
            self.assertEqual(outcome["native_usage"][group]["runs"], count)
            self.assertTrue(outcome["native_usage"][group]["all_runs_reported_usage"])
            self.assertIsNone(outcome["native_usage"][group]["cost_usd_reported"])
        contracts = list((self.root / "demo").rglob("fixture-contract.json"))
        self.assertEqual(len(contracts), 8)  # four solvers, three editors, one replay
        for contract in contracts:
            self.assertEqual(json.loads(contract.read_text())["executor"], "codex")

    def test_native_trajectory_includes_tools_and_observations(self):
        runner = Runner(self.task, self.root / "runs", [sys.executable, str(PROJECT / "h2bridge/fixture_codex.py")],
                        "test-model", fixture=True, executor="codex")
        adapter = Adapter(runner, self.base)
        rd = runner.execute(self.base, "trace")
        trajectory = adapter.trajectory(rd, obs_trunc=10000)
        self.assertIn("ledger.py", trajectory)
        self.assertIn("Fixture finished", trajectory)
        self.assertEqual(adapter.substrate.name, "cx")

    def test_error_exit_and_timeout_are_recorded(self):
        for i, source in enumerate((
            "print('{\"type\":\"error\",\"message\":\"failed\"}')",
            "import sys\nprint('{\"type\":\"turn.completed\"}')\nsys.exit(3)",
            "import time\ntime.sleep(10)",
        )):
            with self.subTest(case=i):
                script = self.root / f"script-{i}.py"
                script.write_text(source)
                runner = Runner(self.task, self.root / f"runs-{i}", [sys.executable, str(script)],
                                "test-model", timeout=0.2, executor="codex", fixture=True)
                rd = runner.execute(self.base, "failed")
                result = json.loads((rd / "result.json").read_text())
                self.assertEqual(result["status"], "timed_out" if i == 2 else "failed")

    def test_codex_rejects_runtime_configs_and_opencode_provider_options(self):
        with self.assertRaisesRegex(ValueError, "only supported by OpenCode"):
            Runner(self.task, self.root / "bad-provider", ["codex"], "test-model",
                   executor="codex", provider_config={"providers": {}})
        for i, name in enumerate((".codex/config.toml", "sub/.agents/skills/test/SKILL.md", "AGENTS.override.md")):
            with self.subTest(path=name):
                source = self.root / f"source-{i}"
                target = source / name
                target.parent.mkdir(parents=True)
                target.write_text("unexpected runtime configuration")
                task = Task("test", "coding", "test", source, ("result.txt",))
                with self.assertRaisesRegex(ValueError, "clean snapshot"):
                    Runner(task, self.root / f"blocked-{i}", ["codex"], "test-model", executor="codex")

    def test_instruction_limit_fails_before_spending_an_execution(self):
        source = self.root / "large-instructions"
        source.mkdir()
        (source / "AGENTS.md").write_text("a" * 65536)
        task = Task("large", "coding", "test", source, ("result.txt",))
        runner = Runner(task, self.root / "runs", ["unused"], "test-model", executor="codex")
        with self.assertRaisesRegex(ValueError, "64 KB budget"):
            runner.execute(self.base, "too-large")
        self.assertFalse((runner.root / "executions").exists())

    def test_codex_sigterm_cancels_without_waiting_for_a_terminal_event(self):
        slow = self.root / "slow-codex"
        slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
        slow.chmod(0o755)
        output = self.root / "cancelled"
        proc = subprocess.Popen([sys.executable, "-m", "h2bridge", "run", "--executor", "codex",
            "--codex", str(slow), "--task", str(PROJECT / "examples/signed-total/task.json"),
            "--harness", str(self.base), "--output", str(output), "--model", "test-model"],
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

    def test_cancellation_before_process_start_remains_cancelled(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("h2bridge.runner._cancelled", cancelled):
            runner = Runner(self.task, self.root / "pre-cancelled", ["unused"], "test-model", executor="codex")
            rd = runner.execute(self.base, "one")
        self.assertEqual(json.loads((rd / "result.json").read_text())["status"], "cancelled")
        self.assertFalse((rd / "events.jsonl").exists())

    def test_worker_cli_and_executor_option_validation(self):
        wrapper = self.root / "fixture-codex"
        wrapper.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(PROJECT / 'h2bridge/fixture_codex.py')!r})\n")
        wrapper.chmod(0o755)
        args = [sys.executable, "-m", "h2bridge", "worker", "--executor", "codex", "--codex", str(wrapper),
                "--task", str(PROJECT / "examples/signed-total/task.json"), "--harness", str(self.base),
                "--output", str(self.root / "worker"), "--model", "bare-model-id"]
        env = dict(os.environ, DSH_SUPERAGENT_TICKET="CX-001", DSH_SUPERAGENT_DIR=str(self.task.input_dir))
        result = subprocess.run(args, env=env, cwd=PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data["executor"], data["ticket_id"]), ("codex", "CX-001"))
        bad = subprocess.run(args + ["--opencode", "/unused"], env=env, cwd=PROJECT, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("options do not match", bad.stderr)


if __name__ == "__main__":
    unittest.main()
