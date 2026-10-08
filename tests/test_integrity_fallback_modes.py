"""Optional integrity metadata follows the existing complete storage fallback."""
import pytest
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, cold_equal, make_fixture

pytestmark = [pytest.mark.ci_tier("integration")]


@pytest.mark.parametrize('previous_version_two', [False, True])
def test_disabled_shared_storage_preserves_complete_output(tmp_path, previous_version_two):
    f = make_fixture(tmp_path, conversations=2, messages=4)
    try:
        if previous_version_two:
            f.pipeline.project_account(ACCOUNT)
        f.stores.projections.reuse_graph_content = False
        result = f.pipeline.rebuild_account(ACCOUNT)
        cold_equal(f, result.artifact)
        assert f.pipeline.prepare_questions(ACCOUNT, 1)
        assert not f.pipeline._requires_integrity_upgrade(ACCOUNT)
        with f.stores.database.read() as db:
            versions = {row[0] for row in db.execute(
                "SELECT u.checksum_version FROM conversation_graph_refs r "
                "JOIN conversation_graph_units u USING(creator_account_id,unit_id) "
                "JOIN projection_generations g USING(creator_account_id,generation_id) "
                "WHERE g.status='active'")}
        assert versions == {1}
        f.stores.projections.reuse_graph_content = True
        assert not f.pipeline.prepare_questions(ACCOUNT, 1)
        upgraded = f.pipeline.project_account(ACCOUNT)
        cold_equal(f, upgraded.artifact)
        assert f.pipeline.prepare_questions(ACCOUNT, 1)
        assert not f.pipeline._requires_integrity_upgrade(ACCOUNT)
    finally:
        cleanup(f)
