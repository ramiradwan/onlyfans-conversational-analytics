"""Missing independent verification or an untested stale handle cannot pass."""
from copy import deepcopy
import pytest

from tools import analytics_qualification as q
from tests.test_analytics_closure_qualification import MANIFEST, visibility

pytestmark = [pytest.mark.ci_tier('integration')]


@pytest.mark.parametrize('fault', ['stale', 'oracle', 'persisted', 'missing', 'different', 'non_hex'])
def test_every_visibility_probe_requires_complete_safety_evidence(fault):
    data = visibility()
    assert not q.check_visibility(MANIFEST, 'visibility/reference-windows-16g/0', data)
    probe = data['probes'][-1]
    if fault == 'stale':
        probe['stale_reference_rejected'] = False
    elif fault == 'oracle':
        probe['independent_rebuild_equal'] = False
    elif fault == 'persisted':
        probe['persisted_content_revalidated'] = False
    elif fault == 'missing':
        del probe['expected']
    elif fault == 'different':
        probe['actual']['graph_digest'] = 'sha256:' + 'b' * 64
    else:
        probe['actual']['graph_digest'] = 'sha256:' + 'z' * 64
        probe['expected'] = deepcopy(probe['actual'])
    assert q.check_visibility(MANIFEST, 'visibility/reference-windows-16g/0', data)


def test_restarted_reference_is_valid_before_and_rejected_after_mutation(tmp_path):
    from tools.analytics_qualification_fixture import Workload
    from app.analytics.errors import CanonicalRevisionChanged
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup
    f = make_fixture(tmp_path)
    work = Workload.__new__(Workload)
    work.f, work.account, work.last = f, ACCOUNT, None
    try:
        f.pipeline.project_account(ACCOUNT)
        captured = work.capture_current_reference()
        assert captured['checked_current'] and captured['source_revision'] == 1
        assert f.stores.projections.read_generation_artifact(ACCOUNT, work.last)
        with f.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='Synthetic change after restart'")
        with pytest.raises(CanonicalRevisionChanged):
            f.stores.projections.read_generation_artifact(ACCOUNT, work.last)
        with pytest.raises(ValueError, match='not_current'):
            work.capture_current_reference()
        work.account = 'other-account'
        with pytest.raises(ValueError, match='missing'):
            work.capture_current_reference()
    finally:
        cleanup(f)
