"""The SQL fixture checks storage effects, never claims publication authority."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from tools import analytics_qualification as q
from tools import analytics_reference_sql_component as component
from tools import light_first_update_benchmark as runner

pytestmark = [pytest.mark.ci_tier("integration")]


def test_real_sql_reference_lifecycle_and_fresh_oracle(tmp_path):
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    args=SimpleNamespace(messages=1000,output=tmp_path,trace_mode='none',focused_repeats=2)
    result={};light=SimpleNamespace(atomic_status=lambda *a,**k:None)
    with patch.object(component,'unit_for',wraps=component.unit_for) as inputs:
        asyncio.run(component.run_reference_sql_component(args,q,light,None,tmp_path/'status',manifest,result,tmp_path/'work'))
    assert inputs.call_count==102+2*101
    assert result['complete'] and result['safety']['component_database_closed']
    assert not result['safety']['publication_authority_tested']
    assert result['attribution']==[]
    assert result['schema']=='a07-reference-sql-component.v2'
    assert result['cache_scopes']==dict(store_and_validation_kib=32768,retirement_kib=131072)
    assert result['samples'][0]['output_digest']==result['samples'][1]['output_digest']
    for sample in result['samples']:
        assert sample['independent_membership_and_content_equal']
        assert sample['foreign_keys_valid'] and sample['retirement_scope_closed']
        assert sample['unshared_unit_removed'] and sample['shared_units_retained']
        assert sample['transition_seconds']>=sum(v['seconds'] for v in sample['intervals'].values())


def test_cli_keeps_reference_component_explicit():
    base=['runner','--source-root','root','--expected-sha','sha','--output','out','--owner-lock','lock','--component-kind','reference-sql']
    with patch('sys.argv',base),pytest.raises(SystemExit):runner.options()
    with patch('sys.argv',base+['--preparation','focused-component']):
        assert runner.options().component_kind=='reference-sql'
