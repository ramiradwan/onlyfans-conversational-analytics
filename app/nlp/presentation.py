"""Authenticated, bounded display data independent of model conditioning."""
from __future__ import annotations

import copy
import hashlib
import json

from app.nlp.release_catalog import canonical as canonical_bytes

MAX_CATALOG_BYTES = 65_536
_CONFIGS = {
    "boundary-entity-config.v2": {
        "schema", "graphs", "tokenizer", "labels", "policies", "decoding",
        "presentation_catalog",
    },
    "instruction-topic-config.v2": {
        "schema", "model", "system_prompt", "grammar", "runtime", "labels", "policies",
        "context_tokens", "output_tokens", "inference_threads", "presentation_catalog",
    },
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("nlp_presentation_duplicate")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nlp_presentation_invalid")


def _parse(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_CATALOG_BYTES:
        raise ValueError("nlp_presentation_size")
    try:
        # Explicit UTF-8 excludes JSON's otherwise accepted UTF-16/32 encodings.
        return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique,
                          parse_constant=_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("nlp_presentation_invalid") from error


def load_resolver(profile, read_configuration=None):
    """Load verified configuration members once per freeze, before resolving labels.

    The callback is supplied by DependencyService and reads ArtifactStore leases.
    Profiles supply logical member IDs only, never paths or executable callbacks.
    """
    assets = {asset.artifact_id: asset for asset in profile.artifacts}
    configs = [(component, assets[key]) for component in profile.components
               for key in component.artifact_ids if assets[key].format_id in _CONFIGS]
    catalogues = {asset.artifact_id for asset in profile.artifacts
                  if asset.format_id == "nlp-presentation.v1"}
    if not configs:
        if catalogues:
            raise ValueError("nlp_presentation_configuration")
        return None
    if read_configuration is None:
        raise ValueError("nlp_presentation_unavailable")
    catalogue_ids = set()
    for component, asset in configs:
        if asset.role != "configuration":
            raise ValueError("nlp_presentation_configuration")
        value = _parse(read_configuration(asset.artifact_id))
        if (type(value) is not dict or set(value) != _CONFIGS[asset.format_id]
                or value["schema"] != asset.format_id
                or type(value["presentation_catalog"]) is not str
                or value["presentation_catalog"] not in component.artifact_ids):
            raise ValueError("nlp_presentation_configuration")
        catalogue_ids.add(value["presentation_catalog"])
    if len(catalogue_ids) != 1 or catalogue_ids != catalogues:
        raise ValueError("nlp_presentation_identity")
    asset = assets[next(iter(catalogue_ids))]
    if (asset.role != "configuration" or asset.sha256 != profile.presentation.sha256
            or not 0 < asset.size_bytes <= MAX_CATALOG_BYTES):
        raise ValueError("nlp_presentation_identity")
    raw = read_configuration(asset.artifact_id)
    if (type(raw) is not bytes or len(raw) != asset.size_bytes
            or hashlib.sha256(raw).hexdigest() != asset.sha256):
        raise ValueError("nlp_presentation_hash")
    return catalogue_resolver(profile, raw)


def catalogue_resolver(profile, raw):
    value = _parse(raw)
    if (type(value) is not dict or set(value) != {"policy_id", "revision", "labels"}
            or type(value["policy_id"]) is not str or type(value["revision"]) is not str
            or value["policy_id"] != profile.presentation.policy_id
            or value["revision"] != profile.presentation.revision
            or canonical_bytes(value) != raw
            or hashlib.sha256(raw).hexdigest() != profile.presentation.sha256
            or type(value["labels"]) is not list or len(value["labels"]) > 128):
        raise ValueError("nlp_presentation_identity")
    from app.analytics.nlp_read_projection import frozen_labels
    expected = {(row["task_contract"], row["category_id"]): row
                for row in frozen_labels(profile)}
    entries = {}
    for row in value["labels"]:
        if (type(row) is not dict
                or set(row) != {"task_contract", "taxonomy", "category_id", "display_label"}
                or type(row["task_contract"]) is not str or type(row["category_id"]) is not str):
            raise ValueError("nlp_presentation_entry")
        key = (row["task_contract"], row["category_id"])
        identity = expected.get(key)
        if identity is None or key in entries or row["taxonomy"] != identity["taxonomy"]:
            raise ValueError("nlp_presentation_entry")
        entries[key] = {key: copy.deepcopy(identity[key]) for key in
                        ("presentation", "task_contract", "taxonomy", "category_id")}
        entries[key]["display_label"] = row["display_label"]
    if entries.keys() != expected.keys():
        raise ValueError("nlp_presentation_membership")

    def resolve(identity):
        answer = entries.get((identity["task_contract"], identity["category_id"]))
        if answer is None or any(answer[key] != value for key, value in identity.items()):
            raise ValueError("nlp_presentation_identity")
        return copy.deepcopy(answer)

    # Reuse the immutable saved-label length, Unicode and strict UTF-8 contract.
    frozen_labels(profile, resolve)
    return resolve
