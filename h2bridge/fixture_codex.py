"""Scripted Codex CLI protocol double. Never calls Codex or a model."""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
assert args[0] == "exec" and "--json" in args and "--ephemeral" in args
assert "--ignore-user-config" in args and "--ignore-rules" in args
assert args[args.index("--sandbox") + 1] == "workspace-write"
assert "approval_policy=\"never\"" in args
assert not any("dangerously" in arg for arg in args)
assert Path(args[args.index("-C") + 1]).resolve() == Path.cwd()
runtime = Path(os.environ["CODEX_HOME"]).parent
assert Path(os.environ["HOME"]).parent == runtime
assert Path(os.environ["PWD"]).resolve() == Path.cwd()
assert (Path.cwd() / ".git").is_dir()
(runtime / "fixture-contract.json").write_text(json.dumps({
    "executor": "codex", "model": args[args.index("--model") + 1],
    "isolated_profile": True, "workspace_correct": True, "fixture": True,
}))

editor = Path("context/harness/systemprompt.md")
if editor.exists():
    editor.write_text(editor.read_text() + "\nInclude both positive and negative amounts.\n")
    changed = str(editor)
    answer = "<notes>Preserve both signs in the sum.</notes>"
else:
    target = Path("ledger.py")
    if "Include both positive and negative amounts" in Path("AGENTS.md").read_text():
        target.write_text(target.read_text().replace(" if Decimal(x) > 0", ""))
    changed = str(target)
    answer = "Fixture finished. Inspect ledger.py and run independent checks."

for event in (
    {"type": "thread.started", "thread_id": "fixture-codex-thread"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "file-1", "type": "file_change", "status": "completed",
                                         "changes": [{"path": changed, "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": answer}},
    {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 5,
                                          "output_tokens": 8, "reasoning_output_tokens": 2}},
):
    print(json.dumps(event))
