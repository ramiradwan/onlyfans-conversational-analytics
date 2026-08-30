#!/usr/bin/env python3
"""Observe current RET-001 lifecycle behavior without adding production controls."""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_PATH = Path(__file__).with_name("run-ret-001-companion-observations.py")
_SPEC = importlib.util.spec_from_file_location("ret001_companion_observation_library", LIBRARY_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load RET-001 observation library {LIBRARY_PATH}")
lib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lib)

RESULT_SCHEMA = "ofca-ret-001-current-behavior-observation/v1"
RUNNER_ID = "ret-001-current-behavior-observations/v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def result_document(
    observation_id: str,
    product_revision: str,
    capability_kind: str,
    factual_result: str,
    facts: dict,
    notes: list[str],
) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "observation_id": observation_id,
        "product_revision": product_revision,
        "observed_at": utc_now(),
        "capability_kind": capability_kind,
        "factual_result": factual_result,
        "facts": facts,
        "execution_context": {
            "runner": RUNNER_ID,
            "synthetic_fixture": True,
            "production_retention_or_deletion_behavior_added": False,
            "production_deletion_api_added": False,
            "test_only_direct_sql_deletion": False,
        },
        "notes": notes,
    }


def observe_companion_delete_route_surface(product_revision: str) -> dict:
    from app.api.endpoints import history, insights, transport_ws, webauthn

    production_routers = [
        ("app.api.endpoints.transport_ws", transport_ws.router),
        ("app.api.endpoints.history", history.router),
        ("app.api.endpoints.insights", insights.router),
        ("app.api.endpoints.webauthn", webauthn.router),
    ]
    route_rows = []
    for module_name, router in production_routers:
        for route in router.routes:
            methods = sorted(getattr(route, "methods", None) or [])
            if not methods:
                continue
            route_path = str(route.path)
            prefix = str(getattr(router, "prefix", "") or "")
            if prefix and not route_path.startswith(prefix):
                route_path = f"{prefix}{route_path}"
            route_rows.append({
                "path": route_path,
                "methods": methods,
                "router_module": module_name,
            })
    route_rows.sort(key=lambda item: (item["path"], item["methods"], item["router_module"]))
    delete_routes = [item for item in route_rows if "DELETE" in item["methods"]]
    delete_paths = [item["path"] for item in delete_routes]

    # This is an intentional negative-capability characterization. If Product
    # exposes a new DELETE route, the characterization must be reviewed rather
    # than silently treating absence as evergreen.
    assert delete_paths == ["/api/v1/settings/history/consent"], delete_routes

    return result_document(
        "BEH-001-COMPANION-DELETE-ROUTE-SURFACE",
        product_revision,
        "NEGATIVE_CAPABILITY",
        "NO_SELECTIVE_DATA_DELETE_HTTP_CONTROL_OBSERVED",
        {
            "delete_routes": delete_routes,
            "sole_delete_route": "/api/v1/settings/history/consent",
            "sole_delete_route_semantics": "history_consent_revocation",
            "message_delete_route_present": False,
            "conversation_delete_route_present": False,
            "dataset_delete_route_present": False,
            "account_data_delete_route_present": False,
            "observed_router_modules": [module_name for module_name, _ in production_routers],
        },
        [
            "The DELETE verb is present for history-consent revocation; verb choice is not treated as evidence of stored-data deletion.",
            "This diagnostic iterates the production APIRouter route objects registered by app.main; it does not construct a new production route or require the frontend build artifact.",
            "The observation does not claim that no internal destructive primitive exists.",
        ],
    )


