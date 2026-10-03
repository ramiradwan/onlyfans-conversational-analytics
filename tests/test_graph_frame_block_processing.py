"""Block execution keeps every membership hash, frame and fallback contract."""
from dataclasses import replace
from hashlib import sha256
import json
import random
import zlib
import pytest
from app.analytics import conversation_id_frames as frames
from app.analytics.conversation_integrity import decode_manifest, groups_for_members
from app.analytics.conversation_graph_units import unit_id
from app.analytics.membership_prefixes import bitmap
from tests.test_conversation_frames_safety import fixture

pytestmark = [pytest.mark.ci_tier("fast")]


@pytest.mark.parametrize('count',[0,1,1023,1024,1025,8193])
def test_block_iteration_matches_indexed_members(count):
    ids=['g1:'+f'{i:064x}' for i in range(count)]
    raw=b''.join(b'"'+k.encode()+b'",' for k in ids)
    values=frames.EncodedIds(b'prefix'+raw,6,count)
    assert list(values)==[values[i] for i in range(count)]==ids
    assert values[::-1]==tuple(reversed(ids))


@pytest.mark.parametrize('kind',['node','edge'])
def test_fused_blocks_preserve_packed_bytes_hash_and_bitmap(kind):
    rng=random.Random(83)
    prefix='g1:' if kind=='node' else 'e1:'
    ids=tuple(sorted(prefix+f'{rng.getrandbits(256):064x}' for _ in range(9001)))
    raw=b''.join(b'"'+k.encode()+b'",' for k in ids)
    # Mix immutable frames and reconstructed changed groups across block edges.
    groups=frames.IdGroups((frames.EncodedIds(raw,0,2049),ids[2049:3000],
                           frames.EncodedIds(raw,3000*70,len(ids)-3000)))
    before=sha256(b'contract')
    packed,summary=groups.pack_contract(64*1024*1024,before,kind)
    independent=sha256(b'contract');independent.update(kind.encode()+b'\0')
    for value in ids:independent.update(value.encode()+b'\n')
    assert before.hexdigest()==independent.hexdigest()
    assert packed==groups.pack(64*1024*1024)
    assert summary==bitmap(ids)
    assert zlib.decompress(packed)==json.dumps(ids,separators=(',',':')).encode()


@pytest.mark.parametrize('fault',['leading_gap','shift_quote','kind','prefix_hex','tail_hex','newline','trailing_gap'])
def test_block_encoder_refuses_all_uncovered_or_noncanonical_bytes(fault):
    raw=b'"g1:'+b'a'*64+b'",'
    if fault=='leading_gap':raw=b' '+raw
    elif fault=='trailing_gap':raw+=b' '
    elif fault=='shift_quote':raw=b'g"1:'+b'a'*64+b'",'
    elif fault=='kind':raw=raw.replace(b'g1:',b'e1:')
    elif fault=='prefix_hex':raw=raw[:4]+b'z'+raw[5:]
    elif fault=='tail_hex':raw=raw[:-3]+b'z'+raw[-2:]
    else:raw=raw[:15]+b'\n'+raw[16:]
    class Broken(frames.IdGroups):
        def frames(self):yield raw
    with pytest.raises(ValueError,match='graph_identity_invalid'):
        Broken((('g1:'+'a'*64,),)).pack_contract(1024,sha256(),'node')


def test_proven_shape_still_rejects_moved_json_quotes_with_same_hash():
    unit,nodes,edges=fixture(2100)
    summaries=decode_manifest(unit)
    raw=zlib.decompress(unit.node_ids)
    # Move a quote within a fixed-length frame. Stripping quotes yields exactly
    # the same digest input; proof equality must not permit malformed framing.
    raw=raw[:1]+raw[2:5]+b'"'+raw[5:]
    bad=replace(unit,node_ids=zlib.compress(raw))
    with pytest.raises(ValueError):groups_for_members(bad,proven_summaries=summaries)


def test_proven_digest_does_not_accept_nonhex_or_reordered_members():
    unit,nodes,edges=fixture(2100)
    summaries=decode_manifest(unit)
    for corrupt in [tuple(reversed(nodes)),nodes[:-1]+('g1:'+'z'*64,)]:
        bad=replace(unit,node_ids=zlib.compress(json.dumps(corrupt,separators=(',',':')).encode()),
                    header=replace(unit.header,unit_id=unit_id(unit.header.graph_digest,corrupt,edges,checksum_version=2)))
        with pytest.raises(ValueError):groups_for_members(bad,proven_summaries=summaries)


def test_fused_bitmap_uses_unique_prefixes_per_bounded_block(monkeypatch):
    import builtins
    values=tuple('g1:1234'+f'{i:060x}' for i in range(4096))
    calls=[]
    def integer(value,base=10):
        calls.append(value);return builtins.int(value,base)
    monkeypatch.setattr(frames,'int',integer,raising=False)
    packed,summary=frames.IdGroups((values,)).pack_contract(64*1024*1024,sha256(),'node')
    assert len(calls)==4   # Four blocks rather than 4096 individual conversions.
    assert summary==bitmap(values)


@pytest.mark.parametrize('byte',[ord('g'),ord(':'),ord('"'),ord(','),ord('Z'),0,10,128,255])
def test_hex_filter_cannot_hide_structural_or_nonascii_identity_bytes(byte):
    raw=bytearray(b'"g1:'+b'a'*64+b'",')
    raw[35]=byte
    class Broken(frames.IdGroups):
        def frames(self):yield bytes(raw)
    with pytest.raises(ValueError,match='graph_identity_invalid'):
        Broken((('g1:'+'a'*64,),)).pack_contract(1024,sha256(),'node')


def test_all_block_positions_reject_displaced_frame_punctuation():
    base=b'"g1:'+b'a'*64+b'",'
    for boundary in (0,1,2,3,68,69):
        for moved in (10,32,67):
            changed=bytearray(base);changed[boundary],changed[moved]=changed[moved],changed[boundary]
            class Broken(frames.IdGroups):
                def frames(self):yield bytes(changed)
            with pytest.raises(ValueError,match='graph_identity_invalid'):
                Broken((('g1:'+'a'*64,),)).pack_contract(1024,sha256(),'node')
