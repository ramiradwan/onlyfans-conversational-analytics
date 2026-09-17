from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _workflow_paths() -> list[Path]:
    return sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))


def _walk(value: object):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    else:
        yield value


def test_first_party_actions_are_pinned_to_immutable_commits() -> None:
    failures: list[str] = []
    for path in _workflow_paths():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        for job in (document.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses.startswith("actions/"):
                    continue
                action, separator, revision = uses.partition("@")
                if separator != "@" or FULL_SHA.fullmatch(revision) is None:
                    failures.append(f"{path.relative_to(ROOT)}: {action}@{revision}")
    assert failures == [], "first-party Actions must use immutable commit SHAs:\n" + "\n".join(failures)


def test_workflows_never_reenable_deprecated_node_action_runtimes() -> None:
    offenders: list[str] = []
    for path in _workflow_paths():
        text = path.read_text(encoding="utf-8")
        if "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], "do not suppress Actions runtime deprecation warnings: " + ", ".join(offenders)


def test_workflows_do_not_target_retired_branches() -> None:
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in _workflow_paths())
    assert "extension/production-readiness" not in rendered
    assert re.search(r"(?:^|[\s\[,])-?\s*develop(?:[\s\],]|$)", rendered, re.MULTILINE) is None