def observe_bridge_projection_reset(root: Path, product_revision: str) -> dict:
    scenario_root = root / "bridge-reset"
    scenario_root.mkdir(parents=True, exist_ok=True)
    repositories = lib.create_canonical_repositories(
        "sqlite",
        canonical_path=scenario_root / "canonical.sqlite3",
        projection_path=scenario_root / "projections.sqlite3",
    )
    account_id, fixture_message = lib.seed_canonical_snapshot(repositories.history)
    repositories.projection.catch_up(account_id)
    before_canonical = lib.canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    before_projection = lib.projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert before_canonical is not None
    assert before_projection is not None

    # Invoke the existing production derived-store reset primitive. This is not
    # a new retention/delete API and the test uses only isolated fixture stores.
    repositories.projection.reset()

    after_canonical = lib.canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    after_projection = lib.projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert after_canonical is not None
    assert after_projection is None

    return result_document(
        "BEH-002-BRIDGE-DERIVED-RESET",
        product_revision,
        "POSITIVE_CAPABILITY",
        "GLOBAL_BRIDGE_DERIVED_RESET_OBSERVED",
        {
            "canonical_message_before": before_canonical,
            "projection_message_before": before_projection,
            "canonical_message_after_reset": after_canonical,
            "projection_message_after_reset": after_projection,
            "canonical_survived": True,
            "derived_projection_removed": True,
            "reset_scope": "entire_bridge_projection_store",
            "selective_message_delete_control_observed": False,
        },
        [
            "ProjectionRepository.reset() is an existing production primitive for disposable Bridge state; this observation does not expose it as a user-facing deletion control.",
            "The observation proves a derived-store reset boundary and separately proves that canonical message state survives that reset.",
        ],
    )


def observe_history_revocation_preserves_data(root: Path, product_revision: str) -> dict:
    scenario_root = root / "history-revocation"
    scenario_root.mkdir(parents=True, exist_ok=True)
    repositories = lib.create_canonical_repositories(
        "sqlite",
        canonical_path=scenario_root / "canonical.sqlite3",
        projection_path=scenario_root / "projections.sqlite3",
    )
    account_id, fixture_message = lib.seed_canonical_snapshot(repositories.history)
    repositories.projection.catch_up(account_id)
    before_canonical = lib.canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    before_projection = lib.projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert before_canonical is not None
    assert before_projection is not None

    current = repositories.history.history_settings(account_id)
    updated = repositories.history.update_history_settings(
        account_id,
        expected_revision=current["settings_revision"],
        values={
            "consent_policy_version": current["consent_policy_version"],
            "consent_revision": None,
            "authorized_platform_creator_id": None,
            "desired_state": "revoked",
            "recent_window_days": current["recent_window_days"],
            "page_size": current["page_size"],
            "pages_per_wake": current["pages_per_wake"],
            "request_interval_ms": current["request_interval_ms"],
            "retry_limit": current["retry_limit"],
        },
    )
    assert updated["desired_state"] == "revoked"
    assert updated["consent_revision"] is None
    assert updated["authorized_platform_creator_id"] is None

    after_canonical = lib.canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    after_projection = lib.projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert after_canonical is not None
    assert after_projection is not None
    assert after_canonical["text_sha256"] == before_canonical["text_sha256"]
    assert after_projection["text_sha256"] == before_projection["text_sha256"]

    return result_document(
        "BEH-003-HISTORY-REVOCATION-NOT-DELETION",
        product_revision,
        "NEGATIVE_CAPABILITY",
        "HISTORY_REVOCATION_PRESERVES_EXISTING_DATA_OBSERVED",
        {
            "settings_state_after_revocation": updated["desired_state"],
            "consent_revision_after_revocation": updated["consent_revision"],
            "authorized_platform_creator_id_after_revocation": updated[
                "authorized_platform_creator_id"
            ],
            "canonical_message_before": before_canonical,
            "canonical_message_after_revocation": after_canonical,
            "projection_message_before": before_projection,
            "projection_message_after_revocation": after_projection,
            "canonical_message_preserved": True,
            "projection_message_preserved": True,
        },
        [
            "The observation invokes the same HistoryRepository state transition used by the existing history-consent revocation endpoint, without publishing transport commands.",
            "Revocation changes authorization/settings state; it is not an observed stored-history deletion operation at this boundary.",
        ],
    )


def execute(product_revision: str) -> list[dict]:
    with TemporaryDirectory(prefix="ret-001-current-behavior-") as temporary:
        root = Path(temporary)
        return [
            observe_companion_delete_route_surface(product_revision),
            observe_bridge_projection_reset(root, product_revision),
            observe_history_revocation_preserves_data(root, product_revision),
        ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.product_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.product_revision
    ):
        raise SystemExit("--product-revision must be a lowercase 40-hex commit SHA")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = execute(args.product_revision)
    for result in results:
        destination = args.output_dir / f"{result['observation_id'].lower()}.json"
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    summary = {
        "schema": "ofca-ret-001-current-behavior-run/v1",
        "product_revision": args.product_revision,
        "runner": RUNNER_ID,
        "observation_ids": sorted(result["observation_id"] for result in results),
        "factual_results": {
            result["observation_id"]: result["factual_result"] for result in results
        },
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
