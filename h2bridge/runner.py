"""One execution path shared by standalone tasks and Harness² probes."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TEXT_COMPONENTS = (
    "systemprompt.md", "guardrails.md", "LongTermMEMORY.md",
    "tool_descriptions/tool_guidance.md",
)
IGNORED = {".git", "node_modules", ".venv", "__pycache__", ".DS_Store"}
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()
_cancelled = threading.Event()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def safe_relative(name: str) -> str:
    p = PurePosixPath(name)
    if not name or not p.parts or p.is_absolute() or ".." in p.parts or "\\" in name:
        raise ValueError(f"Unsafe relative path: {name!r}")
    return str(p)


def files(root: Path) -> dict[str, bytes]:
    found = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in IGNORED for part in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError(f"Symlinks are not supported in snapshots: {rel}")
        if p.is_file():
            found[rel.as_posix()] = p.read_bytes()
    return found


def fingerprint(content: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name, body in sorted(content.items()):
        h.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    return h.hexdigest()


def copy_files(content: dict[str, bytes], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, body in content.items():
        p = target / safe_relative(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)


def harness_files(root: Path) -> dict[str, bytes]:
    content = files(root)
    # Upstream adds empty component README stubs even when that component is disabled.
    content = {k: v for k, v in content.items() if k in TEXT_COMPONENTS or not k.endswith("/README.md")}
    forbidden = set(content) - set(TEXT_COMPONENTS)
    if forbidden:
        raise ValueError(f"Only four text components are editable: {sorted(forbidden)}")
    if not content.get("systemprompt.md", b"").strip():
        raise ValueError("systemprompt.md must be nonempty")
    if sum(map(len, content.values())) > 24000:
        raise ValueError("Harness exceeds the 24 KB text budget")
    return content


@dataclass(frozen=True)
class Task:
    id: str
    domain: str
    prompt: str
    input_dir: Path
    deliverables: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Task:
        raw = json.loads(path.read_text())
        if set(raw) != {"id", "domain", "prompt", "input_dir", "deliverables"}:
            raise ValueError("Task requires exactly id, domain, prompt, input_dir, deliverables")
        for key in ("id", "domain", "prompt", "input_dir"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"Task {key} must be a nonempty string")
        for key in ("id", "domain"):
            if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in raw[key]):
                raise ValueError(f"Task {key} must use letters, digits, _ or -")
        paths = raw["deliverables"]
        if not isinstance(paths, list) or not paths or not all(isinstance(x, str) for x in paths):
            raise ValueError("deliverables must be a nonempty list of relative file paths")
        source = (path.parent / raw["input_dir"]).resolve()
        if not source.is_dir():
            raise ValueError(f"Input directory does not exist: {source}")
        return cls(raw["id"], raw["domain"], raw["prompt"], source,
                   tuple(safe_relative(x) for x in paths))


def stop_process(proc: subprocess.Popen) -> None:
    # A process leader may exit before its child. Always address the whole POSIX group.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def cancel_all(*_args) -> None:
    _cancelled.set()
    with _children_lock:
        children = list(_children)
    for proc in children:
        stop_process(proc)


def run_process(argv: list[str], cwd: Path, log_dir: Path, timeout: int, env: dict) -> dict:
    if _cancelled.is_set():
        return {"status": "cancelled", "returncode": None, "wall_seconds": 0}
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (log_dir / "events.jsonl").open("w") as stdout, (log_dir / "stderr.log").open("w") as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, start_new_session=True)
        with _children_lock:
            _children.add(proc)
        try:
            if _cancelled.is_set():
                stop_process(proc)
            try:
                code = proc.wait(timeout=timeout)
                status = "completed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_process(proc)
                code, status = proc.returncode, "timed_out"
            if _cancelled.is_set():
                status = "cancelled"
        finally:
            stop_process(proc)
            with _children_lock:
                _children.discard(proc)
    return {"status": status, "returncode": code, "wall_seconds": round(time.monotonic() - started, 3)}


def parse_events(path: Path) -> dict:
    texts, errors, sessions = [], [], set()
    cost, finishes, usage = 0.0, 0, {"input": 0, "output": 0, "reasoning": 0}
    last_reason = None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            errors.append("Malformed JSON event")
            continue
        if not isinstance(ev, dict):
            errors.append("Event is not an object")
            continue
        if isinstance(ev.get("sessionID"), str) and ev["sessionID"]:
            sessions.add(ev["sessionID"])
        part = ev.get("part", {})
        if ev.get("type") == "error":
            errors.append(json.dumps(ev.get("error"), ensure_ascii=False))
        if not isinstance(part, dict):
            errors.append("Event part is not an object")
            continue
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if ev.get("type") == "step_finish":
            finishes += 1
            last_reason = part.get("reason")
            cost += float(part.get("cost", 0))
            tokens = part.get("tokens", {})
            for key in usage:
                usage[key] += int(tokens.get(key, 0))
    return {"answer": "\n".join(texts), "errors": errors, "session_ids": sorted(sessions),
            "cost_usd_reported": cost, "tokens_reported": usage,
            "terminal_complete": finishes > 0 and last_reason == "stop" and not errors}


class Runner:
    def __init__(self, task: Task, root: Path, command: list[str], model: str,
                 *, timeout: int = 300, max_runs: int = 16, fixture: bool = False,
                 provider_config: dict | None = None, executor: str = "opencode"):
        if timeout <= 0 or max_runs <= 0:
            raise ValueError("timeout and max_runs must be positive")
        if executor not in {"opencode", "codex"}:
            raise ValueError(f"Unknown executor: {executor}")
        if executor == "codex" and provider_config is not None:
            raise ValueError("--provider-config is only supported by OpenCode")
        if not model.strip() or model.startswith("-"):
            raise ValueError("An explicit model is required")
        self.executor = executor
        self.task, self.root, self.command, self.model = task, root.resolve(), command, model
        self.timeout, self.max_runs, self.fixture = timeout, max_runs, fixture
        if provider_config is not None and (set(provider_config) != {"providers"}
                                            or not isinstance(provider_config["providers"], dict)):
            raise ValueError("Provider config must contain only a providers object")
        self.provider_config = json.loads(json.dumps(provider_config or {}))
        self.config_fingerprint = hashlib.sha256(json.dumps(self.provider_config, sort_keys=True).encode()).hexdigest()
        self._lock, self._count = threading.Lock(), 0
        source = task.input_dir.resolve()
        if self.root == source or source in self.root.parents:
            raise ValueError("Output directory must be outside input_dir")
        self.root.mkdir(parents=True, exist_ok=False)
        self.input_files = files(source)
        self.input_modes = {name: (source / name).stat().st_mode & 0o777 for name in self.input_files}
        # A source config can load plugins/MCP or silently change the experiment settings.
        blocked = [p for p in self.input_files if p in {"opencode.json", "opencode.jsonc", ".env"}
                   or p.startswith((".opencode/", ".env."))]
        if executor == "codex":
            blocked += [p for p in self.input_files if any(
                part in {".codex", ".agents", "AGENTS.override.md", ".env"} or part.startswith(".env.")
                for part in Path(p).parts)]
        if blocked:
            raise ValueError(f"Prepare a clean snapshot without runtime configs or .env files: {blocked}")
        self.source_hash = hashlib.sha256((fingerprint(self.input_files)
                                          + json.dumps(self.input_modes, sort_keys=True)).encode()).hexdigest()
        copy_files(self.input_files, self.root / "source")
        for name, mode in self.input_modes.items():
            (self.root / "source" / name).chmod(mode)
        if files(source) != self.input_files or any((source / p).stat().st_mode & 0o777 != mode
                                                  for p, mode in self.input_modes.items()):
            raise ValueError("Input changed while taking its snapshot")
        write_json(self.root / "task.json", {
            "id": task.id, "domain": task.domain, "prompt": task.prompt,
            "input_fingerprint": self.source_hash, "deliverables": task.deliverables,
            "model": model, "command": command, "fixture": fixture, "executor": executor,
            "provider_config_fingerprint": self.config_fingerprint,
            "ticket_id": os.environ.get("DSH_SUPERAGENT_TICKET"),
            "timeout_seconds": timeout, "max_runs": max_runs,
        })

    def execute(self, harness: Path, run_id: str) -> Path:
        safe_relative(run_id)
        content = harness_files(harness)
        identity = fingerprint(content)
        base_instructions = self.input_files.get("AGENTS.md", b"").decode("utf-8")
        instructions = "\n\n".join(content.get(p, b"").decode("utf-8") for p in TEXT_COMPONENTS)
        combined = base_instructions + "\n\n" + instructions
        if self.executor == "codex" and len(combined.encode()) > 65536:
            raise ValueError("Combined AGENTS.md exceeds Codex's configured 64 KB budget")
        rd = self.root / "executions" / run_id
        with self._lock:
            if rd.exists():
                raise ValueError(f"Run already exists; inspect it before retrying: {run_id}")
            if self._count >= self.max_runs:
                raise RuntimeError("Execution count budget exhausted")
            self._count += 1
            rd.mkdir(parents=True)
        workspace = rd / "workspace"
        copy_files(self.input_files, workspace)
        for name, mode in self.input_modes.items():
            (workspace / name).chmod(mode)
        copy_files(content, rd / "harness")
        (workspace / "AGENTS.md").write_text(combined)
        prompt = self.task.prompt + "\n\nWork in this directory. Do not publish or contact external services. Report what changed."
        result = self.launch(workspace, rd, prompt)
        present = all((workspace / p).is_file() and not (workspace / p).is_symlink()
                      for p in self.task.deliverables)
        result.update({"run_id": run_id, "executor": self.executor, "harness_fingerprint": identity,
                       "input_fingerprint": self.source_hash, "fixture": self.fixture,
                       "provider_config_fingerprint": self.config_fingerprint,
                       "deliverables_present": present, "workspace": str(workspace)})
        (rd / "answer.txt").write_text(result.pop("answer", ""))
        changed = {}
        try:
            after = files(workspace)
        except (OSError, ValueError) as exc:
            result.update({"status": "failed", "error": f"Cannot collect artifacts: {exc}"})
            write_json(rd / "result.json", result)
            return rd
        for name in sorted(set(self.input_files) | set(after)):
            if name != "AGENTS.md" and self.input_files.get(name) != after.get(name):
                changed[name] = "deleted" if name not in after else "modified" if name in self.input_files else "added"
        result["changed_files"] = changed
        write_json(rd / "result.json", result)
        return rd

    def launch(self, workspace: Path, rd: Path, prompt: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE_") and k not in {
            "GITHUB_TOKEN", "GH_TOKEN", "SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL",
            "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
        }}
        # OpenCode 2.2.5 resolves its location from PWD before process.cwd().
        env["PWD"] = str(workspace.resolve())
        # Separate OpenCode config/state from the user's normal profile. Provider keys remain in env.
        for key, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
            env[key] = str(rd / "runtime" / folder)
        config = self.provider_config | {"permissions": [
            {"action": "external_directory", "resource": "*", "effect": "deny"},
            {"action": "webfetch", "resource": "*", "effect": "deny"},
            {"action": "websearch", "resource": "*", "effect": "deny"},
        ]}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        argv = self.command + ["run", "--standalone", "--format", "json", "--model", self.model,
                               "--agent", "build", "--auto", prompt]
        parser = parse_events
        (rd / "prompt.txt").write_text(prompt)
        started = {"status": "running", "fixture": self.fixture, "executor": self.executor}
        write_json(rd / "result.json", started)
        try:
            if self.executor == "codex":
                from .codex import prepare, parse_events as parse_codex_events
                argv, env = prepare(self.command, self.model, workspace, rd / "runtime", prompt)
                parser = parse_codex_events
            state = run_process(argv, workspace, rd, self.timeout, env)
            stream = rd / "events.jsonl"
            # Cancellation can arrive before Popen creates the stream.
            events = ({"answer": "", "terminal_complete": False}
                      if state["status"] == "cancelled" and not stream.exists() else parser(stream))
            if state["status"] == "completed" and not events["terminal_complete"]:
                state["status"] = "failed"
            return state | events | {"executor": self.executor}
        except Exception as exc:
            return {"status": "failed", "executor": self.executor,
                    "error": f"{type(exc).__name__}: {exc}", "answer": ""}


def check_run(run_dir: Path, check_file: Path, timeout: int = 30) -> dict:
    """An operator-owned check runs only after the solver has exited."""
    import sys
    result = json.loads((run_dir / "result.json").read_text())
    check_file = check_file.resolve()
    report = {"passed": False, "check_sha256": hashlib.sha256(check_file.read_bytes()).hexdigest(),
              "run_id": result["run_id"], "fixture": result["fixture"],
              "harness_fingerprint": result["harness_fingerprint"]}
    if result["status"] != "completed" or not result["deliverables_present"]:
        report["reason"] = "Execution did not complete with all deliverables"
    else:
        checked = run_process([sys.executable, "-I", str(check_file)], Path(result["workspace"]),
                              run_dir / "check", timeout, {"PATH": os.defpath})
        report.update(checked)
        report["passed"] = checked["status"] == "completed"
    write_json(run_dir / "check.json", report)
    return report
