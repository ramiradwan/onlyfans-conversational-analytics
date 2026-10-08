"""Read exact-source CI evidence through the existing qualification runner."""
from pathlib import Path
import json
import subprocess
import sys

from tools import analytics_qualification as q


def select_checks(pages, expected, revision):
    latest = {}
    for page in pages:
        for check in page["check_runs"]:
            name = check["name"]
            if name in expected and (name not in latest or check["id"] > latest[name]["id"]):
                latest[name] = check
    return [{"name": name, "id": check["id"], "head_sha": check["head_sha"],
             "status": check["status"], "conclusion": check["conclusion"], "url": check["html_url"]}
            for name, check in sorted(latest.items())]


def collect(root, manifest, review):
    source = q.source_context(root)
    if (review.get("source_revision") != source["revision"]
            or review.get("source_sha256") != q.digest(source)
            or not review.get("reviewer") or not review.get("checks")):
        raise ValueError("review_does_not_bind_exact_source")
    repository = subprocess.check_output(["gh", "repo", "view", "--json", "nameWithOwner",
        "--jq", ".nameWithOwner"], cwd=root, text=True, timeout=30).strip()
    pages = []
    for page in range(1, 101):
        raw = subprocess.check_output(["gh", "api", f"repos/{repository}/commits/{source['revision']}/check-runs?per_page=100&page={page}"],
                                      cwd=root, text=True, timeout=30)
        value = json.loads(raw)
        pages.append(value)
        if len(value["check_runs"]) < 100:
            break
    else:
        raise ValueError("ci_evidence_pagination_limit")
    return {"complete": True, "repository": repository, "requested_sha": source["revision"],
            "source_unchanged": q.source_context(root) == source,
            "reviewed_source_sha": review["source_revision"], "review": review,
            "checks": select_checks(pages, manifest["ci_jobs"], source["revision"]),
            "api_responses": pages, "observed": q.stamp()}


def main(input_path):
    config = q.read_json(Path(input_path))
    payload = collect(Path(config["root"]), config["manifest"], config["review"])
    q.write_once(Path(config["output"]), payload)
    errors = q.check_payload(config["manifest"], {"source": config["source"]}, "source-ci", payload)
    return 1 if errors or payload["source_unchanged"] is not True else 0
