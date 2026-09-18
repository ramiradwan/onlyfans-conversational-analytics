#!/usr/bin/env python3
"""Validate the customer journey manifest against the repository."""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "docs" / "ux-journey.json"

SCHEMA_VERSION = "1.0.0"
TOP_LEVEL_KEYS = frozenset({"schema_version", "description", "surfaces", "journeys", "states"})
SURFACE_ID_PATTERN = re.compile(r"^[a-z]+\.[a-z_]+$")
STATE_ID_PATTERN = re.compile(r"^[a-z]+\.[a-z0-9_]+$")
JOURNEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TRANSITION_TRIGGERS = frozenset({"user", "system"})
EVIDENCE_KINDS = frozenset({"unit", "browser", "visual"})
JOURNEY_ATTRIBUTE = 'data-journey-state="{state_id}"'
JOURNEY_ATTRIBUTE_PATTERN = re.compile(r'data-journey-state="([^"]*)"')
JOURNEY_ATTRIBUTE_ROOTS = ("frontend/src",)
JOURNEY_ATTRIBUTE_SUFFIXES = frozenset({".ts", ".tsx"})
IDENTIFIER_ANCHOR_PATTERNS = (
    re.compile(r'id="[a-z0-9-]+"'),
    re.compile(r"[A-Z][A-Z0-9_]*: '[a-z0-9_]+'"),
    re.compile(r"'[a-z0-9_]+'"),
)


