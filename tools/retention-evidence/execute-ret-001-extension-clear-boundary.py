#!/usr/bin/env python3
"""Bind Phase-4 Extension clear evidence to Phase-2 companion survival evidence.

This script does not reimplement or invoke Extension clear-all. It consumes the
exact-revision BEH-004 observation produced by Phase 4, seeds an isolated
companion canonical store from the Phase-2 Extension replay fixture, and records
that the companion copy remains outside the observed Extension-local clear
boundary.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory

CROSS_PATH = Path(__file__).with_name("execute-ret-001-cross-boundary-json.py")
_SPEC = importlib.util.spec_from_file_location("ret001_cross_boundary_json", CROSS_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load RET-001 cross-boundary JSON module {CROSS_PATH}")
cross = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cross)
module = cross.module
lib = module.lib


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-revision", required=True)
    parser.add_argument("--extension-state", required=True, type=Path)
    parser.add_argument("--extension-clear-observation", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.product_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.product_revision
    ):
        raise SystemExit("--product-revision must be a lowercase 40-hex commit SHA")

    extension_state = json.loads(args.extension_state.read_text(encoding="utf-8"))
    clear_observation = json.loads(
        args.extension_clear_observation.read_text(encoding="utf-8")
    )
    if extension_state.get("schema") != "ofca-ret-001-extension-replay-fixtures/v1":
        raise SystemExit("Extension replay fixture schema differs")
    if extension_state.get("product_revision") != args.product_revision:
        raise SystemExit("Extension replay fixture Product revision differs")
    if clear_observation.get("schema") != "ofca-ret-001-current-behavior-observation/v1":
        raise SystemExit("Phase-4 Extension clear observation schema differs")
    if clear_observation.get("product_revision") != args.product_revision:
        raise SystemExit("Phase-4 Extension clear observation Product revision differs")
    if clear_observation.get("observation_id") != "BEH-004-EXTENSION-CLEAR-ALL":
        raise SystemExit("Expected BEH-004 Extension clear observation")
    if clear_observation.get("factual_result") != "EXTENSION_WIDE_LOCAL_CLEAR_OBSERVED":
        raise SystemExit("Phase-4 Extension clear-all capability was not observed")
    facts = clear_observation.get("facts", {})
    if facts.get("operation_scope") != (
        "all_enumerated_extension_indexeddb_databases_plus_local_and_session_storage"
    ):
        raise SystemExit("Phase-4 Extension clear operation scope differs")

    account_id = extension_state["creator_account_id"]
    replay_state = extension_state["snapshot_replay"]
    stream_id = replay_state["identity"]["agent_stream_id"]
    message_id = replay_state["message_source"]["message_id"]

    with TemporaryDirectory(prefix="ret-001-res-007-") as temporary:
        root = Path(temporary)
        repositories = lib.create_canonical_repositories(
            "sqlite",
            canonical_path=root / "canonical.sqlite3",
            projection_path=root / "projections.sqlite3",
        )
        ingest_result = cross.ingest_snapshot_json(
            repositories.history,
            account_id,
            stream_id,
            replay_state["first_snapshot"],
        )
        before = lib.canonical_message(repositories.database, account_id, message_id)
        if before is None:
            raise RuntimeError(
                "RES-007 precondition failed: Extension snapshot did not create companion message"
            )

        # BEH-004 establishes that the observed operation is Extension-local.
        # No companion mutation is performed here because doing so would invent
        # behavior outside clearExtensionLocalData's actual boundary.
        after = lib.canonical_message(repositories.database, account_id, message_id)
        survived = after is not None
        if not survived:
            raise RuntimeError(
                "RES-007 companion state unexpectedly changed across Extension-local clear boundary"
            )

    result = lib.result_document(
        "RES-007-STALE-COMPANION-AFTER-EXTENSION-DELETE",
        args.product_revision,
        "NOT_APPLICABLE",
        [
            {
                "relationship_requirement_id": "RES-007-R1",
                "outcome": "OBSERVED",
                "evidence_ids": [
                    "EXEC-RES-007-EXTENSION-TO-COMPANION-COPY",
                    "BEH-004-EXTENSION-CLEAR-ALL",
                ],
                "facts": {
                    "initial_extension_snapshot_ingest": ingest_result,
                    "companion_message_before_extension_clear_boundary": before,
                    "phase4_extension_clear_factual_result": clear_observation[
                        "factual_result"
                    ],
                    "phase4_extension_clear_operation_scope": facts[
                        "operation_scope"
                    ],
                    "phase4_deleted_database_count": facts.get(
                        "deleted_database_count"
                    ),
                    "phase4_chrome_storage_local_cleared": facts.get(
                        "chrome_storage_local_cleared"
                    ),
                    "phase4_chrome_storage_session_cleared": facts.get(
                        "chrome_storage_session_cleared"
                    ),
                    "companion_message_after_extension_clear_boundary": after,
                    "companion_canonical_copy_survived": survived,
                },
            }
        ],
        notes=[
            "The top-level result is NOT_APPLICABLE because this scenario characterizes survival of an already-existing downstream copy, not re-entry into a deleted companion object.",
            "Phase 4 independently executes the production clearExtensionLocalData boundary and establishes that the observed operation clears Extension IndexedDB plus Chrome local/session storage.",
            "Phase 2 does not duplicate Phase-4 clear logic. It combines that exact-revision boundary evidence with an independently seeded companion canonical copy and observes that the companion store is outside the Extension-local clear operation.",
            "This is evidence of current boundary behavior, not a statement that Extension delete-all satisfies any future Legal deletion requirement.",
        ],
    )
    result["characterization_outcome"] = "OBSERVED_DOWNSTREAM_COMPANION_COPY_SURVIVAL"

    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "res-007-stale-companion-after-extension-delete.json"
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema": "ofca-ret-001-extension-clear-boundary-run/v1",
                "product_revision": args.product_revision,
                "scenario_id": result["scenario_id"],
                "result_status": result["result_status"],
                "characterization_outcome": result["characterization_outcome"],
                "output_file": str(destination),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
