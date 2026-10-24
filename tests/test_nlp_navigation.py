"""Closed navigation identities, immutable registry admission and wire bounds."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.analytics.errors import ProjectionUnavailable
from app.analytics.nlp_navigation import NavigationRegistry
from app.models.insights import FindingsRequest, GroupedFindingsRequest
from app.nlp.navigation import (CategoryNavigationDescriptor, NavigationGroup,
                                navigation_identity, profile_bindings)
from app.nlp.profiles import canonical_bytes, read_profile
from tests.test_nlp_processing_publication import FIXTURES

pytestmark = pytest.mark.ci_tier('fast')


def profile():
    return read_profile((FIXTURES / 'joint-profile.json').read_bytes())


def declaration(value=None, *, revision='1'):
    value = value or profile()
    return CategoryNavigationDescriptor(policy_id='navigation.synthetic', revision=revision,
        source_presentation=value.presentation, bindings=profile_bindings(value), groups=(
            NavigationGroup(group_id='topics', display_label='Topics', task='message_topics',
                            member_ids=('topic.synthetic',)),
            NavigationGroup(group_id='mentions', display_label='Mentions', task='concept_mentions',
                            member_ids=('concept.synthetic',))))


def test_canonical_identity_covers_order_display_and_exact_binding():
    value = declaration()
    raw = canonical_bytes(value)
    assert set(json.loads(raw)) == {'schema_version','policy_id','revision','source_presentation','bindings','groups'}
    assert navigation_identity(value).sha256 == hashlib.sha256(raw).hexdigest()
    assert raw == json.dumps(value.model_dump(mode='json'),sort_keys=True,ensure_ascii=True,
                             separators=(',',':'),allow_nan=False).encode('ascii')
    moved = value.model_copy(update={'groups':tuple(reversed(value.groups))})
    assert navigation_identity(moved) != navigation_identity(value)
    assert profile_bindings(profile()) == value.bindings


@pytest.mark.parametrize('fault', ['disabled','duplicate_member','duplicate_group','wrong_contract',
    'unordered_bindings','duplicate_enabled','unsafe_label','unknown','too_many_groups','too_many_memberships'])
def test_descriptor_rejects_nonclosed_or_unsafe_declarations(fault):
    body = declaration().model_dump(mode='json')
    if fault == 'disabled': body['groups'][0]['member_ids'] = ['topic.disabled']
    elif fault == 'duplicate_member': body['groups'][0]['member_ids'] *= 2
    elif fault == 'duplicate_group': body['groups'].append(copy.deepcopy(body['groups'][0]))
    elif fault == 'wrong_contract': body['bindings'][0]['task_contract'] = 'message_topics.v1'
    elif fault == 'unordered_bindings': body['bindings'].reverse()
    elif fault == 'duplicate_enabled': body['bindings'][0]['enabled_ids'] *= 2
    elif fault == 'unsafe_label': body['groups'][0]['display_label'] = 'Unsafe\u202e'
    elif fault == 'unknown': body['path'] = 'not-admitted'
    elif fault == 'too_many_groups':
        body['groups'] = [dict(body['groups'][0],group_id=f'group.{i}') for i in range(17)]
    else:
        binding = next(x for x in body['bindings'] if x['task']=='message_topics')
        binding['enabled_ids'] = [f'topic.{i:02}' for i in range(64)]
        body['groups'] = [dict(body['groups'][0],group_id=f'group.{i}',member_ids=binding['enabled_ids'])
                          for i in range(5)]
    with pytest.raises(ValueError): CategoryNavigationDescriptor.model_validate_json(json.dumps(body))


def test_actual_canonical_byte_bound_is_independent_of_member_count():
    body = declaration().model_dump(mode='json')
    binding = next(x for x in body['bindings'] if x['task']=='message_topics')
    binding['enabled_ids'] = [f'topic.{i:02}.'+'x'*150 for i in range(64)]
    body['groups'] = [dict(group_id=f'group.{i}',display_label='\U0001f600'*256,
                          task='message_topics',member_ids=binding['enabled_ids'][:16]) for i in range(16)]
    with pytest.raises(ValueError, match='byte limit'):
        CategoryNavigationDescriptor.model_validate_json(json.dumps(body))


def test_registry_retains_exact_old_pin_without_replacing_current_policy():
    first, second = declaration(), declaration(revision='2')
    key = navigation_identity(first).sha256
    current = navigation_identity(second).sha256
    registry = NavigationRegistry((first,second),active_sha256=(current,))
    assert registry.resolve(profile()) == second
    assert registry.resolve(profile(),'sha256:'+key) == first
    with pytest.raises(FrozenInstanceError): registry._active = {}
    with pytest.raises(TypeError): registry._entries[key] = second
    with pytest.raises(ValueError, match='competing'): NavigationRegistry((first,second))
    changed = first.model_copy(update={'groups':tuple(reversed(first.groups))})
    with pytest.raises(ValueError, match='version changed'): NavigationRegistry((first,changed))
    for pin in ('sha256:'+'f'*64, 'sha256:'+key):
        changed_profile = profile().model_copy(update={'presentation':profile().presentation.model_copy(update={'revision':'other'})})
        with pytest.raises(ProjectionUnavailable): registry.resolve(changed_profile,pin)
    assert registry.resolve(changed_profile) is None


def test_registry_limits_and_full_binding_compatibility():
    with pytest.raises(ValueError, match='registry limit'):
        NavigationRegistry(tuple(declaration(revision=str(i)) for i in range(33)),active_sha256=())
    value = declaration()
    for field in ('taxonomy','enabled_ids'):
        bindings = list(value.bindings)
        target = bindings[0]
        update = ({'taxonomy':target.taxonomy.model_copy(update={'sha256':'f'*64})}
                  if field=='taxonomy' else {'enabled_ids':()})
        bindings[0] = target.model_copy(update=update)
        # A declaration for another exact binding is not an active fallback.
        foreign = value.model_copy(update={'bindings':tuple(bindings), 'groups':()})
        registry = NavigationRegistry((foreign,))
        assert registry.resolve(profile()) is None
        with pytest.raises(ProjectionUnavailable): registry.resolve(profile(),navigation_identity(foreign).sha256)


def test_grouped_request_requires_pin_for_either_page_and_legacy_rejects_it():
    body = dict(expected_generation=1,expected_projection_digest='sha256:'+'a'*64,view='grouped')
    assert GroupedFindingsRequest.model_validate(body).expected_navigation_sha256 is None
    for offset in ('conversation_offset','evidence_offset'):
        with pytest.raises(ValidationError): GroupedFindingsRequest.model_validate(body|{offset:1})
        assert GroupedFindingsRequest.model_validate(body|{offset:1,'expected_navigation_sha256':'sha256:'+'b'*64})
    with pytest.raises(ValidationError): FindingsRequest.model_validate(body)
    with pytest.raises(ValidationError): FindingsRequest.model_validate({k:v for k,v in body.items() if k!='view'}|
                                                                      {'expected_navigation_sha256':'sha256:'+'b'*64})
