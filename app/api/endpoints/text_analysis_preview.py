"""Explicit preview preparation, confirmation and retained shown output."""
from fastapi import APIRouter, Depends, HTTPException, Request, Query

from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session, account_bound_to_session
from app.api.security import verify_csrf_token, verify_same_origin
from app.models.text_analysis_jobs import PreviewConfirmation
from app.models.text_analysis_preview import PreviewContribution
from app.models.text_analysis_preview_execution import (
    PreviewPrepareRequest,
    PreviewOutcome,
    PreviewSavedEnvelope,
    PreviewSavedPage,
)
from app.services.text_analysis_preview import preview_service

router = APIRouter(prefix='/api/v1/nlp/previews', dependencies=[Depends(require_activated_runtime)])


def _service(policy):
    account_bound_to_session(policy, None)
    service = preview_service()
    if service is None:
        raise HTTPException(503, detail={'code': 'text_analysis_preview_unavailable'})
    return service


@router.get('/results/{contribution_id}', response_model=PreviewContribution)
def shown(contribution_id: str, policy=Depends(get_authenticated_account_session)):
    try:
        return _service(policy).read_shown(policy, contribution_id)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_preview_refused'}) from None


@router.get('/results/{contribution_id}/view', response_model=PreviewSavedEnvelope)
def saved_view(contribution_id: str, policy=Depends(get_authenticated_account_session)):
    try:
        return _service(policy).read_saved_view(policy, contribution_id)
    except ValueError:
        raise HTTPException(
            409, detail={'code': 'text_analysis_preview_refused'}
        ) from None


@router.get('/saved', response_model=PreviewSavedPage)
def saved(
    after_contribution: str = '',
    limit: int = Query(64, ge=1, le=64),
    policy=Depends(get_authenticated_account_session),
):
    try:
        return _service(policy).saved_list(
            policy, after_contribution=after_contribution, limit=limit
        )
    except ValueError:
        raise HTTPException(
            409, detail={'code': 'text_analysis_preview_refused'}
        ) from None


@router.get('/{external_key}', response_model=PreviewOutcome)
def outcome(external_key: str, policy=Depends(get_authenticated_account_session)):
    try:
        result = _service(policy).read_outcome(policy, external_key)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_preview_refused'}) from None
    if result is None:
        raise HTTPException(404, detail={'code': 'text_analysis_preview_unknown'})
    return result


@router.post('', response_model=PreviewOutcome)
def prepare(body: PreviewPrepareRequest, request: Request, policy=Depends(get_authenticated_account_session)):
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))
    try:
        return _service(policy).prepare(policy, body)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_preview_refused'}) from None


@router.post('/{external_key}/confirm', response_model=PreviewOutcome)
def confirm(external_key: str, body: PreviewConfirmation, request: Request,
            policy=Depends(get_authenticated_account_session)):
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))
    try:
        return _service(policy).confirm(policy, external_key, body)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_preview_refused'}) from None
