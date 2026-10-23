"""Versioned threshold policy for diagnostic findings; scores remain uncalibrated."""
import copy
import math

POLICY = "inclusive-threshold.v1"


def normalize(row, topic_threshold, mention_threshold):
    for threshold in (topic_threshold, mention_threshold):
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("invalid_threshold")
    if row.get("outcome") not in ("assessed", "partial", "unsupported"):
        raise ValueError("invalid_outcome")
    result = copy.deepcopy(row)
    for field, threshold in (("topics", topic_threshold), ("mentions", mention_threshold)):
        findings = result.get(field)
        if type(findings) is not list:
            raise ValueError("invalid_findings")
        for finding in findings:
            score = finding.get("score") if type(finding) is dict else None
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid_score")
        if row["outcome"] != "assessed" and findings:
            raise ValueError("findings_without_assessment")
        result[field] = [finding for finding in findings if finding["score"] >= threshold]
        result[field + "_outcome"] = (
            ("assessed_findings" if result[field] else "assessed_no_match")
            if row["outcome"] == "assessed" else row["outcome"])
    result["normalization_policy"] = POLICY
    result["calibration_status"] = "not_calibrated"
    return result
