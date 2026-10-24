"""Closed observation requests and source-link records."""
import pytest
from pydantic import ValidationError

from app.models.nlp_observations import ObservationLinksRequest, ObservationSupport

pytestmark = pytest.mark.ci_tier('fast')


def request(**changes):
    return ObservationLinksRequest.model_validate({**dict(expected_generation=1,
        expected_projection_digest='sha256:' + '1' * 64,
        focus={'kind':'category','selection':{'task':'message_topics','category_id':'same'}}), **changes})


@pytest.mark.parametrize('changes', [
    {'row_offset':0}, {'conversation_limit':50}, {'category_filter':None},
    {'evidence_offset':1}, {'expected_normalization_sha256':'sha256:'+'2'*64},
    {'evidence_limit':101}, {'expected_generation':True},
])
def test_requests_reject_unused_fields_unpinned_pages_and_topic_normalization(changes):
    with pytest.raises((ValidationError, TypeError)):
        request(**changes)


def test_both_focus_types_have_distinct_pin_requirements():
    for focus in ({'kind':'literal','node_id':'sha256:'+'2'*64},
                  {'kind':'category','selection':{'task':'concept_mentions','category_id':'same'}}):
        body = {'expected_generation':1,'expected_projection_digest':'sha256:'+'1'*64,'focus':focus}
        if focus['kind'] == 'literal':
            with pytest.raises(ValidationError): ObservationLinksRequest.model_validate(body)
        body.update(expected_normalization_sha256='sha256:'+'3'*64)
        plan = ObservationLinksRequest.model_validate(body)
        assert plan.focus.kind == focus['kind']
        with pytest.raises(ValidationError):
            ObservationLinksRequest.model_validate({**body,'evidence_offset':1})
        assert ObservationLinksRequest.model_validate({**body,'evidence_offset':1,
            'expected_view_sha256':'sha256:'+'4'*64}).evidence_offset == 1
    for focus in ({'kind':'category','selection':{'task':'message_topics','category_id':'x'},'node_id':'sha256:'+'2'*64},
                  {'kind':'category'}, {'kind':'literal'}, {'kind':'other'}):
        with pytest.raises(ValidationError):
            ObservationLinksRequest.model_validate({'expected_generation':1,'expected_projection_digest':'sha256:'+'1'*64,'focus':focus})


def test_support_requires_distinct_message_partition_and_nonempty_conversations():
    for values in ((0,1,1,1,0), (1,1,1,1,1), (0,0,1,0,0), (2,1,1,1,0)):
        with pytest.raises(ValidationError):
            ObservationSupport(**dict(zip(('supporting_conversations','supporting_messages','occurrences',
                'complete_supporting_messages','partial_supporting_messages'),values)))
