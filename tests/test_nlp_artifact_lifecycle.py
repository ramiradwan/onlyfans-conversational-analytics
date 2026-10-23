"""Exact journal recovery uses real flat files and preserves foreign contents."""
import copy
from io import BytesIO

import pytest

from app.nlp.artifact_store import ArtifactStoreError
from tests.test_nlp_artifact_store import example, store, streams, snapshot

pytestmark = pytest.mark.ci_tier('fast')


def journal_sink(journal):
    def record(event):
        if event['event'] == 'lock':
            journal.update({key: event[key] for key in ('root_id', 'lock_id')})
        elif event['event'] == 'stage':
            journal['stage'] = {'name': event['name'], 'identity': event['identity'], 'owned': {}}
        elif event['event'] == 'member':
            journal['stage']['owned'][event['name']] = event['identity']
        elif event['event'] == 'remove':
            journal['stage'] = {key: event[key] for key in ('name', 'identity', 'owned')}
            journal['stage']['removing'] = True
    return record


def test_interrupted_stage_refuses_live_owner_and_foreign_members(tmp_path, monkeypatch):
    target = store(tmp_path)
    profile, payloads = example()
    journal = {}
    clean = target._cleanup

    def interrupted(*args):
        raise OSError('invented interrupted cleanup')

    monkeypatch.setattr(target, '_cleanup', interrupted)
    bad = [(key, BytesIO(b'wrong')) for key in payloads]
    with pytest.raises(OSError):
        target.stage(profile, bad, ownership=journal_sink(journal))
    monkeypatch.setattr(target, '_cleanup', clean)
    before = snapshot(target.root)
    with pytest.raises(ArtifactStoreError, match='owner_live'):
        target.recover_interrupted(journal, owner_absent=lambda: False)
    assert snapshot(target.root) == before
    directory = target.root / journal['stage']['name']
    foreign = directory / 'foreign'
    foreign.write_bytes(b'preserve')
    with pytest.raises(ArtifactStoreError, match='staging_changed'):
        target.recover_interrupted(journal, owner_absent=lambda: True)
    assert foreign.read_bytes() == b'preserve'
    assert all((target.root / name).read_bytes() == data for name, data in before.items())
    foreign.unlink()  # Only this test's own known fixture.
    target.recover_interrupted(journal, owner_absent=lambda: True)
    assert not directory.exists() and list(target.root.iterdir()) == []


def test_partial_removal_recovers_only_remaining_exact_members(tmp_path, monkeypatch):
    target = store(tmp_path)
    profile, payloads = example('split')
    available = target.stage(profile, streams(payloads))
    journal = {}
    clean = target._cleanup

    def partial(directory, identity, owned, root_id):
        target._check_root(root_id)
        (directory / next(iter(owned))).unlink()
        raise OSError('invented partial removal')

    monkeypatch.setattr(target, '_cleanup', partial)
    with pytest.raises(OSError):
        target.remove(profile, ownership=journal_sink(journal))
    assert available.directory.exists()
    assert len(list(available.directory.iterdir())) + 1 == len(journal['stage']['owned'])
    monkeypatch.setattr(target, '_cleanup', clean)
    target.recover_interrupted(journal, owner_absent=lambda: True)
    assert not available.directory.exists()
    assert list(target.root.iterdir()) == []


def test_partial_stage_cleanup_recovers_remaining_exact_owned_member(tmp_path, monkeypatch):
    target = store(tmp_path)
    profile, payloads = example()
    journal = {}
    clean = target._cleanup

    def partial(directory, identity, owned, root_id):
        assert len(owned) == 2
        target._check_root(root_id)
        (directory / next(iter(owned))).unlink()
        raise OSError('invented partial stage cleanup')

    inputs = streams(payloads)
    inputs[-1] = (inputs[-1][0], BytesIO(b'wrong'))
    monkeypatch.setattr(target, '_cleanup', partial)
    with pytest.raises(OSError):
        target.stage(profile, inputs, ownership=journal_sink(journal))
    directory = target.root / journal['stage']['name']
    assert len(list(directory.iterdir())) == 1
    assert len(journal['stage']['owned']) == 2
    monkeypatch.setattr(target, '_cleanup', clean)
    target.recover_interrupted(journal, owner_absent=lambda: True)
    assert not directory.exists() and list(target.root.iterdir()) == []


def test_wrong_identity_cannot_remove_owned_closure(tmp_path):
    target = store(tmp_path)
    profile, payloads = example()
    available = target.stage(profile, streams(payloads))
    before = snapshot(target.root)
    journal = {'root_id': [target.root.stat().st_dev, target.root.stat().st_ino],
        'lock_id': [0, 0], 'stage': {'name': available.directory.name,
        'identity': [available.directory.stat().st_dev, available.directory.stat().st_ino + 1],
        'owned': {}, 'removing': True}}
    with pytest.raises(ArtifactStoreError):
        target.recover_interrupted(copy.deepcopy(journal), owner_absent=lambda: True)
    assert snapshot(target.root) == before


def test_late_stage_journal_does_not_remove_published_directory(tmp_path):
    target = store(tmp_path)
    profile, payloads = example()
    journal = {}
    available = target.stage(profile, streams(payloads), ownership=journal_sink(journal))
    before = snapshot(target.root)
    target.recover_interrupted(journal, owner_absent=lambda: True)
    assert snapshot(target.root) == before
    assert target.reopen(profile) == available
