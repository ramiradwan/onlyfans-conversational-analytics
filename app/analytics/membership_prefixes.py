"""Bounded negative-membership summaries for verified graph-unit envelopes.

A zero bit proves only that no verified member has the selected two-byte hash
prefix. Set bits are never treated as membership proof and always fall back to
the exact persisted membership reader.
"""
from __future__ import annotations

PREFIX_BITS = 1 << 16
PREFIX_BYTES = PREFIX_BITS // 8
MAX_ENVELOPE_PREFIX_BYTES = 2 * 1024 * 1024


def _prefix_index(identity: str) -> int:
    if not isinstance(identity, str) or len(identity) != 67:
        raise ValueError("graph_identity_invalid")
    try:
        return int(identity[3:7], 16)
    except ValueError as error:
        raise ValueError("graph_identity_invalid") from error


def bitmap(values) -> bytes:
    result = bytearray(PREFIX_BYTES)
    for identity in values:
        index = _prefix_index(identity)
        result[index >> 3] |= 1 << (index & 7)
    return bytes(result)


def possibly_contains(summary: bytes | None, identity: str) -> bool:
    """False is an exact negative; True means the caller must verify exactly."""
    if not isinstance(summary, bytes) or len(summary) != PREFIX_BYTES:
        return True
    index = _prefix_index(identity)
    return bool(summary[index >> 3] & (1 << (index & 7)))
