"""Bind semantic question and packaged evidence to their declared authority."""
from __future__ import annotations

from tools import analytics_qualification as q


def split_protocol(manifest):
    return manifest.get("protocol") == "analytics-closure.v2"


def check_profile(manifest, profile, hardware):
    expected = manifest["profiles"][profile]
    errors = []
    for field in ("os", "memory_gib", "cores", "disk"):
        if hardware.get(field) != expected[field]:
            errors.append("hardware_profile_mismatch:" + field)
    for field in ("cpu_model", "power_mode", "instruction_requirements"):
        if not isinstance(hardware.get(field), str) or not hardware[field].strip():
            errors.append("hardware_measurement_missing:" + field)
    if "hardware_evidence" in manifest:
        if hardware.get("virtualization") != manifest["hardware_evidence"]["topology"]:
            errors.append("hardware_virtualization_not_established")
        evidence = hardware.get("storage_evidence", {})
        if evidence.get("schema") != manifest["hardware_evidence"]["schema"]:
            errors.append("hardware_storage_evidence_missing")
        for field in ("host_cpu_model", "host_power_mode"):
            if not isinstance(hardware.get(field), str) or not hardware[field].strip():
                errors.append("hardware_measurement_missing:" + field)
    return errors


def check_track(manifest, context, job, payload, subject):
    """A source exception applies only to the declared semantic question jobs."""
    if not split_protocol(manifest):
        return None
    if job in {"source-ci", "regression"}:
        return []
    profile = job.split("/")[1]
    if payload.get("profile") != profile or subject != context["source"]:
        return ["evidence_source_or_profile_mismatch"]
    if job.startswith("questions/"):
        policy = manifest["evidence_tracks"]["semantic_questions"]
        if any(payload.get(field) != policy[field] for field in
               ("execution", "ingestion_path", "fixture_mode")):
            return ["semantic_question_track_mismatch"]
        if payload.get("evidence_track") != "semantic_questions":
            return ["semantic_question_track_missing"]
        return check_profile(manifest, profile, payload.get("hardware", {}))
    artifacts = {name: item["sha256"] for name, item in context.get("artifacts", {}).items()}
    policy = manifest["evidence_tracks"]["packaged"]
    if (set(artifacts) != set(policy["artifact_names"])
            or payload.get("artifact_hashes") != artifacts
            or payload.get("evidence_track") != "packaged"
            or any(payload.get(field) != policy[field] for field in
                   ("execution", "ingestion_path", "fixture_mode"))):
        return ["exact_package_execution_not_established"]
    return check_profile(manifest, profile, payload.get("hardware", {}))


def check_admitted_mutation(manifest, phase):
    """Verify real commit progression independently of fixture transaction grouping."""
    name = phase["phase"]
    policy = manifest["packaged_mutations"]
    expected_count = policy["operations"].get(name, 0)
    receipts = phase.get("admitted_commits", [])
    if not isinstance(receipts, list):
        return ["admitted_commits_missing"]
    if expected_count == 0:
        return ([] if not receipts and phase.get("source_before") == phase.get("source_after")
                else ["unchanged_phase_contains_commits"])
    if not receipts:
        return ["admitted_commits_missing"]
    revision = phase["source_before"]["revision"]
    operations, identities, last_time = 0, set(), -1
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return ["admitted_commit_invalid"]
        identity = receipt.get("receipt_id")
        at = receipt.get("monotonic_ns")
        count = receipt.get("operation_count")
        if (not isinstance(identity, str) or not identity or identity in identities
                or receipt.get("origin") != policy["origins"][name]
                or type(receipt.get("canonical_revision")) is not int
                or receipt["canonical_revision"] <= revision
                or type(count) is not int or count <= 0
                or type(at) is not int or at < last_time):
            return ["admitted_commit_binding_invalid"]
        identities.add(identity)
        revision, last_time = receipt["canonical_revision"], at
        operations += count
    if operations != expected_count or revision != phase["source_after"]["revision"]:
        return ["admitted_commit_count_or_revision_mismatch"]
    start = phase.get("clocks", {}).get("durable_canonical_commit")
    first = receipts[0]["monotonic_ns"] / 1_000_000_000
    if not q.finite(start) or start != first:
        return ["mutation_start_is_not_durable_commit"]
    return []


def check_unknown_question(value):
    """Production missing event metadata remains visible as uncertainty."""
    if not isinstance(value, dict):
        return False
    page = value.get("page", {})
    question = value.get("question", {})
    if not isinstance(page, dict) or not isinstance(question, dict):
        return False
    snapshot = question.get("snapshot", {})
    if not isinstance(snapshot, dict) or not isinstance(page.get("coverage"), dict):
        return False
    evaluated = page.get("evaluated_conversation_count")
    return (isinstance(page, dict) and page.get("rows") == []
            and page.get("total_matching_conversations") == 0
            and type(evaluated) is int and evaluated > 0
            and page.get("undetermined_conversation_count") == evaluated
            and page.get("coverage", {}).get("history") in {"unknown", "partial", "complete"}
            and page.get("coverage", {}).get("ordering") == "inferred"
            and type(snapshot.get("source_revision")) is int
            and snapshot["source_revision"] >= 0
            and type(snapshot.get("source_message_count")) is int
            and snapshot["source_message_count"] > 0)
