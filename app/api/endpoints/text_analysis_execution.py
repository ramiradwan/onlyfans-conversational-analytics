"""Session-bound enrollment commands without synchronous analysis."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session, account_bound_to_session
from app.api.security import verify_csrf_token, verify_same_origin
from app.models.text_analysis_enrollment import EnrollmentCommand
from app.services.text_analysis_execution import execution_service

router = APIRouter(prefix='/api/v1/nlp/execution', dependencies=[Depends(require_activated_runtime)])


def _service(policy):
    account_bound_to_session(policy, None)
    service = execution_service()
    if service is None:
        raise HTTPException(503, detail={'code': 'text_analysis_unavailable'})
    return service


@router.get('')
def current(policy=Depends(get_authenticated_account_session)):
    service = _service(policy)
    try:
        return service.read_current(policy)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_refused'}) from None


@router.get('/commands/{command_key}')
def receipt(command_key: str, policy=Depends(get_authenticated_account_session)):
    service = _service(policy)
    try:
        result = service.read_command(policy, command_key)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_refused'}) from None
    if result is None:
        raise HTTPException(404, detail={'code': 'text_analysis_command_unknown'})
    return result


@router.post('')
def command(body: EnrollmentCommand, request: Request, policy=Depends(get_authenticated_account_session)):
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))
    service = _service(policy)
    try:
        return service.command(policy, body)
    except ValueError:
        raise HTTPException(409, detail={'code': 'text_analysis_refused'}) from None
