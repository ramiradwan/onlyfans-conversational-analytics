"""Session-bound acknowledgement and original-actor recovery."""

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from app.api.activation import require_activated_runtime
from app.api.dependencies import (
    get_authenticated_account_session,
    account_bound_to_session,
)
from app.api.security import verify_csrf_token, verify_same_origin
from app.models.text_analysis_client import (
    ClientIntentPut,
    ClientIntentRecord,
    ClientIntentPage,
    ClientIntentCursor,
    ClientIntentKind,
)
from app.services.text_analysis_client import client_service

router = APIRouter(
    prefix='/api/v1/nlp/client-intents',
    dependencies=[Depends(require_activated_runtime)],
)


def _service(policy):
    account_bound_to_session(policy, None)
    service = client_service()
    if service is None:
        raise HTTPException(503, detail={'code': 'text_analysis_client_unavailable'})
    return service


@router.post('', response_model=ClientIntentRecord)
def retain(
    body: ClientIntentPut,
    request: Request,
    policy=Depends(get_authenticated_account_session),
):
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))
    try:
        return _service(policy).retain(policy, body)
    except ValueError:
        raise HTTPException(
            409, detail={'code': 'text_analysis_client_refused'}
        ) from None


@router.get('', response_model=ClientIntentPage)
def retained_list(
    after_kind: ClientIntentKind | None = None,
    after_key: str | None = None,
    limit: int = Query(64, ge=1, le=64),
    policy=Depends(get_authenticated_account_session),
):
    try:
        if (after_kind is None) != (after_key is None):
            raise ValueError('text_analysis_client_page_invalid')
        after = (
            None
            if after_kind is None
            else ClientIntentCursor(kind=after_kind, key=after_key)
        )
        return _service(policy).list(policy, after=after, limit=limit)
    except ValueError:
        raise HTTPException(
            409, detail={'code': 'text_analysis_client_refused'}
        ) from None


@router.get('/{kind}/{key}', response_model=ClientIntentRecord)
def retained(
    kind: ClientIntentKind, key: str, policy=Depends(get_authenticated_account_session)
):
    try:
        value = _service(policy).read(policy, kind, key)
    except ValueError:
        raise HTTPException(
            409, detail={'code': 'text_analysis_client_refused'}
        ) from None
    if value is None:
        raise HTTPException(404, detail={'code': 'text_analysis_client_unknown'})
    return value
