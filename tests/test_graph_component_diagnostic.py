"""Graph-component scaffolding is explicit, resettable and independently checked."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
from unittest.mock import patch

from tools import analytics_qualification as q
from tools import analytics_graph_component as component

import pytest

pytestmark = [pytest.mark.ci_tier("integration")]


def test_graph_component_real_schema_and_repeated_oracle(tmp_path):
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    args=SimpleNamespace(messages=1000,output=tmp_path,trace_mode='coarse',focused_repeats=2)
    light=SimpleNamespace(atomic_status=lambda *a,**k:None)
    result={}
    from app.analytics import conversation_graph_units as units
    original=units.create_graph_unit
    with patch.object(units,'create_graph_unit',wraps=original) as oracle:
        asyncio.run(component.run_graph_component(args,q,light,None,tmp_path/'status.json',manifest,result,tmp_path/'work'))
        assert oracle.call_count>=3
    assert result['complete']
    assert result['safety']['component_database_closed']
    assert result['safety']['publication_authority_tested'] is False
    assert len(result['samples'])==2
    assert result['samples'][0]['output_unit_id']==result['samples'][1]['output_unit_id']
    assert all(s['independent_graph_equal'] and s['persisted_membership_verified'] for s in result['samples'])
    assert result['fixture']['predecessor_messages']==501


def test_transition_total_includes_store_and_release(tmp_path):
    from tools.analytics_insertion_diagnostic import Attribution
    from app.analytics.graph_membership_selection import MembershipSelection
    from datetime import datetime
    import time
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    f=component.GraphFixture(tmp_path,1000,datetime.fromisoformat(manifest['fixture']['evaluation_clock']))
    original=MembershipSelection.discard
    def release(value):
        time.sleep(.01)
        original(value)
    try:
        with patch.object(MembershipSelection,'discard',release):
            sample=f.sample(Attribution(enabled=False),0)
        names=('construct','store','prepare_selection','changed_segments','integrity','release_selection')
        assert sample['selection_released'] is True
        assert sample['intervals']['release_selection']['seconds'] >= .01
        assert sample['complete_transition_seconds'] >= sum(sample['intervals'][n]['seconds'] for n in names)
        assert sample['complete_transition_seconds'] > sample['selected_interval_seconds']
        assert f.db.execute('SELECT COUNT(*) FROM conversation_graph_refs WHERE generation_id=?',(f.new_id,)).fetchone()[0]==0
    finally:f.close()
