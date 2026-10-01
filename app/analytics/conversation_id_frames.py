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
_HEX_BYTES = b'0123456789abcdef'


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

    def __iter__(self):
        # Decode one bounded block rather than making a Python method call,
        # bytes allocation and decoder call for every selected identity.
        for block in self.frames():
            text = block.decode('ascii')
            for offset in range(0, len(text), _FRAME):
                yield text[offset + 1:offset + 68]

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

    def pack_contract(self, maximum, digest, kind):
        """Encode, hash and summarize canonical IDs in one traversal.

        The compressed JSON, unit-digest input and prefix bitmap are byte-for-byte
        compatible with ``pack()``, ``unit_id()`` and ``membership_prefixes.bitmap``.
        This is construction-only; persisted candidate validation still decodes and
        checks the stored unit independently.
        """
        from app.analytics.membership_prefixes import PREFIX_BYTES
        if kind not in ('node', 'edge'):
            raise ValueError('graph_record_kind_invalid')
        expected = b'g1:' if kind == 'node' else b'e1:'
        pattern = re.compile(rb'"' + expected + rb'([0-9a-f]{4}).{60}",', re.DOTALL)
        framing = (b'"' + expected + b'",').translate(None, _HEX_BYTES)
        compressor = zlib.compressobj(1)
        pieces, size = [], 0
        summary = bytearray(PREFIX_BYTES)
        tail = b'['
        digest.update(kind.encode('ascii') + b'\0')
        for block in self.frames():
            if not block or len(block) % _FRAME:
                raise ValueError('graph_identity_invalid')
            encoded = compressor.compress(tail + block[:-1])
            tail = block[-1:]
            if encoded:
                pieces.append(encoded)
                size += len(encoded)
                if size > maximum:
                    return None
            digest.update(block.translate(_TRANSLATE, b'"'))
            # Deleting hexadecimal bytes must leave only the expected frame
            # punctuation. Fixed-width matches below also pin every delimiter,
            # prefix and ordinal boundary. Together these two bounded C passes
            # check ALL identity characters without a per-character regex loop.
            if block.translate(None, _HEX_BYTES) != framing * (len(block) // _FRAME):
                raise ValueError('graph_identity_invalid')
            prefixes = pattern.findall(block)
            if len(prefixes) * _FRAME != len(block):
                raise ValueError('graph_identity_invalid')
            # At most _BATCH four-byte values are retained. Multiple members of
            # the same prefix set one bit; no per-identity Python parse is needed.
            for value in set(prefixes):
                prefix = int(value, 16)
                summary[prefix >> 3] |= 1 << (prefix & 7)
        final = (compressor.compress(tail[:-1] + b']')
                 if len(self) else compressor.compress(b'[]'))
        if final:
            pieces.append(final)
            size += len(final)
        flushed = compressor.flush()
        if flushed:
            pieces.append(flushed)
            size += len(flushed)
        if size > maximum:
            return None
        return b''.join(pieces), bytes(summary)


def canonical_groups(unit, summaries, check, *, proven_summaries=None):
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
        last = None
        if proven_summaries is None:
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
            if proven_summaries is not None:
                # Hash equality reuses only an independently proved membership
                # ordering. Fixed JSON framing is still checked on actual bytes.
                known = proven_summaries.get(key)
                same = known is not None and known[2:4] == expected[2:4]
                # The independently proved membership digest above binds every
                # identity byte. Equal count and digest permit skipping repeated
                # per-character hex validation, not JSON frame boundaries.
                # Without that binding, retain the complete hexadecimal check.
                body = rb'.{62}' if same else rb'[0-9a-f]{62}'
                bound_shape = re.compile(rb'(?:"'+prefix+bucket+body+rb'",)+', re.DOTALL)
                for block in group.frames():
                    check()
                    if bound_shape.fullmatch(block) is None:
                        return None
                    first, end = block[:_FRAME], block[-_FRAME:]
                    if last is not None and last >= first:
                        raise ValueError('conversation_graph_unit_membership_invalid')
                    if not same:
                        values = [block[i:i+_FRAME] for i in range(0,len(block),_FRAME)]
                        if any(a >= b for a,b in zip(values,values[1:])):
                            raise ValueError('conversation_graph_unit_membership_invalid')
                    last = end
            if key in result:
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


def checked_predecessor_groups(unit, check=lambda: None):
    """Recheck immutable bytes before reusing a previously proved ID shape.

    Caller must bind the actual stored unit header to a complete process-local
    predecessor proof. Hashes alone never grant that proof authority.
    """
    opened = trusted_groups(unit, check)
    if opened is None:
        return None
    summaries, groups = opened
    digest = sha256(f'conversation-graph-unit.v{unit.header.checksum_version}\0'.encode())
    digest.update(unit.header.graph_digest.encode('ascii') + b'\0')
    for kind in ('node', 'edge'):
        digest.update(kind.encode('ascii') + b'\0')
        for key, group in groups.items():
            if key[0] != kind:
                continue
            part = sha256(b'conversation-members.v2\0')
            for block in group.lines():
                check()
                part.update(block); digest.update(block)
            if summaries[key][3] != part.hexdigest():
                raise ValueError('conversation_integrity_membership_invalid')
    if digest.hexdigest() != unit.header.unit_id:
        raise ValueError('conversation_graph_unit_digest_invalid')
    check()
    return summaries, groups
