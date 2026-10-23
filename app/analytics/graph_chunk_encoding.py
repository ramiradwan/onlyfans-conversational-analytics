"""Bounded storage encoding for canonical graph segment chunks."""

import hashlib
import zlib


MAX_CHUNK_BYTES = 128 * 1024 * 1024
BLOCK_BYTES = 64 * 1024
MAGIC = b'\x00graph-chunk\x00'
VERSION = 1
CODEC_ZLIB = 1
HEADER_BYTES = len(MAGIC) + 10
ADMISSION_CHECKSUM = hashlib.sha256(
    b'-- Admit versioned lossless graph chunk payloads without rewriting stored rows.\n'
    b'SELECT 1;\n'
).hexdigest()


class GraphChunkEncodingError(ValueError):
    """The persisted chunk encoding is invalid or not admitted."""


def admitted(connection):
    """Require the exact forward admission recorded by the migration runner."""

    if connection.execute('PRAGMA user_version').fetchone()[0] < 32:
        return False
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() is None:
        return False
    row = connection.execute('SELECT name,checksum FROM schema_migrations WHERE version=32').fetchone()
    return row is not None and tuple(row) == ('graph_chunk_encoding', ADMISSION_CHECKSUM)


def encode(connection, raw, *, check=lambda: None):
    check()
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_CHUNK_BYTES:
        raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    if not admitted(connection):
        return raw
    encoded = bytearray(MAGIC + bytes((VERSION, CODEC_ZLIB)) + len(raw).to_bytes(8, 'big'))
    compressor = zlib.compressobj(1)
    for offset in range(0, len(raw), BLOCK_BYTES):
        check()
        encoded.extend(compressor.compress(raw[offset:offset + BLOCK_BYTES]))
        if len(encoded) >= len(raw):
            check()
            return raw
    check()
    encoded.extend(compressor.flush())
    check()
    return bytes(encoded) if len(encoded) < len(raw) else raw


def decode(connection, payload, *, check=lambda: None):
    check()
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_CHUNK_BYTES:
        raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    if not payload.startswith(b'\x00'):
        return payload
    if (not admitted(connection) or len(payload) <= HEADER_BYTES
            or not payload.startswith(MAGIC)
            or payload[len(MAGIC):len(MAGIC) + 2] != bytes((VERSION, CODEC_ZLIB))):
        raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    declared = int.from_bytes(payload[len(MAGIC) + 2:HEADER_BYTES], 'big')
    if not 0 < declared <= MAX_CHUNK_BYTES:
        raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    decoder = zlib.decompressobj()
    decoded = bytearray()

    def consume(block):
        remaining = declared - len(decoded)
        try:
            result = decoder.decompress(block, min(BLOCK_BYTES, remaining + 1))
        except zlib.error:
            raise GraphChunkEncodingError('graph_chunk_encoding_invalid') from None
        if len(result) > remaining or decoder.unused_data:
            raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
        decoded.extend(result)
        return decoder.unconsumed_tail, bool(result)

    for offset in range(HEADER_BYTES, len(payload), BLOCK_BYTES):
        block = payload[offset:offset + BLOCK_BYTES]
        while block:
            check()
            tail, produced = consume(block)
            if tail == block and not produced:
                raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
            block = tail
        if decoder.eof and offset + BLOCK_BYTES < len(payload):
            raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    while not decoder.eof:
        check()
        _, produced = consume(b'')
        if not produced:
            break
    check()
    if not decoder.eof or len(decoded) != declared:
        raise GraphChunkEncodingError('graph_chunk_encoding_invalid')
    return bytes(decoded)
