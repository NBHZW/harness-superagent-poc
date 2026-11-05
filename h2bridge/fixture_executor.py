"""Deterministic protocol fixture, NOT OpenCode and NOT a model-quality experiment."""
import json
from pathlib import Path

instructions = Path("AGENTS.md").read_text()
target = Path("ledger.py")
if "Include both positive and negative amounts" in instructions:
    target.write_text(target.read_text().replace(" if Decimal(x) > 0", ""))
session = "fixture-session"
print(json.dumps({"type": "tool_use", "sessionID": session, "part": {
    "type": "tool", "tool": "fixture_edit", "state": {"status": "completed", "input": {"path": "ledger.py"},
    "output": "Applied the deterministic fixture rule."}}}))
print(json.dumps({"type": "text", "sessionID": session, "part": {
    "type": "text", "text": "Fixture finished. Inspect ledger.py and run independent checks."}}))
print(json.dumps({"type": "step_finish", "sessionID": session, "part": {
    "type": "step-finish", "reason": "stop", "cost": 0, "tokens": {"input": 0, "output": 0}}}))
