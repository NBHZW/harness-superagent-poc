"""Repeatable wiring check using Google's actual recursion core and scripted actors."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from harness2.core.proposer import Proposer, Reply
from harness2.core.spec import ChannelSpec

from .optimize import optimize
from .runner import Runner, Task, check_run, write_json


class FixtureProposer(Proposer):
    def __init__(self):
        super().__init__(ChannelSpec("opencode", "fixture/deterministic"))
        self.lock = threading.Lock()

    def call(self, mission, context_dir, label, **kwargs):
        harness = Path(context_dir) / "harness"
        p = harness / "systemprompt.md"
        p.write_text(p.read_text() + "\nInclude both positive and negative amounts.\n")
        reply = Reply(label=label, text="<notes>Preserve both signs in the sum.</notes>",
                      ok=True, attempts=1, workspace=harness)
        with self.lock:
            self._calls.append(reply)
        return reply


def demo(root: Path, executor: str = "opencode") -> dict:
    project = Path(__file__).resolve().parent.parent
    fixture = Path(__file__).with_name("fixture_codex.py" if executor == "codex" else "fixture_executor.py")
    task = Task.load(project / "examples/signed-total/task.json")
    runner = Runner(task, root, [sys.executable, str(fixture)], "fixture/deterministic",
                    fixture=True, timeout=10, max_runs=5, executor=executor)
    outcome = optimize(runner, project / "examples/base-harness",
                       None if executor == "codex" else FixtureProposer())
    if outcome["status"] != "ok":
        raise RuntimeError(f"Harness² fixture failed: {outcome['errors']}")
    base = Path(outcome["base"][0]["run_dir"])
    chosen = Path(outcome["selected_run_dir"])
    check = project / "examples/signed-total/check.py"
    before, after = check_run(base, check), check_run(chosen, check)
    # Re-run the adopted text through the same ordinary-task entry point.
    replay = runner.execute(runner.root / "candidate_harness", "worker-replay")
    replay_check = check_run(replay, check)
    ledger = outcome["modes"]["agg"]["ledger"]
    assert ledger["R"] == 4 and ledger["P"] == 3, ledger
    assert not before["passed"] and after["passed"] and replay_check["passed"]
    summary = {"status": "passed", "fixture": True, "real_model_used": False, "executor": executor,
               "real_agent_binary_used": False,
               "harness2_commit": outcome["harness2_commit"], "rollouts": ledger["R"],
               "improvement_stages": ledger["P"], "base_passed": before["passed"],
               "candidate_passed": after["passed"], "shared_runner_replay_passed": replay_check["passed"],
               "dsh_host_verified": False, "candidate_fingerprint": outcome["candidate_fingerprint"],
               "quality_claim": "None: executor and editor are deterministic fixtures"}
    write_json(runner.root / "demo-report.json", summary)
    return summary
