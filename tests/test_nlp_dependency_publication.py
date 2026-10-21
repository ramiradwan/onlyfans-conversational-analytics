"""Profile changes retain published findings and rollback data."""
import hashlib
import json

import pytest

from app.analytics.nlp_findings_store import FrozenFindings, checked_snapshot
from app.analytics.nlp_processing import FindingsRunner
from app.nlp import v2
from app.nlp.profiles import AnalysisProfile, canonical_bytes
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_nlp_artifact_store import example
from tests.test_nlp_dependencies import env
from tests.test_nlp_processing_publication import ConformanceAdapter
from tests.test_nlp_profile_authentication import sign

pytestmark = pytest.mark.ci_tier('integration')


def test_profile_switch_reselection_and_rollback_preserve_retained_findings(env):
    e = env
    e.fixture.stores.projections.rollback_retention = 3
    first = e.profile
    labels = {first.presentation.sha256: 'First label'}

    def publish(profile):
        adapter = ConformanceAdapter(profile)

        def nonempty(request, cancelled):
            response = json.loads(adapter(request, cancelled))
            descriptors = {t.task: t for t in profile.component(request.component_id).tasks}
            for task in response['items'][0]['tasks']:
                payload = task['payload']
                category = descriptors[payload['task']].enabled_ids[0]
                payload.update(outcome='assessed_findings', findings=[
                    {'topic_id': category} if payload['task'] == 'message_topics' else
                    {'concept_id': category, 'span': {'start': 0, 'end': 6}}])
            return json.dumps(response).encode()

        runner = FindingsRunner(e.fixture.source, e.fixture.stores.projections,
            authorize=lambda account: None, execution_ready=e.service.ready,
            dependencies=e.service, adapter=nonempty, clock=lambda: e.fixture.clock.now,
            enrichment=e.fixture.pipeline.enrichment,
            label_resolver=lambda identity: {**identity,
                'display_label': labels[identity['presentation']['sha256']]})
        assert runner.process_one(ACCOUNT)
        assert runner.process_one(ACCOUNT)
        assert not runner.process_one(ACCOUNT)
        assert len(adapter.requests) == 2 * len(profile.components)
        result = runner.publish_pending(ACCOUNT)
        assert result.changed
        generation = e.fixture.stores.database.active_generation(ACCOUNT).generation_id
        with e.fixture.stores.database.read() as db:
            reference = dict(db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',
                                       (generation,)).fetchone())
            binding = FrozenFindings(reference['account_ref'], reference['snapshot_id'])
            snapshot = checked_snapshot(db, binding)
            records = [v2.read_record(bytes(db.execute(
                'SELECT document FROM nlp_records WHERE record_digest=?', (digest,)).fetchone()[0]))
                for digest in snapshot['records']]
        assert len(records) == 2
        assert all(record.profile.identity_digest() == profile.identity_digest() for record in records)
        assert all(len(task.result.payload.findings) == 1
                   for record in records for task in record.tasks)
        assert {label['display_label'] for label in snapshot['read_inventory']['labels']} == {
            labels[profile.presentation.sha256]}
        return generation, reference, binding, snapshot

    e.selected()
    history = [publish(first)]
    second, payloads = example('split')
    document = second.model_dump(mode='json')
    document['revision'] = '2'
    document['presentation'].update(revision='2', sha256=hashlib.sha256(b'second labels').hexdigest())
    e.profile = second = AnalysisProfile.model_validate_json(json.dumps(document))
    labels[second.presentation.sha256] = 'Second label'
    e.approved.add(second.identity_digest())
    e.objects['profiles/' + second.identity_digest()] = sign(canonical_bytes(second))
    e.objects.update({'data/' + hashlib.sha256(raw).hexdigest(): raw for raw in payloads.values()})
    e.service.learn(e.catalog(sequence=2))
    e.selected()
    history.append(publish(second))
    first_directory = e.artifacts.reopen(first).directory
    second_directory = e.artifacts.reopen(second).directory
    assert first_directory != second_directory

    e.service.select(ACCOUNT, second.profile_id, second.revision)
    assert AnalysisProfile.model_validate_json(
        e.findings.state(ACCOUNT)['previous_profile_json']) == first
    history.append(publish(second))
    e.service.remove(ACCOUNT, first.profile_id, first.revision)
    assert first_directory.exists()
    e.service.select(ACCOUNT, None, None, rollback=True)
    assert AnalysisProfile.model_validate_json(e.findings.state(ACCOUNT)['profile_json']) == first
    history.append(publish(first))
    e.service.remove(ACCOUNT, second.profile_id, second.revision)
    assert first_directory.exists() and second_directory.exists()
    assert len({generation for generation, _, _, _ in history}) == 4

    with e.fixture.stores.database.read() as db:
        for generation, reference, binding, original in history:
            assert dict(db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',
                                   (generation,)).fetchone()) == reference
            assert checked_snapshot(db, binding) == original
