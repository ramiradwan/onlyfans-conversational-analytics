from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_RULES = {"authority", "retention_rule", "deletion_behavior", "recovery_behavior"}
DOMAINS = {"vault", "workspace", "operational", "managed_recovery", "creator_outward_copy"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("disposition", type=Path)
    args = parser.parse_args()

    root = json.loads((args.source_dir / "ret-001-objects.json").read_text(encoding="utf-8"))
    source_ids: list[str] = []
    for name in root["object_catalog_files"]:
        rows = json.loads((args.source_dir / name).read_text(encoding="utf-8"))
        source_ids.extend(str(row["id"]) for row in rows)

    document = json.loads(args.disposition.read_text(encoding="utf-8"))
    mappings = document["objects"]
    mapped_ids = [str(row["phase_a_object_id"]) for row in mappings]
    domains = document["domain_rules"]

    if len(source_ids) != 38 or len(set(source_ids)) != 38:
        raise SystemExit("Phase-A source must contain exactly 38 unique objects")
    if document["source_phase_a"]["object_count"] != 38:
        raise SystemExit("disposition source count must be 38")
    if len(mapped_ids) != 38 or len(set(mapped_ids)) != 38:
        raise SystemExit("disposition must map exactly 38 unique objects")
    if set(mapped_ids) != set(source_ids):
        raise SystemExit("disposition IDs do not exactly match immutable Phase-A source IDs")
    if set(domains) != DOMAINS:
        raise SystemExit("disposition domain rules are incomplete")
    for name, rule in domains.items():
        if set(rule) != REQUIRED_RULES or any(not str(rule[field]).strip() for field in REQUIRED_RULES):
            raise SystemExit(f"domain rule is incomplete: {name}")
    for row in mappings:
        if row["phase_b_domain"] not in DOMAINS:
            raise SystemExit(f"invalid Phase-B domain: {row['phase_b_domain']}")

    print(json.dumps({"status": "PASS", "phase_a_objects": 38, "mapped_objects": 38}, sort_keys=True))


if __name__ == "__main__":
    main()
