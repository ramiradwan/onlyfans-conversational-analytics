"""Bounded operation dispatch for a private, owner-created worker channel."""
from __future__ import annotations

import json
import struct
from typing import Annotated, BinaryIO, Literal

from pydantic import Field, model_validator

from app.nlp.adapter import AdapterCapability
from app.nlp.contracts import Contract, Digest, Identifier, MAX_FRAME_BYTES, NlpBoundaryError
from app.nlp.profiles import Artifact, EngineIdentity, canonical_bytes, read_profile
from app.nlp.v2 import _decode

Operation = Literal['bootstrap', 'handshake', 'prepare', 'analyze', 'cancel', 'unload',
                    'close', 'transport_failure']
Nonce = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
Handle = Annotated[int, Field(ge=1, le=2**64 - 1)]


class Envelope(Contract):
    channel: Literal['nlp-child.v1'] = 'nlp-child.v1'
    operation: Operation
    payload: dict[str, object]


class BootstrapRequest(Contract):
    channel_nonce: Nonce
    profile_json: Annotated[str, Field(min_length=1, max_length=MAX_FRAME_BYTES)]
    artifact_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=256)]

    @model_validator(mode='after')
    def exact_profile(self):
        profile = read_profile(self.profile_json.encode('ascii'))
        if (canonical_bytes(profile).decode('ascii') != self.profile_json or
                self.artifact_ids != tuple(a.artifact_id for a in profile.artifacts)):
            raise ValueError('bootstrap profile mismatch')
        return self


class BootstrapResult(Contract):
    channel_nonce: Nonce
    profile_sha256: Digest
    outcome: Literal['bound', 'refused']
    reason: Literal['none', 'context_mismatch', 'artifact_mismatch', 'resource_limit', 'host_unavailable']

    @model_validator(mode='after')
    def coherent(self):
        if (self.outcome == 'bound') != (self.reason == 'none'):
            raise ValueError('bootstrap outcome mismatch')
        return self


class ArtifactHandle(Contract):
    artifact: Artifact
    handle: Handle


class OwnerContext(Contract):
    schema_tag: Literal['nlp-owner-context.v1'] = Field(alias='schema')
    channel_nonce: Nonce
    profile_json: Annotated[str, Field(min_length=1, max_length=MAX_FRAME_BYTES)]
    engine: EngineIdentity
    adapters: Annotated[tuple[AdapterCapability, ...], Field(min_length=1, max_length=16)]
    artifacts: Annotated[tuple[ArtifactHandle, ...], Field(min_length=1, max_length=256)]


class CloseRequest(Contract):
    request_id: Identifier


class CloseResult(Contract):
    request_id: Identifier
    outcome: Literal['drained', 'failed']
    reason: Literal['none', 'busy', 'timeout', 'host_failure']

    @model_validator(mode='after')
    def coherent(self):
        if (self.outcome == 'drained') != (self.reason == 'none'):
            raise ValueError('close outcome mismatch')
        return self


class TransportFailure(Contract):
    reason: Literal['invalid_frame', 'unexpected_operation', 'invalid_payload', 'binding_mismatch',
                    'resource_limit', 'io_failure', 'deadline', 'host_failure']


class HostLaunch(Contract):
    schema_tag: Literal['nlp-host-launch.v1'] = Field(default='nlp-host-launch.v1', alias='schema')
    channel_nonce: Nonce
    profile_json: Annotated[str, Field(min_length=1, max_length=MAX_FRAME_BYTES)]
    artifacts: Annotated[tuple[ArtifactHandle, ...], Field(min_length=1, max_length=256)]


class HostControl(Contract):
    schema_tag: Literal['nlp-host-control.v1'] = Field(default='nlp-host-control.v1', alias='schema')
    channel_nonce: Nonce
    operation: Literal['commit', 'close']


class HostObservation(Contract):
    schema_tag: Literal['nlp-host-observe.v1'] = Field(default='nlp-host-observe.v1', alias='schema')
    channel_nonce: Nonce
    engine: EngineIdentity


class HostStatus(Contract):
    schema_tag: Literal['nlp-host-status.v1'] = Field(alias='schema')
    channel_nonce: Nonce
    state: Literal['ready', 'running', 'drained', 'failed']
    reason: Literal['none', 'invalid_bootstrap', 'identity_mismatch', 'artifact_mismatch',
                    'resource_limit', 'launch_failure', 'io_failure', 'parent_lost',
                    'timeout', 'worker_failure']
    worker_pid: Annotated[int, Field(ge=1, le=2**32 - 1)] | None
    tree_drained: bool
    failure_stage: Literal['parent_access', 'outer_access', 'parent_process', 'current_package',
        'parent_package', 'parent_sid', 'parent_session', 'parent_job', 'host_job', 'outer_limits',
        'main_observation', 'main_trust', 'parent_image', 'host_image', 'package_observation',
        'package_trust', 'descriptor_read', 'descriptor_parse', 'descriptor_validation',
        'declared_engine', 'engine_selection', 'installed_engine', 'main_manifest', 'engine_manifest',
        'parent_family', 'current_package_identity', 'main_files', 'initial_command',
        'worker_metadata', 'observation_binding', 'metadata_binding', 'artifact_bindings',
        'worker_control', 'worker_launch', 'worker_drain'] | None = None

    @model_validator(mode='after')
    def coherent(self):
        if ((self.failure_stage is not None and self.state != 'failed') or
                (self.state == 'failed') == (self.reason == 'none') or
                (self.state == 'ready' and self.worker_pid is not None) or
                (self.state == 'running' and (self.worker_pid is None or self.tree_drained)) or
                (self.state in ('ready', 'drained') and not self.tree_drained)):
            raise ValueError('host status mismatch')
        return self


class OwnedLaunchError(NlpBoundaryError):
    """A failed launch whose exact owner may still require a join."""
    def __init__(self, owner):
        super().__init__('nlp_host_launch_failed')
        self.owner = owner


def payload_bytes(value: Contract | dict) -> bytes:
    body = value.model_dump(mode='json', by_alias=True) if isinstance(value, Contract) else value
    try:
        data = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise NlpBoundaryError('nlp_invalid_payload') from None
    if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise NlpBoundaryError('nlp_frame_too_large')
    return data


def encode_envelope(operation: Operation, payload: Contract) -> bytes:
    return payload_bytes(Envelope(operation=operation, payload=payload.model_dump(mode='json')))


def decode_envelope(body: bytes) -> tuple[str, bytes]:
    envelope = _decode(Envelope, body, 'envelope')
    return envelope.operation, payload_bytes(envelope.payload)


def read_frame(stream: BinaryIO) -> bytes:
    def exact(length, *, header=False):
        value = bytearray()
        while len(value) < length:
            block = stream.read(length - len(value))
            if not block:
                if header and not value:
                    raise EOFError('nlp_channel_eof')
                raise NlpBoundaryError('nlp_truncated_frame')
            value.extend(block)
        return bytes(value)

    length = struct.unpack('<I', exact(4, header=True))[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise NlpBoundaryError('nlp_frame_too_large')
    return exact(length)


def write_frame(stream: BinaryIO, body: bytes) -> None:
    if not 0 < len(body) <= MAX_FRAME_BYTES:
        raise NlpBoundaryError('nlp_frame_too_large')
    remaining = memoryview(struct.pack('<I', len(body)) + body)
    while remaining:
        written = stream.write(remaining)
        if not written or written > len(remaining):
            raise NlpBoundaryError('nlp_channel_write')
        remaining = remaining[written:]
    stream.flush()
