"""Application framing limits independent of the Noise cipher implementation."""

import base64
import json
from uuid import uuid4

import pytest

from app.transport.companion_records import (
    ASSEMBLY_SECONDS,
    FRAGMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    Assembly,
    CompanionRecordError,
    document,
    encode_document,
    fragments,
)


def _part(data=b'{"ok":true}', **changes):
    value = {
        "type": "fragment",
        "id": str(uuid4()),
        "index": 0,
        "final": True,
        "data": (
            base64.urlsafe_b64encode(data).rstrip(b"=").decode()
            if isinstance(data, bytes)
            else data
        ),
    }
    value.update(changes)
    return encode_document(value)


@pytest.mark.parametrize("size", [1, FRAGMENT_BYTES, 65_536, MAX_DOCUMENT_BYTES - 11])
def test_documents_round_trip_through_bounded_noise_plaintext_records(size):
    value = {"data": "x" * size}
    assembly = Assembly()
    result = None
    for part in fragments(value):
        assert len(part) <= 4_079
        result = assembly.feed(part)
    assert result is not None
    assert len(result["data"]) == size
    assert assembly.identifier is None
    assert not assembly.buffer


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e400}',
        b'{"x":' + b"1" * 400 + b"}",
        b'{"x":"\\ud800"}',
        b'{"\\ud800":1}',
        b'{"x":' + b"[" * 65 + b"0" + b"]" * 65 + b"}",
        b"[]",
        b"null",
        b"\xff",
        b"",
    ],
)
def test_malformed_documents_have_fixed_payload_free_errors(raw):
    with pytest.raises(CompanionRecordError, match="^session_record_refused$"):
        document(raw)


@pytest.mark.parametrize(
    "value",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": 10**400},
        {"x": "\ud800"},
        {"\ud800": 1},
        {1: "unsupported-object-key"},
    ],
)
def test_outbound_documents_reject_noninteroperable_values(value):
    with pytest.raises(CompanionRecordError, match="^session_record_refused$"):
        encode_document(value)


def test_document_depth_is_bounded_in_both_directions():
    nested = 0
    for _ in range(63):
        nested = [nested]
    value = {"value": nested}
    assert document(encode_document(value)) == value
    with pytest.raises(CompanionRecordError):
        encode_document({"value": [[nested]]})


@pytest.mark.parametrize(
    "changes",
    [
        {"index": True},
        {"index": -1},
        {"index": 1},
        {"final": 1},
        {"type": "rpc.request"},
        {"id": "not-a-uuid"},
        {"data": "e30="},
        {"data": ""},
        {"extra": 1},
    ],
)
def test_malformed_fragment_clears_assembly(changes):
    assembly = Assembly()
    with pytest.raises(CompanionRecordError, match="^session_record_refused$"):
        assembly.feed(_part(**changes))
    assert not assembly.buffer
    assert assembly.identifier is None


def test_reordering_and_interleaving_are_refused():
    parts = list(fragments({"data": "x" * 8_000}))
    assembly = Assembly()
    assert assembly.feed(parts[0]) is None
    with pytest.raises(CompanionRecordError):
        assembly.feed(parts[2])
    assert assembly.feed(parts[0]) is None
    alien = json.loads(parts[1])
    alien["id"] = str(uuid4())
    with pytest.raises(CompanionRecordError):
        assembly.feed(encode_document(alien))
    assert not assembly.buffer


def test_nonfinal_fragments_must_be_full_sized():
    with pytest.raises(CompanionRecordError):
        Assembly().feed(_part(data=b"short", final=False))


def test_document_and_assembly_boundaries_are_enforced():
    with pytest.raises(CompanionRecordError):
        list(fragments({"data": "x" * MAX_DOCUMENT_BYTES}))
    assembly = Assembly()
    identifier = str(uuid4())
    for index in range(MAX_DOCUMENT_BYTES // FRAGMENT_BYTES):
        assert (
            assembly.feed(
                _part(
                    data=b" " * FRAGMENT_BYTES, id=identifier, index=index, final=False
                )
            )
            is None
        )
    with pytest.raises(CompanionRecordError):
        assembly.feed(
            _part(
                data=b" " * FRAGMENT_BYTES,
                id=identifier,
                index=assembly.index,
                final=False,
            )
        )
    assert not assembly.buffer


def test_partial_document_expires_at_fixed_total_deadline():
    now = [0.0]
    parts = list(fragments({"data": "x" * 8_000}))
    assembly = Assembly(clock=lambda: now[0])
    assert assembly.feed(parts[0]) is None
    now[0] = ASSEMBLY_SECONDS - 0.01
    assert assembly.feed(parts[1]) is None
    now[0] = ASSEMBLY_SECONDS
    with pytest.raises(CompanionRecordError):
        assembly.feed(parts[2])
    assert not assembly.buffer
