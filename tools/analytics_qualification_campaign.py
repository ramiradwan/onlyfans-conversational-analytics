"""Finite routing of the existing 38 qualification jobs and their prerequisites.

No worker, hardware or consent proof is created here. Existing public collectors
and the final verifier remain the only acceptance paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from tools import analytics_qualification as q

SCHEMA = "analytics-campaign-inputs.v1"


def route(manifest, job):
    if job not in q.required_jobs(manifest):
        raise ValueError("campaign_unknown_job:" + job)
    if job in {"regression", "source-ci"}:
        return {"job": job, "family": job, "profile": None}
    parts = job.split("/")
    result = {"job": job, "family": parts[0], "profile": parts[1]}
    if parts[0] == "questions":
        result.update(case=parts[2], state=parts[3], messages=manifest["questions"]["messages"])
    elif parts[0] == "mutation":
        result["messages"] = int(parts[2])
    elif parts[0] == "visibility":
        result["repeat"] = int(parts[2])
    return result


def plan(manifest):
    required = list(q.required_jobs(manifest))
    # Establish the first proof, then expose reference latency/visibility early.
    first = ["questions/constrained-windows-8g/populated/fresh",
             "questions/reference-windows-16g/populated/fresh",
             "visibility/reference-windows-16g/0"]
    if any(job not in required for job in first):
        raise ValueError("campaign_first_gate_missing")
    remaining = [job for job in required if job not in first]
    # Stable profile grouping; setup dependencies remain explicit rather than
    # restoring one browser/consent profile into unrelated packaged jobs.
    remaining.sort(key=lambda job: (route(manifest, job)["profile"] != "reference-windows-16g",
                                   route(manifest, job)["profile"] or "z", required.index(job)))
    jobs = first + remaining
    if len(jobs) != len(set(jobs)) or set(jobs) != set(required):
        raise ValueError("campaign_required_set_changed")
    return [route(manifest, job) for job in jobs]


def _reference(value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError("campaign_input_reference_required")
    path = Path(value["path"])
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("campaign_input_missing:" + str(path))
    if q.file_digest(path) != value["sha256"]:
        raise ValueError("campaign_input_hash_changed:" + str(path))
    return path


def checked_inputs(value, context, manifest):
    if (not isinstance(value, dict) or set(value) != {"schema", "source_sha256", "manifest_sha256", "jobs"}
            or value["schema"] != SCHEMA
            or value["source_sha256"] != q.digest(context["source"])
            or value["manifest_sha256"] != q.digest(manifest)
            or set(value["jobs"]) != set(q.required_jobs(manifest))):
        raise ValueError("campaign_input_binding_or_job_set")
    paths = set()
    for job, inputs in value["jobs"].items():
        family = route(manifest, job)["family"]
        fields = ({"hardware_handoff"} if family == "questions" else
                  {"package_inputs", "hardware_handoff", "setup_review"} if family in {"package", "mutation", "visibility"} else
                  {"review_record"} if family == "source-ci" else set())
        if not isinstance(inputs, dict) or set(inputs) != fields:
            raise ValueError("campaign_prerequisite_fields:" + job)
        # Missing references are a visible preflight blocker, not permission to
        # substitute the prior installed browser/consent profile.
        if family in {"package", "mutation", "visibility"} and inputs["package_inputs"] is not None:
            descriptor = q.read_json(_reference(inputs["package_inputs"]))
            selected = tuple(str(Path(descriptor[key]).resolve()).casefold()
                             for key in ("data_directory", "browser_profile"))
            if any(item in paths for item in selected):
                raise ValueError("campaign_packaged_job_directories_reused:" + job)
            paths.update(selected)
    return value


def prerequisites(manifest, context, job, inputs, *, active_profile):
    selected = route(manifest, job)
    errors = []
    if selected["profile"] is not None and selected["profile"] != active_profile:
        errors.append("profile_transition_required:" + selected["profile"])
    for field, value in inputs.items():
        if value is None:
            errors.append("missing_" + field)
            continue
        try:
            path = _reference(value)
            if field == "hardware_handoff":
                from tools.analytics_qualification_hardware_evidence import check_settings
                check_settings(q.read_json(path))
            elif field == "package_inputs":
                data = q.read_json(path)
                if (data.get("source_revision") != context["source"]["revision"]
                        or data.get("artifacts") != context["artifacts"]):
                    raise ValueError("package_source_or_artifact_binding")
            elif field == "setup_review":
                review = q.read_json(path)
                if (review.get("status") != "ACCEPTED" or review.get("job") != job
                        or review.get("source_revision") != context["source"]["revision"]
                        or review.get("package_inputs_sha256") != inputs["package_inputs"]["sha256"]
                        or review.get("consent_replayed") is not False):
                    raise ValueError("genuine_job_setup_review_required")
                # Selected reviews are provenance, not public PASS evidence.
                # Collector still observes installation, authorized ingestion,
                # runtime/hash/hardware/UI/receipt facts independently.
            elif field == "review_record":
                review = q.read_json(path)
                if (review.get("source_revision") != context["source"]["revision"]
                        or review.get("source_sha256") != q.digest(context["source"])):
                    raise ValueError("source_CI_review_binding")
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(field + ":" + str(error))
    if selected["family"] == "source-ci":
        cli = shutil.which("gh")
        if cli is None:
            errors.append("same_guest_github_cli_missing")
        else:
            try:
                authenticated = subprocess.run([cli, "auth", "status", "--hostname", "github.com"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
                if authenticated.returncode:
                    errors.append("same_guest_github_authentication_or_connectivity_unconfirmed")
            except (OSError, subprocess.SubprocessError):
                errors.append("same_guest_github_authentication_unconfirmed")
    return errors


def arguments(manifest, job, inputs):
    item = route(manifest, job)
    args = SimpleNamespace(profile=item["profile"], messages=item.get("messages", 100000),
        case=item.get("case", "populated"), state=item.get("state", "fresh"), repeat=item.get("repeat", 0),
        subject_root=None, profile_updates=False, continue_after_visibility_failure=False,
        run_questions=item["family"] == "questions", known_synthetic_kinds=item["family"] == "questions",
        run_source="questions" if item["family"] == "questions" else None,
        run_package={"package": "package", "mutation": "matrix", "visibility": "visibility"}.get(item["family"]),
        package_inputs=_reference(inputs["package_inputs"]) if inputs.get("package_inputs") else None,
        hardware_handoff=_reference(inputs["hardware_handoff"]) if inputs.get("hardware_handoff") else None)
    return args



def evidence_valid_so_far(verdict):
    """Missing future jobs is not corrupt evidence; other blocks are not waived."""
    gate = verdict.get("gates", {}).get("evidence_validity", {})
    return ((gate.get("status") == "PASS" and gate.get("reasons") == [])
            or (gate.get("status") == "BLOCKED"
                and gate.get("reasons") == ["mandatory_evidence_missing"]))


def dispatch(root, directory, context, manifest, job, inputs):
    """Execute one authoritative family through its existing actual collector."""
    from tools import analytics_qualification_runner as runner
    family = route(manifest, job)["family"]
    session = q.session(directory, context)
    if family == "regression":
        runner.run_regressions(root, directory, context, session, manifest)
    elif family == "source-ci":
        runner.run_ci(root, directory, context, session, manifest, _reference(inputs["review_record"]))
    elif family == "questions":
        runner.run_source(root, directory, context, session, manifest, arguments(manifest, job, inputs))
    else:
        from tools.analytics_qualification_packaged import run
        run(root, directory, context, session, manifest, arguments(manifest, job, inputs))
    verdict = q.verify(directory, manifest, current_source=q.source_context(root))
    q.write_once(directory / "verdicts" / (session + ".json"), verdict)
    if not evidence_valid_so_far(verdict):
        raise ValueError("campaign_evidence_invalid")
    return verdict


def run_ready(root, directory, context, manifest, config, *, active_profile, selected_job=None):
    checked_inputs(config, context, manifest)
    before = q.verify(directory, manifest, current_source=q.source_context(root))
    if before["status"] == "FAIL" or not evidence_valid_so_far(before):
        raise ValueError("campaign_prior_evidence_invalid")
    accepted = {job for job, value in before["jobs"].items() if value["status"] == "PASS"}
    if selected_job is not None and selected_job not in q.required_jobs(manifest):
        raise ValueError("campaign_unknown_job:" + selected_job)
    first_job = "questions/constrained-windows-8g/populated/fresh"
    if selected_job is not None and first_job not in accepted and selected_job != first_job:
        return {"status": "BLOCKED", "accepted_jobs": len(accepted),
                "required_jobs": len(q.required_jobs(manifest)),
                "blocked": {selected_job: ["first_question_acceptance_required"]}, "public_verdict": before}
    attempted = {q.read_json(path)["job"] for path in (directory/"attempts").glob("*/start.json")}
    blocked = {}
    jobs = plan(manifest)
    if selected_job:
        jobs = [route(manifest, selected_job)]
    for item in jobs:
        job = item["job"]
        if job in accepted:
            continue  # Read actual public evidence; never rerun a passed tuple.
        if (directory.parent / (directory.name + ".stop-after-current")).exists():
            blocked[job] = ["operator_stop_after_current"]
            break
        if job in attempted:
            return {"status": "BLOCKED", "accepted_jobs": len(accepted),
                    "required_jobs": len(q.required_jobs(manifest)),
                    "blocked": {job: ["existing_attempt_requires_review_no_automatic_retry"]}, "public_verdict": before}
        errors = prerequisites(manifest, context, job, config["jobs"][job], active_profile=active_profile)
        if errors:
            blocked[job] = errors
            if not accepted:
                break  # The original first-question gate is not optional.
            continue
        verdict = dispatch(root, directory, context, manifest, job, config["jobs"][job])
        if verdict["jobs"][job]["status"] != "PASS":
            return {"status": verdict["status"], "accepted_jobs": len(accepted),
                    "required_jobs": len(q.required_jobs(manifest)), "stopped_job": job, "public_verdict": verdict}
        newly = {name for name, value in verdict["jobs"].items() if value["status"] == "PASS"}
        if newly != accepted | {job}:
            raise ValueError("campaign_accepted_set_changed")
        accepted = newly
    final = q.verify(directory, manifest, current_source=q.source_context(root))
    return {"status": final["status"], "accepted_jobs": len(accepted),
            "required_jobs": len(q.required_jobs(manifest)), "blocked": blocked, "public_verdict": final}


def describe(manifest):
    jobs = plan(manifest)
    return {"schema": "analytics-campaign-plan.v1", "jobs": jobs, "required_jobs": len(jobs),
            "families": dict(Counter(item["family"] for item in jobs)),
            "profile_changes": "Host coordinator must change the actual VM profile between profile groups.",
            "completion": "Only exact-source public aggregate PASS establishes completion."}



def input_template(context, manifest):
    jobs = {}
    for item in plan(manifest):
        family = item['family']
        fields = ('hardware_handoff',) if family == 'questions' else (
            'package_inputs','hardware_handoff','setup_review') if family in {'package','mutation','visibility'} else (
            'review_record',) if family == 'source-ci' else ()
        jobs[item['job']] = dict.fromkeys(fields)
    return {'schema':SCHEMA,'source_sha256':q.digest(context['source']),
            'manifest_sha256':q.digest(manifest),'jobs':jobs}
