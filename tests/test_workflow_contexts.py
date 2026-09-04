"""Behavioural falsifiers for the contexts a workflow reads at job level.

An expression that reads a context unavailable at its position does not fail
the step that uses it: the file fails schema validation, so no job compiles for
any event and the workflow cannot run at all. Nothing local reports that. Every
other workflow suite in this tree parses the same files with PyYAML, which
accepts them, and reads structure out of them, which is intact, so all of those
checks pass against a workflow that GitHub refuses to start.

The one context that is easy to reach for and is not available to a job
environment is ``runner``, because the ephemeral storage location a release
stages verified documents in is named by ``runner.temp``. It is available from
a step onwards.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"

EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)

# Every context GitHub defines, so an expression is read for the ones that are
# named rather than for every dotted token, which would flag a format string.
ALL_CONTEXTS = frozenset(
    {
        "github",
        "env",
        "vars",
        "job",
        "jobs",
        "steps",
        "runner",
        "secrets",
        "strategy",
        "matrix",
        "needs",
        "inputs",
    }
)

# What GitHub makes available at ``jobs.<job_id>.env``. Anything else there is
# a file that does not compile.
JOB_ENVIRONMENT_CONTEXTS = frozenset(
    {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}
)


def _workflow_paths() -> list[Path]:
    paths = sorted(
        path
        for path in WORKFLOW_DIRECTORY.iterdir()
        if path.suffix in {".yml", ".yaml"} and path.is_file()
    )
    assert paths, f"no workflow was read from {WORKFLOW_DIRECTORY}"
    return paths


def _workflow_document(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} is not a mapping document"
    return document


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow declares no jobs"
    return jobs


def _referenced_contexts(value: Any) -> set[str]:
    referenced: set[str] = set()
    for expression in EXPRESSION.finditer(str(value)):
        body = expression.group("body")
        referenced |= {
            context
            for context in ALL_CONTEXTS
            if re.search(rf"\b{context}\s*\.", body)
        }
    return referenced


def _assert_every_job_environment_compiles(
    workflow: dict[str, Any], path: Path
) -> None:
    for job_name, job in _jobs(workflow).items():
        environment = job.get("env") or {}
        assert isinstance(environment, dict), f"{path}: {job_name} env is not a mapping"
        for name, value in environment.items():
            unavailable = _referenced_contexts(value) - JOB_ENVIRONMENT_CONTEXTS
            assert not unavailable, (
                f"{path.name}: {job_name}.env.{name} reads "
                f"{sorted(unavailable)}, which no job environment can resolve, so "
                "the workflow file compiles no jobs for any event"
            )


def test_no_job_environment_reads_a_context_it_cannot_resolve() -> None:
    """Moving a step-level runner reference up to the job turns this red."""

    for path in _workflow_paths():
        _assert_every_job_environment_compiles(_workflow_document(path), path)

    workflow = _workflow_document(WORKFLOW_DIRECTORY / "windows-package.yml")
    hoisted = deepcopy(workflow)
    job_name = sorted(_jobs(hoisted))[0]
    _jobs(hoisted)[job_name]["env"] = {
        "STAGED_DOCUMENT": "${{ runner.temp }}/staged/document.json"
    }
    with pytest.raises(AssertionError, match="no job environment can resolve"):
        _assert_every_job_environment_compiles(
            hoisted, WORKFLOW_DIRECTORY / "windows-package.yml"
        )


def test_a_job_environment_may_still_read_the_contexts_it_can_resolve() -> None:
    """The check refuses an unavailable context, not every context, so a job
    environment built from the run identifiers stays legal."""

    workflow = _workflow_document(WORKFLOW_DIRECTORY / "windows-package.yml")
    allowed = deepcopy(workflow)
    job_name = sorted(_jobs(allowed))[0]
    _jobs(allowed)[job_name]["env"] = {
        "RUN_STEM": "${{ github.run_id }}-${{ github.run_attempt }}",
        "REPOSITORY": "${{ secrets.LEGAL_BINDINGS_REPOSITORY }}",
    }
    _assert_every_job_environment_compiles(
        allowed, WORKFLOW_DIRECTORY / "windows-package.yml"
    )
