"""Graph-component scaffolding is explicit, resettable and independently checked."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
from unittest.mock import patch

from tools import analytics_qualification as q
from tools import analytics_graph_component as component


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
