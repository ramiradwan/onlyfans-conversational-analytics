"""Byte-compatible membership framing and pending-state falsifiers."""
from dataclasses import replace
from datetime import timedelta
import gc
import hashlib
import json
import zlib
import pytest
from app.analytics.conversation_id_frames import EncodedIds, IdGroups, canonical_groups
from app.analytics.conversation_integrity import groups_for_members, encode_manifest, summarize_group
from app.analytics.conversation_graph_units import ConversationGraphUnit, ConversationGraphUnitHeader, graph_unit_ids, unit_id
from app.analytics.opaque_refs import account_ref,conversation_ref,participant_ref
from tests.continuous_analytics_fixture import ACCOUNT,NOW


def fixture(count=2100):
    account,chat=account_ref(ACCOUNT),conversation_ref(ACCOUNT,'chat')
    nodes=tuple('g1:'+f'{n:064x}' for n in range(count))
    edges=tuple('e1:'+f'{n:064x}' for n in range(count//2))
    groups=[]
    for kind,keys in [('edge',edges),('node',nodes)]:
        if keys:groups.append(summarize_group(account,chat,kind,'00',{k:'c'*64 for k in keys}))
    root,metadata=encode_manifest(account,chat,groups)
    h=ConversationGraphUnitHeader(account,chat,'sha256:'+'a'*64,'sha256:'+'b'*64,
        NOW-timedelta(days=1),NOW+timedelta(days=89),participant_ref(ACCOUNT,'p'),NOW,NOW,root,
        len(nodes),len(edges),unit_id(root,nodes,edges,checksum_version=2),2)
    pack=lambda values:zlib.compress(json.dumps(values,separators=(',',':')).encode(),1)
    return ConversationGraphUnit(h,pack(nodes),pack(edges),metadata),nodes,edges


@pytest.mark.parametrize('count',[1,2,1023,1024,1025,2100])
def test_frame_identity_digest_and_compression_match_existing_contract(count):
    unit,nodes,edges=fixture(count)
    summaries,groups=groups_for_members(unit)
    for kind,keys in [('node',nodes),('edge',edges)]:
        selected=[v for (k,b),v in sorted(groups.items()) if k==kind]
        sequence=IdGroups(selected)
        assert tuple(sequence)==keys
        assert zlib.decompress(sequence.pack(64*1024*1024))==json.dumps(keys,separators=(',',':')).encode()
    assert unit_id(unit.header.graph_digest,IdGroups([groups['node','00']]),
        IdGroups([groups['edge','00']]) if edges else IdGroups([]),checksum_version=2)==unit.header.unit_id
    assert graph_unit_ids(unit)==(nodes,edges)


@pytest.mark.parametrize('fault',['duplicate','order','count','scope','trailing','concatenated','digest','kind','truncated'])
def test_corrupt_units_cannot_retain_membership_verification(fault):
    unit,nodes,edges=fixture(8)
    if fault=='duplicate':nodes=nodes[:-1]+(nodes[-2],)
    if fault=='order':nodes=tuple(reversed(nodes))
    if fault in ('duplicate','order'):unit=replace(unit,node_ids=zlib.compress(json.dumps(nodes,separators=(',',':')).encode()))
    elif fault=='count':unit=replace(unit,header=replace(unit.header,node_count=9))
    elif fault=='scope':unit=replace(unit,header=replace(unit.header,account_ref=account_ref('wrong')))
    elif fault=='digest':unit=replace(unit,header=replace(unit.header,unit_id='0'*64))
    elif fault=='kind':unit=replace(unit,node_ids=zlib.compress(json.dumps(edges,separators=(',',':')).encode()))
    elif fault=='trailing':unit=replace(unit,node_ids=unit.node_ids+b'trailing')
    elif fault=='concatenated':unit=replace(unit,node_ids=unit.node_ids+unit.node_ids)
    else:unit=replace(unit,node_ids=unit.node_ids[:-1])
    with pytest.raises(ValueError):groups_for_members(unit)


def test_noncanonical_legal_json_uses_generic_decoder():
    unit,nodes,edges=fixture(12)
    unit=replace(unit,node_ids=zlib.compress(json.dumps(nodes).encode()))
    summaries,groups=groups_for_members(unit)
    assert not isinstance(groups['node','00'],EncodedIds)
    assert tuple(groups['node','00'])==nodes


def test_capacity_refusal_and_cancel(monkeypatch):
    from app.analytics import conversation_graph_units as units
    unit,nodes,edges=fixture(1200)
    _,groups=groups_for_members(unit)
    assert IdGroups([groups['node','00']]).pack(1) is None
    def cancelled():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):groups_for_members(unit,cancelled)
    monkeypatch.setattr(units,'MAX_GRAPH_UNIT_BYTES',1)
    with pytest.raises(ValueError):groups_for_members(unit)


def test_pending_notices_are_account_scoped_weak_and_never_ready_authority():
    from app.analytics.pending_questions import PendingQuestions
    pending=PendingQuestions()
    class Owner:
        state=('owner','epoch',2)
        def read(self,account):return self.state if account==ACCOUNT else None
    owner=Owner();pending.register(owner.read)
    assert pending.is_pending(ACCOUNT) and not pending.is_pending('other')
    owner.state=None;assert not pending.is_pending(ACCOUNT)
    owner.state=('owner','epoch',2);del owner;gc.collect()
    assert not pending.is_pending(ACCOUNT)


@pytest.mark.parametrize('value',[True,False,('owner','epoch',-1),('owner',None,1),('owner','epoch',True),('only',)])
def test_uncertain_pending_notice_uses_ordinary_checks(value):
    from app.analytics.pending_questions import PendingQuestions
    class Owner:
        def read(self,account):return value
    owner=Owner();pending=PendingQuestions();pending.register(owner.read)
    assert not pending.is_pending(ACCOUNT)
