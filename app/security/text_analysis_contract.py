"""Closed text-analysis identity objects and offline schema validation."""
from __future__ import annotations

import json
import math

import rfc8785

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from app.core.resource_paths import resource_path
from contracts.loader import verify_snapshot_integrity

PREFIX = "urn:bridge-clean:text-analysis-"
OUTPUT_POLICY = {"profile": PREFIX + "preview-output-policy:v1", "version": 1}
SUPPORTED = tuple(PREFIX + name + ":v1" for name in (
    "authority-delivery-proof", "authority-delivery", "authority-issuance",
    "license-issuance-proof", "license", "preview-acquisition-proof",
    "preview-acquisition", "preview-issuance-proof", "preview-output-policy",
    "preview", "release-identity", "supported-profiles"))
REQUIRED_LICENSE = frozenset(SUPPORTED[i] for i in (0, 1, 2, 3, 4, 10, 11))
REQUIRED_PREVIEW = frozenset(SUPPORTED[i] for i in (0, 1, 2, 5, 6, 7, 8, 9, 10, 11))


class TextAnalysisContractError(ValueError):
    """A closed object or independently supplied binding was rejected."""


def canonical(value: object) -> bytes:
    """RFC8785 encoding; signed field bounds are checked separately."""
    return rfc8785.dumps(value)


def parse_json(raw: bytes, *, maximum: int, require_canonical: bool = False):
    """Bounded I-JSON parsing with IEEE754 numbers and closed member names."""
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError("json_size")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_member")
            result[key] = value
        return result
    def integer(token):
        value = int(token)
        # JCS uses binary64 numbers, including its exactly representable large
        # integers. Admitted signed integer fields have narrower schema bounds.
        return value if abs(value) <= 2**53 - 1 else float(token)
    def invalid_constant(_):
        raise ValueError("json_number")
    def check(item, depth=0):
        if depth > 16:
            raise ValueError("json_depth")
        if type(item) is str:
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ValueError("json_unicode")
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("json_number")
        elif type(item) is dict:
            for key, child in item.items():
                check(key, depth + 1)
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
    try:
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("json_bom")
        value = json.loads(text, object_pairs_hook=pairs, parse_int=integer,
                           parse_constant=invalid_constant)
        check(value)
        if require_canonical and canonical(value) != raw:
            raise ValueError("noncanonical_json")
        return value
    except (UnicodeError, RecursionError, rfc8785.CanonicalizationError, OverflowError) as error:
        raise ValueError("invalid_json") from error


class TextAnalysisSchemas:
    """Manifest-checked schemas with no remote reference retrieval."""
    def __init__(self):
        root = resource_path("contracts")
        manifest = verify_snapshot_integrity(root)
        documents = {}
        resources = []
        for entry in manifest["files"]:
            name = entry["path"]
            if name.startswith("schemas/") and name.endswith(".schema.json"):
                document = json.loads((root / name).read_bytes())
                documents[name] = document
                resource = Resource.from_contents(document)
                resources.append(("https://contracts.bridge-clean.invalid/" + name, resource))
        def unavailable(uri):
            raise NoSuchResource(ref=uri)
        registry = Registry(retrieve=unavailable).with_resources(resources)
        self._validators = {
            name: Draft202012Validator(document, registry=registry)
            for name, document in documents.items()
            if name.startswith("schemas/text-analysis-")
        }

    def valid(self, schema: str, value: object) -> bool:
        if schema not in self._validators:
            raise TextAnalysisContractError("unsupported_schema")
        return self._validators[schema].is_valid(value)

    def require(self, schema: str, value: object) -> None:
        if not self.valid(schema, value):
            raise TextAnalysisContractError("schema_invalid")

    def valid_definition(self, schema: str, definition: str, value: object) -> bool:
        if schema not in self._validators or type(definition) is not str:
            raise TextAnalysisContractError("unsupported_schema")
        validator = self._validators[schema]
        if definition not in validator.schema.get("$defs", {}):
            raise TextAnalysisContractError("unsupported_schema")
        pointer = definition.replace("~", "~0").replace("/", "~1")
        reference = validator.schema["$id"] + "#/$defs/" + pointer
        return validator.evolve(schema={"$ref": reference}).is_valid(value)

    def require_definition(self, schema: str, definition: str, value: object) -> None:
        if not self.valid_definition(schema, definition, value):
            raise TextAnalysisContractError("schema_invalid")


def _object(raw: bytes, maximum: int) -> dict:
    try:
        value = parse_json(raw, maximum=maximum)
    except ValueError:
        raise TextAnalysisContractError("invalid_object") from None
    if type(value) is not dict:
        raise TextAnalysisContractError("invalid_object")
    return value


def output_policy(raw: bytes, *, schemas: TextAnalysisSchemas) -> dict:
    value = _object(raw, 256)
    schemas.require("schemas/text-analysis-preview-output-policy/v1/policy.schema.json", value)
    if type(value["version"]) is not int:
        raise TextAnalysisContractError("schema_invalid")
    return value


def supported_profiles(raw: bytes, *, schemas: TextAnalysisSchemas, authority: str,
                       expected_profiles: tuple[str, ...]) -> tuple[str, ...]:
    value = _object(raw, 3072)
    schemas.require("schemas/text-analysis-supported-profiles/v1/supported-profiles.schema.json", value)
    profiles = value["profiles"]
    if profiles != sorted(set(profiles)):
        raise TextAnalysisContractError("unsupported_profile")
    required = {"license": REQUIRED_LICENSE, "preview": REQUIRED_PREVIEW}.get(authority)
    if required is None:
        raise TextAnalysisContractError("unsupported_authority")
    if (type(expected_profiles) is not tuple or
            any(type(item) is not str or item not in SUPPORTED for item in expected_profiles) or
            list(expected_profiles) != sorted(set(expected_profiles))):
        raise TextAnalysisContractError("invalid_profile_context")
    if not required <= set(profiles) or tuple(profiles) != expected_profiles:
        raise TextAnalysisContractError("unsupported_profile")
    return tuple(profiles)


def release_identity(raw: bytes, *, schemas: TextAnalysisSchemas, expected: dict) -> dict:
    value = _object(raw, 1024)
    schema = "schemas/text-analysis-release-identity/v1/release-identity.schema.json"
    schemas.require(schema, value)
    schemas.require(schema, expected)
    if value != expected:
        raise TextAnalysisContractError("unsupported_release")
    return value
