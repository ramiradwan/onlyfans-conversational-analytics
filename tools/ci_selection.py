"""Explicit backend CI ownership, shared by pytest and the developer runner.

The legacy marker expressions are deliberately frozen here. Changing a tier
cannot make a previously required test disappear from the parity inventory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


TIERS = frozenset({"fast", "integration", "scale", "stateful"})
LANES = ("fast", "integration", "scale", "stateful", "windows-platform",
         "windows-analytics", "legacy-linux", "legacy-windows", "all")
LEGACY_EXCLUDED = frozenset({"slow", "stateful_tier_a", "stateful_tier_b", "stateful_agent_tier_a"})


class SelectionError(ValueError):
    """An invalid classification or selection must fail before tests execute."""


def load_manifest(rootpath: str | Path, path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else Path(rootpath) / "ci/backend-test-shards.json"
    if not target.is_absolute():
        target = Path(rootpath) / target
    def unique_pairs(pairs):
        values = {}
        for name, value in pairs:
            if name in values:
                raise SelectionError(f"Duplicate manifest key: {name}")
            values[name] = value
        return values
    document = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SelectionError("Unsupported backend shard manifest schema")
    assignments = document.get("integration_shards", {})
    if not isinstance(assignments, dict) or not assignments or any(
            type(value) is not int or value not in (1, 2, 3, 4) for value in assignments.values()):
        raise SelectionError("Every integration file must belong to shard 1, 2, 3 or 4")
    if set(document.get("windows_contracts", {})) != {"platform", "analytics"}:
        raise SelectionError("Manifest must declare platform and analytics Windows contracts")
    for name in ("platform", "analytics"):
        for entry in document.get("windows_contracts", {}).get(name, []):
            if not entry.get("selector") or not entry.get("reason"):
                raise SelectionError("Windows contract entries require a selector and review reason")
    return document


def matches(nodeid: str, selector: str) -> bool:
    """Match a file, class, function or exact parameter without prefix leaks."""
    return nodeid == selector or nodeid.startswith(selector + "::") or nodeid.startswith(selector + "[")


def describe_item(item: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    raw_path, separator, remainder = item.nodeid.partition("::")
    path = raw_path.replace("\\", "/")
    # Parameter IDs can contain literal backslashes/byte escapes. Only the
    # filesystem component is normalized; changing a parameter changes identity.
    nodeid = path + separator + remainder
    marker = item.get_closest_marker("ci_tier")
    if (marker is None or len(marker.args) != 1 or marker.kwargs
            or not isinstance(marker.args[0], str) or marker.args[0] not in TIERS):
        raise SelectionError(f"{nodeid}: declare exactly one ci_tier('fast'|'integration'|'scale'|'stateful')")
    # Different scopes may override a module default; duplicates on one scope
    # are ambiguous and usually mean a copied decorator was left behind.
    for owner in item.listchain():
        local = [mark for mark in getattr(owner, "own_markers", ()) if mark.name == "ci_tier"]
        if len(local) > 1:
            raise SelectionError(f"{nodeid}: conflicting ci_tier markers at the same scope")
    markers = {mark.name for mark in item.iter_markers()}
    tier = marker.args[0]
    shard = manifest["integration_shards"].get(path)
    if tier == "integration" and shard is None:
        raise SelectionError(f"{nodeid}: unassigned integration file; run python tools/test_backend.py update-manifest")
    contracts = [name for name, entries in manifest["windows_contracts"].items()
                 if any(matches(nodeid, entry["selector"]) for entry in entries)]
    if len(contracts) > 1:
        raise SelectionError(f"{nodeid}: overlaps both Windows contracts")
    windows_compat = "windows_compat" in markers or "windows_production" in markers
    legacy_default = not bool(markers & LEGACY_EXCLUDED)
    if windows_compat and legacy_default and not contracts:
        raise SelectionError(f"{nodeid}: windows_compat test is missing a Windows contract selector")
    profiles = [name for name, value in manifest["stateful_profiles"].items()
                if any(matches(nodeid, selector) for selector in value["selectors"])]
    if tier == "stateful" and not profiles:
        raise SelectionError(f"{nodeid}: stateful test is missing an explicit profile")
    if legacy_default and tier in {"scale", "stateful"}:
        raise SelectionError(f"{nodeid}: moving an existing default test outside required CI is forbidden; retain its integration tier")
    return {"nodeid": nodeid, "path": path, "tier": tier, "windows_compat": windows_compat,
            "serial": "serial" in markers, "shard": shard if tier == "integration" else None,
            "legacy_default": legacy_default, "legacy_windows": legacy_default,
            "legacy_linux": legacy_default and "windows_production" not in markers,
            "windows_contract": contracts[0] if contracts else None, "stateful_profiles": profiles}


def select_item(metadata: dict[str, Any], lane: str, shard: int | None = None,
                profile: str | None = None) -> bool:
    if lane not in LANES:
        raise SelectionError(f"Unknown CI lane: {lane}")
    if shard is not None and (lane != "integration" or shard not in (1, 2, 3, 4)):
        raise SelectionError("--shard must be 1..4 and is only valid for integration")
    if lane == "all":
        return True
    if lane.startswith("legacy-"):
        return bool(metadata[lane.replace("-", "_")])
    if lane.startswith("windows-"):
        return metadata["legacy_windows"] and metadata["windows_contract"] == lane.removeprefix("windows-")
    if lane == "stateful":
        if not profile:
            raise SelectionError("The stateful lane requires --profile")
        return profile in metadata["stateful_profiles"]
    if lane == "scale":
        return metadata["tier"] == "scale"
    return (metadata["tier"] == lane and metadata["legacy_linux"]
            and (shard is None or metadata["shard"] == shard))


def validate_inventory(inventory: Iterable[dict[str, Any]], manifest: dict[str, Any], *, complete: bool = False) -> None:
    rows = list(inventory)
    nodeids = [row["nodeid"] for row in rows]
    if len(nodeids) != len(set(nodeids)):
        raise SelectionError("Duplicate collected node IDs")
    for row in rows:
        if row["legacy_linux"] and row["tier"] not in {"fast", "integration"}:
            raise SelectionError(f"Required Linux test has no required owner: {row['nodeid']}")
    if not complete:
        return
    integration_files = {row["path"] for row in rows if row["tier"] == "integration"}
    if integration_files != set(manifest["integration_shards"]):
        raise SelectionError("Stale or missing integration files; run python tools/test_backend.py update-manifest")
    for entries in manifest["windows_contracts"].values():
        for entry in entries:
            if not any(matches(nodeid, entry["selector"]) for nodeid in nodeids):
                raise SelectionError(f"Stale Windows contract selector: {entry['selector']}")
    for name, value in manifest["stateful_profiles"].items():
        for selector in value["selectors"]:
            if not any(matches(nodeid, selector) for nodeid in nodeids):
                raise SelectionError(f"Stale stateful profile selector: {name}: {selector}")


def select_items(items: Iterable[Any], manifest: dict[str, Any], lane: str,
                 shard: int | None = None, profile: str | None = None):
    if lane == "stateful" and profile not in manifest["stateful_profiles"]:
        raise SelectionError(f"Unknown stateful profile: {profile}")
    selected, deselected, inventory = [], [], []
    for item in items:
        metadata = describe_item(item, manifest)
        inventory.append(metadata)
        (selected if select_item(metadata, lane, shard, profile) else deselected).append(item)
    validate_inventory(inventory, manifest)
    return selected, deselected, inventory
