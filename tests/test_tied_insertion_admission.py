"""Do not add predecessor payload reads to ordinary strictly later arrivals."""
from contextvars import ContextVar
from tests.test_tied_insertion_safety import small_fixture,mutate
from tests.continuous_analytics_fixture import ACCOUNT,NOW,cleanup,cold_equal

import pytest

pytestmark = [pytest.mark.ci_tier("integration")]


def test_later_append_refuses_insertion_before_loading_enrichment_payload(tmp_path,monkeypatch):
    from app.analytics import conversation_insertion as insertion
    f=small_fixture(tmp_path)
    active=ContextVar('inside_insertion_trial',default=False)
    loaded=[]
    try:
        original=insertion.try_insert
        def observe(*args,**kwargs):
            token=active.set(True)
            try:return original(*args,**kwargs)
            finally:active.reset(token)
        read=f.stores.projections.load_enrichment_unit_contents
        def contents(*args,**kwargs):
            if active.get():loaded.append(True)
            return read(*args,**kwargs)
        monkeypatch.setattr(insertion,'try_insert',observe)
        monkeypatch.setattr(f.stores.projections,'load_enrichment_unit_contents',contents)
        mutate(f,'later-arrival',time=NOW)
        candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        assert not loaded,'A strictly later message is not an insertion; do not read its old unit twice'
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)
