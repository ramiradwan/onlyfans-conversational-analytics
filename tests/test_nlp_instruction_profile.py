"""Instruction topic descriptors retain a closed embedded-tokenizer identity."""
import copy
import json
from pathlib import Path
import pytest
from app.nlp.profiles import (Artifact, BASELINE_RESOURCE_POLICY, ResourcePolicy,
    artifact_closure_digest, read_profile, validate_resource_policy)
from app.nlp.contracts import NlpBoundaryError

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]

def descriptor():
    value=json.loads((ROOT/'shared/nlp/v2/fixtures/split-profile.json').read_bytes())
    value['schema_version']='nlp-profile.v3'
    topic=value['components'][0]
    topic['adapter'].update(adapter_id='instruction_topics',architecture='llama-b11669-cpu-v1',
        artifact_format='gguf-model.v1',tokenizer_format='gguf-embedded.v1')
    topic['inference_threads']=8
    topic['artifact_ids']=['weights.topics','topic.system','topic.grammar','topic.config']
    topic['executed_labels']=[{'task':'message_topics','label_id':f'topic.{i}', 'description':f'Category {i}'} for i in range(1,19)]
    topic['tasks'][0]['enabled_ids']=[l['label_id'] for l in topic['executed_labels']]
    topic['tasks'][0]['score_policy'].update(kind='none',semantics_id='unscored',calibration='not_calibrated',calibration_sha256=None)
    weights=next(a for a in value['artifacts'] if a['artifact_id']=='weights.topics')
    weights.update(format_id='gguf-model.v1',size_bytes=600_000_000)
    for name,fmt in [('system','instruction-system-utf8.v1'),('grammar','llama-gbnf.v1'),('config','instruction-topic-config.v1')]:
        value['artifacts'].append(dict(artifact_id='topic.'+name,sha256='e'*64,size_bytes=1,role='configuration',format_id=fmt))
    topic['artifact_closure_sha256']=artifact_closure_digest(tuple(Artifact.model_validate_json(json.dumps(a)) for a in value['artifacts']
        if a['role'] in ('engine','runtime') or a['artifact_id'] in topic['artifact_ids']))
    topic['tokenizer_sha256']=artifact_closure_digest((Artifact.model_validate_json(json.dumps(weights)),))
    value['resources'].update(download_bytes=1_000_000_000,installed_bytes=1_000_000_000,
        transition_peak_memory_bytes=8_000_000_000,inference_threads=8)
    return value

def parse(value):
    return read_profile(json.dumps(value))

def test_embedded_tokenizer_dual_task_composition_requires_explicit_resource_policy():
    profile=parse(descriptor())
    assert len(profile.bindings)==2 and profile.resources.inference_threads==8
    assert profile.components[0].tokenizer_sha256 != artifact_closure_digest(())
    with pytest.raises(NlpBoundaryError,match='nlp_resource_limit'):
        validate_resource_policy(profile,BASELINE_RESOURCE_POLICY)
    policy=ResourcePolicy(download_bytes=1_000_000_000,installed_bytes=1_000_000_000,
        temporary_bytes=9_000_000_000,transition_peak_memory_bytes=8_000_000_000,inference_threads=8)
    validate_resource_policy(profile,policy)
    assert BASELINE_RESOURCE_POLICY.inference_threads==2

@pytest.mark.parametrize('change',['vocabulary','format','scores','threads','adapter','engine'])
def test_incompatible_instruction_compositions_are_rejected(change):
    value=descriptor();topic=value['components'][0]
    if change=='vocabulary':topic['tokenizer_sha256']=artifact_closure_digest(())
    elif change=='format':topic['adapter']['tokenizer_format']='none'
    elif change=='scores':topic['tasks'][0]['score_policy']['kind']='probability'
    elif change=='threads':topic['inference_threads']=4
    elif change=='adapter':topic['adapter']['architecture']='boundary-shared-v1'
    elif change=='engine':topic['engine']['engine_build_sha256']='f'*64
    with pytest.raises(ValueError):parse(value)
