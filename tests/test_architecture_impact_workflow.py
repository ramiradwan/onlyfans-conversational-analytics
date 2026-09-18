from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "architecture-impact.yml"


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def test_pr_body_edits_schedule_the_standalone_architecture_gate() -> None:
    # BaseLoader preserves YAML's "on" key instead of coercing it to True.
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    events = workflow["on"]["pull_request"]
    assert set(events["types"]) == {"opened", "synchronize", "reopened", "edited"}


def test_architecture_gate_checks_the_exact_pr_head_with_the_event_payload() -> None:
    workflow = _workflow_document()
    assert workflow.get("permissions") == {"contents": "read"}
    job = workflow["jobs"]["architecture-impact"]
    assert job.get("runs-on") == "ubuntu-latest"
    assert job.get("timeout-minutes") == 10

    steps = job["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout proposed Product revision")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"

    gate = next(step for step in steps if step.get("name") == "Check protected architecture impact declaration")
    command = gate["run"]
    assert "tools/check_boundary_declaration.py" in command
    assert '--event-path "$GITHUB_EVENT_PATH"' in command
    assert '--base-ref "${{ github.event.pull_request.base.sha }}"' in command
    assert '--head-ref "${{ github.event.pull_request.head.sha }}"' in command