class UxJourneyError(ValueError):
    """Raised when the journey manifest is malformed or disagrees with the repository."""


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise UxJourneyError(f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise UxJourneyError("Manifest must be a JSON object")
    return manifest


def _require_keys(value: Any, keys: set[str], optional: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UxJourneyError(f"{where} must be an object")
    missing = keys - value.keys()
    unknown = value.keys() - keys - optional
    if missing or unknown:
        raise UxJourneyError(f"{where} has missing keys {sorted(missing)} or unknown keys {sorted(unknown)}")
    return value


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UxJourneyError(f"{where} must be a non-empty string")
    return value


def _read_repository_file(root: Path, relative: str, where: str) -> str:
    _require_text(relative, where)
    if relative.startswith("/") or "\\" in relative or ".." in Path(relative).parts:
        raise UxJourneyError(f"{where} must be a repository-relative POSIX path: {relative}")
    path = root / relative
    if not path.is_file():
        raise UxJourneyError(f"{where} does not exist: {relative}")
    return path.read_text(encoding="utf-8")


def _require_anchor(root: Path, anchor: Any, state_id: str, where: str) -> str:
    """Anchors are code identifiers, so interface copy can change without touching the manifest."""
    anchor = _require_keys(anchor, {"file", "text"}, set(), where)
    source = _read_repository_file(root, anchor["file"], f"{where}.file")
    text = _require_text(anchor["text"], f"{where}.text")
    match = JOURNEY_ATTRIBUTE_PATTERN.fullmatch(text)
    if match is not None and match.group(1) != state_id:
        raise UxJourneyError(f"{where} text {text!r} must name its own state {state_id}")
    if match is None and not any(pattern.fullmatch(text) for pattern in IDENTIFIER_ANCHOR_PATTERNS):
        raise UxJourneyError(f"{where} text {text!r} must be a code identifier, not interface copy")
    if text not in source:
        raise UxJourneyError(f"{where} text {text!r} is not present in {anchor['file']}")
    return anchor["file"]


def _require_evidence(root: Path, item: dict[str, Any], where: str) -> None:
    """Evidence names a test title, or a capture scenario for visual evidence."""
    source = _read_repository_file(root, item["file"], f"{where}.file")
    name = re.escape(_require_text(item["name"], f"{where}.name"))
    if item["kind"] == "visual":
        patterns = (rf"\b(?:workspace|state|variant): '{name}'", rf"^\s*{name}: Object\.freeze\(")
    else:
        patterns = (rf"\b(?:it|test|describe|test\.step)\(\s*(['\"`]){name}\1", rf"^\s*(?:async\s+)?def {name}\(")
    if not any(re.search(pattern, source, re.MULTILINE) for pattern in patterns):
        kind = "capture scenario" if item["kind"] == "visual" else "test title"
        raise UxJourneyError(f"{where} name {item['name']!r} is not a {kind} in {item['file']}")


def _require_declared_attributes(root: Path, attribute_files: dict[str, str], scanned: set[str]) -> None:
    """Every rendered journey attribute belongs to a state anchored on that attribute in that file."""
    for base in JOURNEY_ATTRIBUTE_ROOTS:
        directory = root / base
        if directory.is_dir():
            scanned |= {
                path.relative_to(root).as_posix()
                for path in directory.rglob("*")
                if path.is_file() and path.suffix in JOURNEY_ATTRIBUTE_SUFFIXES
            }
    for relative in sorted(scanned):
        path = root / relative
        if not path.is_file():
            continue
        for value in JOURNEY_ATTRIBUTE_PATTERN.findall(path.read_text(encoding="utf-8")):
            if value not in attribute_files:
                raise UxJourneyError(f"{relative} renders undeclared journey state {value!r}")
            if attribute_files[value] != relative:
                raise UxJourneyError(
                    f"{relative} renders journey state {value!r}, which is anchored in {attribute_files[value]}"
                )


def validate_manifest(manifest: dict[str, Any], root: Path = ROOT) -> None:
    _require_keys(manifest, set(TOP_LEVEL_KEYS), set(), "Manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise UxJourneyError(f"schema_version must be {SCHEMA_VERSION}")
    _require_text(manifest["description"], "description")

    surfaces = manifest["surfaces"]
    if not isinstance(surfaces, dict) or not surfaces:
        raise UxJourneyError("surfaces must be a non-empty object")
    implementation: dict[str, set[str]] = {}
    for surface_id, surface in surfaces.items():
        if not SURFACE_ID_PATTERN.fullmatch(surface_id):
            raise UxJourneyError(f"Invalid surface id: {surface_id}")
        surface = _require_keys(surface, {"implementation"}, set(), f"surfaces.{surface_id}")
        files = surface["implementation"]
        if not isinstance(files, list) or not files:
            raise UxJourneyError(f"surfaces.{surface_id}.implementation must be a non-empty list")
        for index, file in enumerate(files):
            _read_repository_file(root, file, f"surfaces.{surface_id}.implementation[{index}]")
        implementation[surface_id] = set(files)

    states = manifest["states"]
    if not isinstance(states, list) or not states:
        raise UxJourneyError("states must be a non-empty list")
    state_ids = [state.get("id") if isinstance(state, dict) else None for state in states]
    duplicates = sorted({state_id for state_id in state_ids if state_ids.count(state_id) > 1}, key=str)
    if duplicates:
        raise UxJourneyError(f"Duplicate state ids: {duplicates}")
    known_states = set(state_ids)

    edges: dict[str, set[str]] = {}
    used_surfaces: set[str] = set()
    attribute_files: dict[str, str] = {}
    for state in states:
        state = _require_keys(state, {"id", "surface", "anchor", "transitions", "evidence"}, set(), "state")
        state_id = state["id"]
        if not isinstance(state_id, str) or not STATE_ID_PATTERN.fullmatch(state_id):
            raise UxJourneyError(f"Invalid state id: {state_id}")
        where = f"states.{state_id}"
        if state["surface"] not in implementation:
            raise UxJourneyError(f"{where}.surface is not a declared surface: {state['surface']}")
        used_surfaces.add(state["surface"])
        anchor_file = _require_anchor(root, state["anchor"], state_id, f"{where}.anchor")
        if anchor_file not in implementation[state["surface"]]:
            raise UxJourneyError(f"{where}.anchor.file is not an implementation file of {state['surface']}")
        if state["anchor"]["text"] == JOURNEY_ATTRIBUTE.format(state_id=state_id):
            attribute_files[state_id] = anchor_file

        transitions = state["transitions"]
        if not isinstance(transitions, list):
            raise UxJourneyError(f"{where}.transitions must be a list")
        edges[state_id] = set()
        for index, transition in enumerate(transitions):
            transition = _require_keys(transition, {"to", "by"}, {"action"}, f"{where}.transitions[{index}]")
            if transition["to"] not in known_states:
                raise UxJourneyError(f"{where}.transitions[{index}] targets unknown state {transition['to']}")
            if transition["to"] == state_id:
                raise UxJourneyError(f"{where}.transitions[{index}] targets its own state")
            if transition["by"] not in TRANSITION_TRIGGERS:
                raise UxJourneyError(f"{where}.transitions[{index}].by must be one of {sorted(TRANSITION_TRIGGERS)}")
            if transition["by"] == "user":
                _require_text(transition.get("action"), f"{where}.transitions[{index}].action")
            elif "action" in transition:
                raise UxJourneyError(f"{where}.transitions[{index}] is a system transition with an action")
            edges[state_id].add(transition["to"])

        evidence = state["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise UxJourneyError(f"{where}.evidence must be a non-empty list")
        for index, item in enumerate(evidence):
            item = _require_keys(item, {"kind", "file", "name"}, set(), f"{where}.evidence[{index}]")
            if item["kind"] not in EVIDENCE_KINDS:
                raise UxJourneyError(f"{where}.evidence[{index}].kind must be one of {sorted(EVIDENCE_KINDS)}")
            _require_evidence(root, item, f"{where}.evidence[{index}]")

    unused = sorted(set(implementation) - used_surfaces)
    if unused:
        raise UxJourneyError(f"Surfaces without states: {unused}")
    _require_declared_attributes(root, attribute_files, set().union(*implementation.values()))

    journeys = manifest["journeys"]
    if not isinstance(journeys, list) or not journeys:
        raise UxJourneyError("journeys must be a non-empty list")
    journey_ids: set[str] = set()
    reachable_from_entries: set[str] = set()
    for journey in journeys:
        journey = _require_keys(journey, {"id", "entry_state", "success_state"}, set(), "journey")
        journey_id = journey["id"]
        if not isinstance(journey_id, str) or not JOURNEY_ID_PATTERN.fullmatch(journey_id):
            raise UxJourneyError(f"Invalid journey id: {journey_id}")
        if journey_id in journey_ids:
            raise UxJourneyError(f"Duplicate journey id: {journey_id}")
        journey_ids.add(journey_id)
        for key in ("entry_state", "success_state"):
            if journey[key] not in known_states:
                raise UxJourneyError(f"journeys.{journey_id}.{key} is not a known state: {journey[key]}")
        reachable = _reachable(edges, journey["entry_state"])
        if journey["success_state"] not in reachable:
            raise UxJourneyError(
                f"journeys.{journey_id} cannot reach {journey['success_state']} from {journey['entry_state']}"
            )
        reachable_from_entries |= reachable

    orphaned = sorted(known_states - reachable_from_entries)
    if orphaned:
        raise UxJourneyError(f"States not reachable from any journey entry: {orphaned}")


def _reachable(edges: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        for target in edges.get(queue.popleft(), set()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_MANIFEST_PATH
    try:
        manifest = load_manifest(path)
        validate_manifest(manifest)
    except (OSError, UxJourneyError) as error:
        print(f"UX journey manifest invalid: {error}", file=sys.stderr)
        return 1
    print(f"UX journey manifest valid: {len(manifest['states'])} states, {len(manifest['journeys'])} journeys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
