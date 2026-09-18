"""Load hand-authored question cases independently of application queries."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "analytics" / "questions"


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_requires_timezone")
    return parsed


def load_cases() -> list[dict[str, Any]]:
    cases = []
    for path in sorted(FIXTURES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema"] != "analytics-question-cases.v1" or document["synthetic"] is not True:
            raise ValueError("invalid_fixture_document")
        cases.extend(document["cases"])
    return cases


def validate_case(case: dict[str, Any]) -> None:
    required = {"id", "question", "account", "start", "end", "cutoff", "now",
                "coverage", "source_revision", "projection_generation", "replays",
                "messages", "expected"}
    if set(case) != required:
        raise ValueError("case_fields_invalid")
    if case["question"] not in {"no_later_creator_reply.v1", "pricing_discussions.v1"}:
        raise ValueError("question_invalid")
    start, end, cutoff, now = (instant(case[key]) for key in ("start", "end", "cutoff", "now"))
    if not start < end <= cutoff <= now:
        raise ValueError("time_window_invalid")
    if case["coverage"] not in {"complete", "partial", "unknown"}:
        raise ValueError("coverage_invalid")
    if type(case["source_revision"]) is not int or case["source_revision"] < 0:
        raise ValueError("source_revision_invalid")
    if type(case["projection_generation"]) is not int or case["projection_generation"] < 1:
        raise ValueError("generation_invalid")
    records = {}
    message_fields = {"account", "conversation", "id", "version", "at", "role", "kind",
                      "order", "ordering", "deleted", "pricing", "finding_version", "language"}
    for message in case["messages"]:
        if set(message) != message_fields or not message["account"].startswith("synthetic-"):
            raise ValueError("message_fields_invalid")
        key = (message["account"], message["conversation"], message["id"])
        if key in records:
            raise ValueError("duplicate_canonical_message")
        if message["role"] not in {"participant", "creator", "system", "unknown"}:
            raise ValueError("role_invalid")
        if message["kind"] not in {"message", "attachment", "system", "unknown"}:
            raise ValueError("kind_invalid")
        if message["ordering"] not in {"source", "inferred"}:
            raise ValueError("ordering_invalid")
        if message["pricing"] not in {"positive", "negative", "uncertain", "unsupported", "missing", "error"}:
            raise ValueError("classification_invalid")
        if type(message["version"]) is not int or message["version"] < 1:
            raise ValueError("message_version_invalid")
        if type(message["deleted"]) is not bool:
            raise ValueError("deletion_invalid")
        instant(message["at"])
        records[key] = message
    expected = case["expected"]
    if set(expected) != {"matches", "undetermined", "evidence", "availability"}:
        raise ValueError("expected_fields_invalid")
    if expected["availability"] != "available":
        raise ValueError("availability_invalid")
    for field in ("matches", "undetermined"):
        if len(expected[field]) != len(set(expected[field])):
            raise ValueError("duplicate_conversation_result")
    if set(expected["matches"]) & set(expected["undetermined"]):
        raise ValueError("contradictory_result")
    if set(expected["evidence"]) != set(expected["matches"]):
        raise ValueError("evidence_required")
    own = {key[1] for key in records if key[0] == case["account"]}
    if not set(expected["matches"] + expected["undetermined"]) <= own:
        raise ValueError("result_account_invalid")
    for conversation, refs in expected["evidence"].items():
        if not refs or len(refs) != len(set(refs)):
            raise ValueError("evidence_refs_invalid")
        for ref in refs:
            message = records.get((case["account"], conversation, ref))
            if message is None:
                raise ValueError("evidence_account_invalid")
            at = instant(message["at"])
            if message["deleted"] or not now - timedelta(days=90) < at <= cutoff:
                raise ValueError("evidence_ineligible")
            if message["kind"] not in {"message", "attachment"}:
                raise ValueError("evidence_kind_invalid")
            if case["question"] == "pricing_discussions.v1":
                if not start <= at < end or message["pricing"] != "positive":
                    raise ValueError("pricing_evidence_invalid")
                if message["finding_version"] != message["version"]:
                    raise ValueError("evidence_version_invalid")
                if message["role"] not in {"participant", "creator"}:
                    raise ValueError("evidence_role_invalid")
            elif message["role"] != "participant":
                raise ValueError("evidence_role_invalid")
    for ref in case["replays"]:
        if sum(key[0] == case["account"] and key[2] == ref for key in records) != 1:
            raise ValueError("replay_ref_invalid")
