"""Operator-owned assertions; never copied into the solver's input snapshot."""
from decimal import Decimal
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("candidate", Path.cwd() / "ledger.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for amounts, expected in [([], "0"), (["100.25", "-20.10", "5.00"], "85.15"),
                          (["-2.30", "-0.70"], "-3.00"), (["0.10", "0.20"], "0.30")]:
    actual = module.total(amounts)
    assert isinstance(actual, Decimal), type(actual)
    assert actual == Decimal(expected), (amounts, actual, expected)
print("4 signed-decimal checks passed")
