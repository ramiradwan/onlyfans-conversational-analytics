"""The reference diagnostic preserves content and cannot qualify visibility."""
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import pytest
import shutil
from functools import partial
from app.analytics.database import ProjectionsDatabase
from tools.analytics_enrichment_reference_sql import EnrichmentReferenceFixture,INDEX
from tools.analytics_insertion_diagnostic import Attribution
from tools import light_first_update_benchmark as runner

NOW=datetime(2026,9,18,12,tzinfo=timezone.utc)

@pytest.mark.parametrize('index',[False,True])
def test_real_reference_transition_and_fresh_oracle(tmp_path,index,monkeypatch):
    if index:
        catalog=tmp_path/'catalog24';catalog.mkdir()
        for sql in sorted((Path(__file__).resolve().parents[1]/'app/analytics/sql').glob('*.sql'))[:24]:
            shutil.copy2(sql,catalog/sql.name)
        monkeypatch.setattr('app.analytics.database.ProjectionsDatabase',partial(ProjectionsDatabase,migrations_dir=catalog))
    fixture=EnrichmentReferenceFixture(tmp_path,1000,NOW,probe_index=index)
    try:
        assert all(r['analyzer_bytes']>8 and r['analyzer_records'] for r in fixture.shape)
        plans=fixture.plans()
        if index:
            reads=[r['object'] for r in plans['copy']['opened_objects'] if r['opcode']=='OpenRead']
            assert INDEX in reads and 'conversation_enrichment_units' not in reads
            assert any(INDEX in str(row) for row in plans['references']['eqp'])
        with patch.object(fixture,'verify',wraps=fixture.verify) as verify:
            a=fixture.sample(0,Attribution(enabled=False))
            b=fixture.sample(1,Attribution(enabled=False))
        assert verify.call_count==2 and a['output_digest']==b['output_digest']
        assert a['independent_source_and_persisted_equal'] and a['complete_reference_comparisons']==4
        assert a['transition_seconds']>=sum(a['intervals'][k]['seconds'] for k in ('store_transaction','activation_transaction','gc_transaction'))
        assert a['commits']==3 and a['unchanged_references_copied']==100
    finally:fixture.close()


def test_failed_reference_selection_does_not_pass(tmp_path):
    from app.analytics import enrichment_proof_transition as transition
    fixture=EnrichmentReferenceFixture(tmp_path,1000,NOW)
    try:
        with patch.object(transition,'finish_transition',return_value=None):
            with pytest.raises(ValueError,match='activation_selection_changed'):
                fixture.sample(0,Attribution(enabled=False))
    finally:fixture.close()


def test_cli_keeps_experiment_explicit():
    base=['runner','--source-root','src','--expected-sha','sha','--output','out','--owner-lock','lock','--preparation','focused-component']
    for extra in [['--enrichment-index-probe'],['--reference-sql-kind','enrichment']]:
        with patch('sys.argv',base+extra),pytest.raises(SystemExit):runner.options()
    with patch('sys.argv',base+['--component-kind','reference-sql','--reference-sql-kind','enrichment','--enrichment-index-probe']):
        assert runner.options().enrichment_index_probe
