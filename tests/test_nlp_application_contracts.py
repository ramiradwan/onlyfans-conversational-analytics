"""Shared local execution examples preserve closed request and output meanings."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from app.models.text_analysis_client import ClientIntentPut, ClientIntentRecord, ClientIntentPage
from app.models.text_analysis_enrollment import EnrollmentCommand, EnrollmentCommandResult, EnrollmentView
from app.models.text_analysis_jobs import PreviewConfirmation
from app.models.text_analysis_preview_execution import (
    PreviewOutcome, PreviewPrepareRequest, PreviewSavedEnvelope, PreviewSavedPage,
)
from tools.generate_nlp_contracts import documents_application, fixtures_application

pytestmark = pytest.mark.ci_tier('fast')
ROOT = Path(__file__).resolve().parents[1] / 'shared/nlp/application/v1'
MODELS = {
    'enrollment-command': EnrollmentCommand, 'enrollment-view': EnrollmentView,
    'enrollment-result': EnrollmentCommandResult, 'preview-prepare': PreviewPrepareRequest,
    'preview-outcome': PreviewOutcome, 'preview-confirmation': PreviewConfirmation,
    'preview-saved': PreviewSavedEnvelope, 'preview-saved-partial': PreviewSavedEnvelope,
    'client-enrollment-intent': ClientIntentPut, 'client-enrollment-record': ClientIntentRecord,
    'client-prepare-intent': ClientIntentPut, 'client-confirm-intent': ClientIntentPut,
    'client-intent-page': ClientIntentPage, 'preview-saved-page': PreviewSavedPage,
}


def fixture(name):
    return json.loads((ROOT / 'fixtures' / (name + '.json')).read_text('utf-8'))


def test_generated_application_schemas_and_fixtures_match_their_owners():
    for name, schema in documents_application().items():
        assert json.loads((ROOT / name).read_text('utf-8')) == schema
        Draft202012Validator.check_schema(schema)
    for name, value in fixtures_application().items():
        assert json.loads((ROOT / 'fixtures' / name).read_text('utf-8')) == value


@pytest.mark.parametrize('name', MODELS)
def test_shared_application_examples_validate_as_closed_typed_records(name):
    model, value = MODELS[name], fixture(name)
    assert model.model_validate_json(json.dumps(value)).model_dump(mode='json') == value
    Draft202012Validator(model.model_json_schema()).validate(value)
    with pytest.raises(ValueError):
        model.model_validate_json(json.dumps({**value, 'hidden_candidates': []}))


@pytest.mark.parametrize('name', ['client-enrollment-intent', 'client-prepare-intent', 'client-confirm-intent'])
def test_retained_intent_key_cannot_replace_its_original_body_key(name):
    value = fixture(name)
    value['key'] = '00000000-0000-4000-8000-000000000099'
    with pytest.raises(ValueError):
        ClientIntentPut.model_validate_json(json.dumps(value))


@pytest.mark.parametrize('fault', ['occurrences', 'duplicate', 'span', 'coverage', 'total'])
def test_saved_summary_does_not_accept_changed_units_or_incomplete_totals(fault):
    value = fixture('preview-saved-partial' if fault == 'total' else 'preview-saved')
    summary = value['output']['summaries'][1]
    if fault == 'occurrences':
        summary['occurrences'] = summary['supporting_messages']
    elif fault == 'duplicate':
        summary['evidence'].append(deepcopy(summary['evidence'][0]))
    elif fault == 'span':
        summary['evidence'][0]['reference']['span']['end'] = 12
    elif fault == 'coverage':
        value['output']['tasks'][1]['assessed'] = 0
    else:
        summary['additional_categories'] = 0
    with pytest.raises(ValueError):
        PreviewSavedEnvelope.model_validate_json(json.dumps(value))
