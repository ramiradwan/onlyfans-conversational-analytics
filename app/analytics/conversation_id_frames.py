"""Canonical immutable ID frames shared inside one checked operation.

The persisted format, hashes and generic decoder remain unchanged. Noncanonical
legal encodings use that decoder; malformed data is never certified by framing.
"""
from collections.abc import Sequence
from hashlib import sha256
import re
import zlib

_FRAME = 70
_BATCH = 1024
_TRANSLATE = bytes.maketrans(b',', b'\n')


class EncodedIds(Sequence):
    __slots__ = ('_raw', '_start', '_count')

    def __init__(self, raw, start, count):
        self._raw, self._start, self._count = raw, start, count

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        begin = self._start + index * _FRAME + 1
        return self._raw[begin:begin + 67].decode('ascii')

    def frames(self):
        for index in range(0, len(self), _BATCH):
            begin = self._start + index * _FRAME
            end = begin + min(_BATCH, len(self)-index) * _FRAME
            yield self._raw[begin:end]

    def lines(self):
        for block in self.frames():
            yield block.translate(_TRANSLATE, b'"')


class IdGroups(Sequence):
    """Bounded groups, rather than one reconstructed tuple of all identities."""
    def __init__(self, groups):
        self.groups = tuple(groups)
        self.count = sum(map(len, self.groups))

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self)[index]
        if index < 0:
            index += len(self)
        if index < 0:
            raise IndexError(index)
        for group in self.groups:
            if index < len(group):
                return group[index]
            index -= len(group)
        raise IndexError(index)

    def __iter__(self):
        for group in self.groups:
            yield from group

    def frames(self):
        for group in self.groups:
            if isinstance(group, EncodedIds):
                yield from group.frames()
            else:
                for index in range(0, len(group), _BATCH):
                    yield b''.join(b'"' + value.encode('ascii') + b'",' for value in group[index:index+_BATCH])

    def lines(self):
        for block in self.frames():
            yield block.translate(_TRANSLATE, b'"')

    def pack(self, maximum):
        compressor = zlib.compressobj(1)
        pieces, size = [], 0
        tail = b'['
        for block in self.frames():
            encoded = compressor.compress(tail + block[:-1])
            tail = block[-1:]
            pieces.append(encoded)
            size += len(encoded)
            if size > maximum:
                return None
        pieces.append(compressor.compress(tail[:-1] + b']') if len(self) else compressor.compress(b'[]'))
        pieces.append(compressor.flush())
        if sum(map(len,pieces)) > maximum:
            return None
        return b''.join(pieces)


def canonical_groups(unit, summaries, check):
    """Verify actual bytes, counts, ordering and both unchanged hash contracts."""
    from app.analytics.conversation_graph_units import MAX_GRAPH_UNIT_BYTES, MAX_GRAPH_UNIT_RECORDS
    h = unit.header
    digest = sha256(f'conversation-graph-unit.v{h.checksum_version}\0'.encode())
    digest.update(h.graph_digest.encode('ascii') + b'\0')
    result = {}
    for kind, data, count in (('node',unit.node_ids,h.node_count),('edge',unit.edge_ids,h.edge_count)):
        check()
        if (type(count) is not int or not 0 <= count <= MAX_GRAPH_UNIT_RECORDS
                or not isinstance(data,bytes) or not data or len(data)>MAX_GRAPH_UNIT_BYTES):
            return None
        decoder = zlib.decompressobj()
        maximum = min(MAX_GRAPH_UNIT_BYTES*4, max(2,count*72+2))
        try:
            raw = decoder.decompress(data, maximum+1)
        except zlib.error:
            return None
        if len(raw)>maximum or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            return None
        if not count:
            if raw!=b'[]':
                return None
            digest.update(kind.encode()+b'\0')
            continue
        if len(raw)!=count*_FRAME+1 or raw[:1]!=b'[' or raw[-1:]!=b']':
            return None
        raw=raw[1:-1]+b','
        prefix=b'g1:' if kind=='node' else b'e1:'
        shape=re.compile(rb'(?:"'+prefix+rb'[0-9a-f]{64}",)+')
        last=None
        for start in range(0,count,_BATCH):
            check()
            block=raw[start*_FRAME:min(count,start+_BATCH)*_FRAME]
            if shape.fullmatch(block) is None:
                return None
            values=[block[i:i+_FRAME] for i in range(0,len(block),_FRAME)]
            if (last is not None and last>=values[0]) or any(a>=b for a,b in zip(values,values[1:])):
                raise ValueError('conversation_graph_unit_membership_invalid')
            last=values[-1]
        digest.update(kind.encode()+b'\0')
        start=0
        while start<count:
            check()
            bucket=raw[start*_FRAME+4:start*_FRAME+6]
            # Canonical sorted IDs place every stable identity bucket together.
            lo,hi=start+1,count
            while lo<hi:
                mid=(lo+hi)//2
                if raw[mid*_FRAME+4:mid*_FRAME+6]==bucket:lo=mid+1
                else:hi=mid
            group=EncodedIds(raw,start*_FRAME,lo-start)
            part=sha256(b'conversation-members.v2\0')
            for data_block in group.lines():
                part.update(data_block);digest.update(data_block)
            key=(kind,bucket.decode('ascii'))
            expected=summaries.get(key)
            if expected is None or expected[2:4]!=(len(group),part.hexdigest()):
                raise ValueError('conversation_integrity_membership_invalid')
            result[key]=group
            start=lo
    check()
    if result.keys()!=summaries.keys() or digest.hexdigest()!=h.unit_id:
        raise ValueError('conversation_graph_unit_digest_invalid')
    return result


def trusted_groups(unit, check=lambda: None):
    """Reuse an already-proven immutable unit without repeating per-ID validation.

    This helper is construction-only. Persisted candidate validation still uses
    canonical_groups and therefore independently checks every candidate ID.
    """
    from app.analytics.conversation_graph_units import (
        MAX_GRAPH_UNIT_BYTES, MAX_GRAPH_UNIT_RECORDS,
    )
    from app.analytics.conversation_integrity import decode_manifest

    h = unit.header
    summaries = decode_manifest(unit)
    result = {}
    for kind, data, count in (
        ('node', unit.node_ids, h.node_count),
        ('edge', unit.edge_ids, h.edge_count),
    ):
        check()
        if (type(count) is not int or not 0 <= count <= MAX_GRAPH_UNIT_RECORDS
                or not isinstance(data, bytes) or not data
                or len(data) > MAX_GRAPH_UNIT_BYTES):
            return None
        decoder = zlib.decompressobj()
        maximum = min(MAX_GRAPH_UNIT_BYTES * 4, max(2, count * 72 + 2))
        try:
            raw = decoder.decompress(data, maximum + 1)
        except zlib.error:
            return None
        if (len(raw) > maximum or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            return None
        if not count:
            if raw != b'[]':
                return None
            continue
        if len(raw) != count * _FRAME + 1 or raw[:1] != b'[' or raw[-1:] != b']':
            return None
        framed = raw[1:-1] + b','
        offset = 0
        for key, summary in summaries.items():
            if key[0] != kind:
                continue
            size = summary[2]
            if offset + size > count:
                return None
            begin = offset * _FRAME
            end = (offset + size) * _FRAME
            bucket = key[1].encode('ascii')
            if (framed[begin + 4:begin + 6] != bucket
                    or framed[end - _FRAME + 4:end - _FRAME + 6] != bucket):
                return None
            result[key] = EncodedIds(framed, begin, size)
            offset += size
        if offset != count:
            return None
    check()
    return summaries, result
