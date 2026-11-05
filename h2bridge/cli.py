from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import sys
from pathlib import Path

from .runner import Runner, Task, cancel_all, check_run


def default_executable(name: str) -> str:
    project = Path(__file__).resolve().parent.parent
    candidate = project / "node_modules/.bin" / name
    return str(candidate) if candidate.is_file() else name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OpenCode / Codex execution for Harness² and DSH workers")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Check local prerequisites without model calls")
    demo = sub.add_parser("demo", help="Deterministic fixture with the real Harness² core; no LLM")
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument("--executor", choices=("opencode", "codex"), default="opencode")
    for name in ("run", "worker", "optimize"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--task", required=True, type=Path)
        cmd.add_argument("--harness", required=True, type=Path)
        cmd.add_argument("--output", required=True, type=Path)
        cmd.add_argument("--model", required=True, help="OpenCode: provider/model; Codex: model ID")
        cmd.add_argument("--executor", choices=("opencode", "codex"), default="opencode")
        cmd.add_argument("--opencode", help="OpenCode executable path")
        cmd.add_argument("--codex", help="Codex executable path")
        cmd.add_argument("--provider-config", type=Path, help="OpenCode V2 JSON with only a providers object")
        cmd.add_argument("--timeout", type=int, default=300)
    check = sub.add_parser("check", help="Run an operator-owned Python check after execution")
    check.add_argument("--run", type=Path, required=True)
    check.add_argument("--check-file", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, cancel_all)
    signal.signal(signal.SIGINT, cancel_all)
    try:
        if args.action == "doctor":
            data = {"python": sys.version.split()[0], "harness2_installed": importlib.util.find_spec("harness2") is not None,
                    "opencode": shutil.which(default_executable("opencode")),
                    "codex": shutil.which(default_executable("codex")), "dsh": shutil.which("dsh"),
                    "provider_key_present": any(os.environ.get(k) for k in (
                        "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")),
                    "checks_call_models": False}
        elif args.action == "demo":
            from .demo import demo as run_demo
            data = run_demo(args.output, executor=args.executor)
        elif args.action == "check":
            data = check_run(args.run.resolve(), args.check_file)
        elif args.action == "status":
            data = {str(p.relative_to(args.directory)): json.loads(p.read_text())
                    for p in args.directory.rglob("result.json")}
        else:
            task = Task.load(args.task.resolve())
            if args.action == "worker":
                ticket = os.environ.get("DSH_SUPERAGENT_TICKET")
                directory = os.environ.get("DSH_SUPERAGENT_DIR")
                if not ticket or not directory:
                    raise ValueError("worker requires DSH_SUPERAGENT_TICKET and DSH_SUPERAGENT_DIR")
                ticket_dir = Path(directory).resolve()
                if task.input_dir != ticket_dir and ticket_dir not in task.input_dir.parents:
                    raise ValueError("Task input_dir must be inside this Ticket's workspace")
            if (args.executor == "codex" and (args.opencode or args.provider_config)) or (
                    args.executor == "opencode" and args.codex):
                raise ValueError("Executor-specific options do not match --executor")
            binary = getattr(args, args.executor) or default_executable(args.executor)
            executable = shutil.which(binary)
            if not executable:
                raise ValueError(f"{args.executor} executable not found: {binary}")
            if args.executor == "opencode" and ("/" not in args.model or not all(args.model.split("/", 1))):
                raise ValueError("OpenCode requires an explicit provider/model")
            runner = Runner(task, args.output, [executable], args.model,
                            timeout=args.timeout, max_runs=4 if args.action == "optimize" else 1,
                            provider_config=json.loads(args.provider_config.read_text()) if args.provider_config else None,
                            executor=args.executor)
            if args.action in {"run", "worker"}:
                rd = runner.execute(args.harness.resolve(), "task")
                data = json.loads((rd / "result.json").read_text())
                data["run_dir"] = str(rd)
                data["ticket_id"] = os.environ.get("DSH_SUPERAGENT_TICKET")
            else:
                from .optimize import optimize
                data = optimize(runner, args.harness.resolve())
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if args.action == "check":
            return 0 if data["passed"] else 1
        if args.action in {"run", "worker", "optimize"}:
            return 0 if data["status"] in {"completed", "ok"} else 1
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
