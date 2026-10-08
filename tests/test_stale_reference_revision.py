"""Reject obsolete revisions before scanning; equal revisions still verify content."""
from unittest.mock import Mock

import pytest

from app.analytics.errors import CanonicalRevisionChanged
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, make_fixture

pytestmark = [pytest.mark.ci_tier('integration')]


def test_old_reference_is_rejected_without_scanning_canonical_content(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    try:
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        with f.repositories.database.transaction() as db:
            advance(db)
        reader = Mock(side_effect=AssertionError('obsolete revision needs no content scan'))
        monkeypatch.setattr(f.stores.projections, 'canonical_identity_reader', reader)
        with pytest.raises(CanonicalRevisionChanged):
            f.stores.projections.read_generation_artifact(ACCOUNT, candidate.reference)
        reader.assert_not_called()
    finally:
        cleanup(f)


@pytest.mark.parametrize('fault', ['same_revision_edit', 'missing_account', 'wrong_account'])
def test_revision_precheck_never_authorizes_a_stale_reference(tmp_path, fault):
    f = make_fixture(tmp_path)
    try:
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        with f.repositories.database.transaction() as db:
            if fault == 'same_revision_edit':
                db.execute("UPDATE account_messages SET text='changed but same revision'")
            elif fault == 'missing_account':
                db.execute('DELETE FROM account_heads WHERE creator_account_id=?', (ACCOUNT,))
        with pytest.raises(CanonicalRevisionChanged):
            f.stores.projections.read_generation_artifact(
                'another-account' if fault == 'wrong_account' else ACCOUNT, candidate.reference)
    finally:
        cleanup(f)


def test_current_reference_still_uses_full_verification_and_final_recheck(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    try:
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        original = f.stores.projections._validate_persisted_generation
        def change_after_validation(*args, **kwargs):
            value = original(*args, **kwargs)
            with f.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='changed during validation'")
            return value
        monkeypatch.setattr(f.stores.projections, '_validate_persisted_generation', change_after_validation)
        with pytest.raises(CanonicalRevisionChanged):
            f.stores.projections.read_generation_artifact(ACCOUNT, candidate.reference)
    finally:
        cleanup(f)
