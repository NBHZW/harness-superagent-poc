"""Adapt our task runner to Google's unchanged Harness² recursion core."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from harness2.core.proposer import Proposer, ProposerError, Reply
from harness2.core.schedule import run_task
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
from harness2.substrate.opencode import OpencodeSubstrate
from harness2.substrate.codex import CodexSubstrate

from .runner import Runner, TEXT_COMPONENTS, copy_files, fingerprint, harness_files, write_json

UPSTREAM_COMMIT = "6868817518876de8fd170c6a8354daca273b6191"


class TextSubstrate(OpencodeSubstrate):
    """Keep the same four-file search surface; render each CLI's native trajectory."""
    def __init__(self, executor: str):
        super().__init__()
        self.name = "cx" if executor == "codex" else "oc"
        self.renderer = CodexSubstrate() if executor == "codex" else OpencodeSubstrate()

    def read_trajectory(self, stream_path, **kwargs):
        return self.renderer.read_trajectory(stream_path, **kwargs)


class Adapter:
    def __init__(self, runner: Runner, base: Path):
        self.runner = runner
        self.substrate = TextSubstrate(runner.executor)
        # Upstream's channel enum has no Codex. ExecutorProposer overrides all I/O;
        # this compatibility descriptor never dispatches an OpenCode process.
        channel = ChannelSpec("opencode", runner.model, timeout=runner.timeout)
        self.spec = BenchSpec(
            name="localcoding", runs_root=runner.root / "harness2", install_mode="workspace_agents_md",
            prompts=BenchPrompts(
                work="complete a bounded coding task in a local source snapshot",
                inv_levers="Improve task analysis, implementation checks and failure handling through text instructions.",
                dom_levers="Improve domain-specific implementation guidance through text instructions.",
            ), channels={"proposer": channel, "selector": channel}, allow_subagents=False,
        )
        self.base = runner.root / "base_harness"
        copy_files(harness_files(base), self.base)

    def tasks(self, split="all"):
        return [self.runner.task.id]

    def domain_of(self, task_id):
        return self.runner.task.domain

    def task_prompt(self, task_id):
        return self.runner.task.prompt

    def task_files(self, task_id):
        return self.runner.root / "source"

    def base_harness(self):
        return self.base

    def validate_tree(self, tree):
        forbidden = [p for p in tree if p not in TEXT_COMPONENTS and not p.endswith("/README.md")]
        if forbidden:
            raise ValueError(f"This experiment only edits text guidance, not executable components: {forbidden}")

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        if task_id != self.runner.task.id:
            raise ValueError("Unknown task")
        rd = self.runner.execute(harness_dir or self.base, run_id)
        result = json.loads((rd / "result.json").read_text())
        if result["status"] != "completed":
            raise RuntimeError(f"{result['status']}; inspect {rd / 'result.json'}")
        return rd

    def delivered(self, run_dir):
        result = json.loads((run_dir / "result.json").read_text())
        return result["status"] == "completed" and result["deliverables_present"]

    def trajectory(self, run_dir, *, obs_trunc):
        return self.substrate.read_trajectory(run_dir / "events.jsonl", obs_trunc=obs_trunc, max_chars=100000)

    def deliverable_files(self, run_dir):
        return {name: (run_dir / "workspace" / name).read_text(errors="replace")[:60000]
                for name in self.runner.task.deliverables if (run_dir / "workspace" / name).is_file()}

    def deliverable_text(self, run_dir):
        return "\n\n".join(f"## {name}\n{body}" for name, body in self.deliverable_files(run_dir).items())

    def score(self, run_dir, task_id):
        # Independent checks are invoked by the operator after selection, never in the optimizer.
        return None


class ExecutorProposer(Proposer):
    """Use the same installed CLI for editing and solving, rather than upstream's Bun checkout."""
    def __init__(self, runner: Runner, spec: ChannelSpec):
        super().__init__(spec)
        self.runner, self.lock, self.count = runner, threading.Lock(), 0

    def call(self, mission, context_dir, label, *, system="", timeout=None, max_attempts=None, harvest=None):
        with self.lock:
            if self.count >= 6:
                raise ProposerError("Editor invocation budget exhausted", attempts=0)
            self.count += 1
            rd = self.runner.root / "editors" / f"{self.count:02d}"
        workspace = rd / "workspace"
        workspace.mkdir(parents=True)
        shutil.copytree(context_dir, workspace / "context")
        prompt = system + "\n" + mission + (
            "\nOnly edit the four text files inside context/harness: systemprompt.md, guardrails.md, "
            "LongTermMEMORY.md and tool_descriptions/tool_guidance.md. "
            "Do not edit source files, add plugins/scripts, or change permissions. Return the requested notes."
        )
        outcome = self.runner.launch(workspace, rd, prompt)
        write_json(rd / "result.json", outcome)
        if outcome["status"] != "completed":
            raise ProposerError(f"Editor {label} failed; inspect {rd}", attempts=1)
        if not outcome["answer"].strip():
            raise ProposerError(f"Editor {label} returned no notes", attempts=1)
        reply = Reply(label=label, text=outcome["answer"], ok=True, attempts=1,
                      cost_usd=outcome["cost_usd_reported"] or 0.0, wall_s=outcome["wall_seconds"],
                      workspace=workspace / harvest if harvest else None, audit=rd, channel=self.runner.executor)
        with self.lock:
            self._calls.append(reply)
        return reply


def optimize(runner: Runner, base: Path, proposer: Proposer | None = None) -> dict:
    adapter = Adapter(runner, base)
    prop = proposer or ExecutorProposer(runner, adapter.spec.channels["proposer"])
    result = run_task(runner.task.id, 1, adapter=adapter, prop=prop,
                      runs_root=adapter.spec.runs_root, modes=("agg",), seed=0)
    report = result.to_dict()
    report.update({"harness2_commit": UPSTREAM_COMMIT, "fixture": runner.fixture, "executor": runner.executor,
                   "publication": "candidate_only", "editor_invocations": len(prop._calls)})
    # Upstream's generic RunView does not ingest solver prices; retain our native accounting separately.
    report["native_usage"] = {}
    for group in ("executions", "editors"):
        records = [json.loads(p.read_text()) for p in (runner.root / group).rglob("result.json")]
        report["native_usage"][group] = {
            "runs": len(records),
            "cost_usd_reported": (sum(r["cost_usd_reported"] for r in records)
                                  if records and all(r.get("cost_usd_reported") is not None for r in records) else None),
            "tokens_reported": {key: sum(r.get("tokens_reported", {}).get(key, 0) for r in records)
                                for key in ("input", "cached_input", "output", "reasoning")},
            "all_runs_reported_usage": bool(records) and all(r.get("usage_reported", "tokens_reported" in r) for r in records),
        }
    if result.status == "ok":
        selected = result.selections["agg"]
        chosen = Path(selected["chosen_run_dir"])
        content = harness_files(chosen / "harness")
        copy_files(content, runner.root / "candidate_harness")
        report["candidate_fingerprint"] = fingerprint(content)
        report["selected_run_dir"] = str(chosen)
    write_json(runner.root / "optimization.json", report)
    return report
