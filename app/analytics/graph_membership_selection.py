"""Bounded, transaction-local membership versions from independent graph checks."""
from sys import getsizeof
import re
import zlib

from app.analytics.validation_receipt import content_stamp, generation_binding


class MembershipSelection:
    """Hints become usable only after complete stored changed-segment checking.

    Never persisted or passed to construction. Missing hints retain SQL checks.
    """
    def __init__(self, connection, generation, account, validation):
        from app.analytics import shared_graph
        self.connection = connection
        self.generation = generation['generation_id']
        self.binding = generation_binding(generation)
        self.account, self.validation = account, validation
        self.version = connection.total_changes
        self.stamp = content_stamp(connection)
        self.max_rows = shared_graph.MAX_CHANGED_VERIFICATION_ROWS
        self.max_bytes = shared_graph.MAX_CHANGED_VERIFICATION_BYTES
        self.values, self.rows, self.bytes = {}, 0, 0
        self.ready = False
        self.discarded = False

    def discard(self):
        self.values.clear()
        self.rows = self.bytes = 0
        self.ready = False
        self.discarded = True

    def request(self, kind, segment, identity):
        if self.discarded:
            return
        key = kind, segment
        bucket = self.values.get(key)
        if bucket is None:
            overhead = getsizeof(key) + 256
            if self.bytes + overhead > self.max_bytes:
                self.discard(); return
            bucket = self.values[key] = {}
            self.bytes += overhead
        if identity in bucket:
            return
        # Reserve content-hash and dictionary space before retaining the hint.
        cost = getsizeof(identity) + getsizeof('0' * 64) + 128
        if self.rows + 1 > self.max_rows or self.bytes + cost > self.max_bytes:
            self.discard(); return
        bucket[identity] = None
        self.rows += 1; self.bytes += cost

    def observe(self, kind, segment, identity, content):
        bucket = self.values.get((kind, segment))
        if bucket is not None and identity in bucket:
            if bucket[identity] is not None:
                raise ValueError('graph_membership_selection_duplicate')
            bucket[identity] = content

    def finish(self):
        if (not self.discarded and self.connection.in_transaction
                and self.connection.total_changes == self.version
                and self.stamp is not None and content_stamp(self.connection) == self.stamp):
            self.ready = True
        else:
            self.discard()

    def matches(self, connection, generation, account, validation):
        return (self.ready and connection is self.connection and connection.in_transaction
                and connection.total_changes == self.version
                and generation['generation_id'] == self.generation
                and generation_binding(generation) == self.binding
                and account == self.account and validation is self.validation
                and content_stamp(connection) == self.stamp)

    def selected(self, kind, segment, identities):
        bucket = self.values.get((kind, segment))
        if not self.ready or bucket is None:
            return None
        answer = {}
        for identity in identities:
            content = bucket.get(identity)
            if content is None:
                return None
            answer[identity] = content
        return answer


def _request_frame(selection, kind, data, count, segments, check):
    """Stream hints only. Shape, order and digests are checked downstream."""
    from app.analytics.conversation_graph_units import MAX_GRAPH_UNIT_BYTES, MAX_GRAPH_UNIT_RECORDS
    if (type(count) is not int or not 0 <= count <= MAX_GRAPH_UNIT_RECORDS
            or not isinstance(data, bytes) or len(data) > MAX_GRAPH_UNIT_BYTES):
        selection.discard(); return
    wanted = {bucket: segment for (k, bucket), segment in segments.items() if k == kind}
    if not wanted:
        return
    prefix = b'g1:' if kind == 'node' else b'e1:'
    pattern = re.compile(rb'"(' + prefix + rb'(?:' + b'|'.join(k.encode() for k in sorted(wanted))
                         + rb')[0-9a-f]{62})"')
    decoder = zlib.decompressobj()
    maximum = min(MAX_GRAPH_UNIT_BYTES * 4, max(2, count * 72 + 2))
    produced, tail = 0, b''
    try:
        for start in range(0, len(data), 65536):
            compressed = data[start:start+65536]
            while compressed:
                check()
                block = decoder.decompress(compressed, 131072)
                compressed = decoder.unconsumed_tail
                produced += len(block)
                if produced > maximum:
                    selection.discard(); return
                combined = tail + block
                for match in pattern.finditer(combined):
                    check()
                    identity = match[1].decode('ascii')
                    selection.request(kind, wanted[identity[3:5]], identity)
                    if selection.discarded: return
                # Preserve split IDs. Repeated complete matches are deduplicated.
                tail = combined[-69:]
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            selection.discard()
    except zlib.error:
        selection.discard()


def prepare_selection(connection, generation, account, proof, validation, check):
    from app.analytics import conversation_graph_unit_sql as units
    prior_graph = getattr(validation, 'proof', None)
    if (proof is None or prior_graph is None or not connection.in_transaction
            or not units.integrity_supported(connection)):
        return None
    stamp = content_stamp(connection)
    prior = connection.execute('SELECT * FROM projection_generations WHERE generation_id=? '
        'AND creator_account_id=?', (proof.generation_id, account)).fetchone()
    if (stamp is None or prior is None or prior['status'] != 'active'
            or generation['expected_active_generation_id'] != proof.generation_id
            or prior_graph.generation_id != proof.generation_id
            or prior_graph.binding != proof.binding or proof.binding != generation_binding(prior)
            or tuple(stamp[:3]) != proof.stamp_prefix or proof.stamp_prefix != prior_graph.stamp_prefix):
        return None
    changed = {(p.kind, p.bucket): p.segment_id for p in validation.plans if not p.reused}
    if not changed:
        return None
    trusted = {h.conversation_ref: h for h in proof.headers}
    selection = MembershipSelection(connection, generation, account, validation)
    for ref in units.list_references(connection, generation['generation_id'], account):
        check()
        if ref.header.checksum_version != 2 or trusted.get(ref.header.conversation_ref) == ref.header:
            continue
        unit = units.load_unit(connection, generation['generation_id'], account, ref.header.conversation_ref)
        if unit is None or unit.header != ref.header:
            selection.discard(); return None
        for kind, data, count in (('node', unit.node_ids, unit.header.node_count),
                                  ('edge', unit.edge_ids, unit.header.edge_count)):
            _request_frame(selection, kind, data, count, changed, check)
            if selection.discarded: return None
    return selection
