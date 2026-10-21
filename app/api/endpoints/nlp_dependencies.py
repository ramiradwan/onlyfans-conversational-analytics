"""Session-bound explicit dependency operations; reads have no acquisition effects."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session, account_bound_to_session
from app.api.security import verify_csrf_token, verify_same_origin
from app.nlp.contracts import Contract, Identifier
from app.services.nlp_dependencies import dependency_service

router = APIRouter(prefix='/api/v1/nlp/dependencies', dependencies=[Depends(require_activated_runtime)])


class DependencyAction(Contract):
    action: Literal['refresh', 'plan', 'acquire', 'pause', 'cancel', 'resume', 'prepare', 'select', 'rollback', 'remove',
                    'account_pause', 'account_enable']
    profile_id: Identifier | None = None
    revision: Identifier | None = None
    operation_id: Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')] | None = None
    profile_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')] | None = None


@router.get('')
def status(operation_id: Annotated[str | None, Field(pattern=r'^[0-9a-f]{32}$')] = None,
           policy=Depends(get_authenticated_account_session)):
    account = account_bound_to_session(policy, None)
    service = dependency_service()
    if service is not None:
        try:
            return service.observation(operation_id) if operation_id else service.status(account)
        except (ValueError, KeyError):
            raise HTTPException(404, detail={'code':'nlp_operation_unknown'}) from None
    return {
        'configured': False, 'available_profiles': [], 'profiles': [], 'selected': None, 'acquisitions': [],
        'failure': 'nlp_unavailable',
        'resume_granularity': 'verified_files_current_file_restarts'}


@router.post('')
def act(body: DependencyAction, request: Request, policy=Depends(get_authenticated_account_session)):
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))
    account = account_bound_to_session(policy, None)
    service = dependency_service()
    if service is None:
        raise HTTPException(503, detail={'code': 'nlp_unavailable'})
    try:
        action = body.action
        if action in ('acquire', 'select', 'plan'):
            if body.profile_id is None or body.revision is None:
                raise ValueError()
            if action == 'select':
                value = service.select(account, body.profile_id, body.revision)
            elif action == 'plan':
                value = service.plan(body.profile_id, body.revision)
            else:
                if body.operation_id is None:
                    raise ValueError()
                value = service.acquire(body.profile_id, body.revision, plan_id=body.operation_id)
        elif action in ('pause', 'cancel', 'resume'):
            if body.operation_id is None:
                raise ValueError()
            value = service.resume(body.operation_id) if action == 'resume' else service.pause(body.operation_id, cancel=action == 'cancel')
        elif action == 'prepare':
            if body.profile_sha256 is None:
                raise ValueError()
            value = service.prepare_installed(body.profile_sha256)
        elif action == 'rollback':
            value = service.select(account, None, None, rollback=True)
        elif action == 'remove':
            value = service.remove(account, body.profile_id, body.revision)
        elif action in ('account_pause', 'account_enable'):
            value = service.account_mode(account, 'paused' if action == 'account_pause' else 'enabled')
        else:
            value = service.refresh()
        return {'operation': value, 'status': service.status(account)}
    except Exception:
        raise HTTPException(409, detail={'code': 'nlp_dependency_refused'}) from None
