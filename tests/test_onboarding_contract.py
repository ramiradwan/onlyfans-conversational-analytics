"""Local contracts cannot be substituted for the hosted projection or authority."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

pytestmark = [pytest.mark.ci_tier("fast")]

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "shared" / "onboarding"
SCHEMA = json.loads((BUNDLE / "schema.json").read_text())
CASES = json.loads((BUNDLE / "vectors.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_local_contract_vector(case):
    Draft7Validator.check_schema(SCHEMA)
    assert Draft7Validator(SCHEMA).is_valid(case["value"]) is case["valid"]


def test_generated_contracts_are_current():
    subprocess.run([sys.executable, str(ROOT / "tools/generate_onboarding_contract.py"), "--check"], check=True)
