"""Codex exec adapter; no personal login database or persistent threads."""
from __future__ import annotations

import json
import os
from pathlib import Path


def prepare(command: list[str], model: str, workspace: Path, runtime: Path,
            prompt: str) -> tuple[list[str], dict[str, str]]:
    # Explicit automation credentials only. Never inherit the app's CODEX_* state.
    keep = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_API_KEY"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    for key, folder in (("HOME", "home"), ("CODEX_HOME", "codex"), ("TMPDIR", "tmp"),
                        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        directory = runtime / folder
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory.resolve())
    env["PWD"] = str(workspace.resolve())
    # Stop project instruction/config discovery at the isolated workspace.
    (workspace / ".git").mkdir(exist_ok=True)
    argv = command + ["exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                      "--skip-git-repo-check", "--color", "never", "--model", model,
                      "--sandbox", "workspace-write", "-C", str(workspace)]
    config = {
        "approval_policy": '"never"',
        "cli_auth_credentials_store": '"file"',
        "model_provider": '"openai"',
        "web_search": '"disabled"',
        "project_doc_max_bytes": "65536",
        "sandbox_workspace_write.network_access": "false",
        "shell_environment_policy.inherit": '"core"',
        "shell_environment_policy.ignore_default_excludes": "false",
        "shell_environment_policy.experimental_use_profile": "false",
    }
    for key, value in config.items():
        argv += ["-c", f"{key}={value}"]
    return argv + ["--", prompt], env


def parse_events(path: Path) -> dict:
    texts, errors, sessions = [], [], set()
    usage = {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0}
    mapping = {"input": "input_tokens", "cached_input": "cached_input_tokens",
               "output": "output_tokens", "reasoning": "reasoning_output_tokens"}
    turn_open, completed, reported = False, 0, True
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append("Malformed JSON event")
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            errors.append("Event must be an object with a type")
            continue
        kind = event["type"]
        if kind == "thread.started":
            thread = event.get("thread_id")
            if isinstance(thread, str) and thread:
                sessions.add(thread)
            else:
                errors.append("Missing thread_id")
        elif kind == "turn.started":
            if turn_open:
                errors.append("Turn started before previous turn finished")
            turn_open = True
        elif kind in {"error", "turn.failed"}:
            errors.append(json.dumps(event.get("error", event.get("message", kind)), ensure_ascii=False))
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                errors.append("Invalid completed item")
            elif item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            # A failed tool can be recovered by the agent; only turn errors are fatal here.
        elif kind == "turn.completed":
            if not turn_open:
                errors.append("Completed turn without turn.started")
            turn_open = False
            completed += 1
            tokens = event.get("usage")
            if not isinstance(tokens, dict):
                reported = False
                continue
            for key, field in mapping.items():
                value = tokens.get(field)
                if type(value) is not int or value < 0:
                    reported = False
                    if value is not None:
                        errors.append(f"Invalid token count: {field}")
                else:
                    usage[key] += value
    return {"answer": "\n".join(texts), "errors": errors, "session_ids": sorted(sessions),
            "cost_usd_reported": None, "tokens_reported": usage,
            "usage_reported": completed > 0 and reported,
            "terminal_complete": bool(sessions) and completed > 0 and not turn_open and not errors}
