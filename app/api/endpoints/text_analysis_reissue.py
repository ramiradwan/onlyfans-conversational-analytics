"""Authenticated local reissue controls and nonauthorizing browser entry."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_authenticated_runtime_policy, require_creator, verify_csrf_token, verify_same_origin,
)
from app.onboarding.admission_routes import HEADERS, _browser
from app.security.runtime_policy import RuntimePolicy
from app.security.installation_key import InstallationKeyError
from app.security.text_analysis_contract import parse_json
from app.services.text_analysis_reissue import reissue_service
from app.services.text_analysis_reissue_navigation import ReissueNavigationEntries

router = APIRouter(dependencies=[Depends(require_activated_runtime)])
controls = APIRouter(prefix='/api/v1/nlp/license-reissue', dependencies=[Depends(require_activated_runtime)])
_entries = ReissueNavigationEntries()
_assets = Path(__file__).parents[2] / 'onboarding'
_flow_pattern = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
_states = frozenset({'prepared', 'awaiting_authorization', 'authorized', 'adopted', 'expired'})


def _refuse(status: int, code: str):
    raise HTTPException(status, detail={'code': code}, headers=HEADERS)


def _flow(value):
    if type(value) is not str or _flow_pattern.fullmatch(value) is None:
        _refuse(422, 'text_analysis_reissue_invalid')
    return value


def _service(request: Request, policy: RuntimePolicy):
    require_creator(policy)
    if request.url.query:
        _refuse(422, 'text_analysis_reissue_invalid')
    service = reissue_service()
    if service is None:
        _refuse(503, 'text_analysis_reissue_unavailable')
    return service


def _status(value):
    if value is None:
        return JSONResponse({'current': None}, headers=HEADERS)
    if value.state not in _states or type(value.navigation_available) is not bool:
        _refuse(503, 'text_analysis_reissue_unavailable')
    return JSONResponse({'current': {'flow_id': _flow(value.flow_id), 'state': value.state,
                                    'navigation_available': value.navigation_available}}, headers=HEADERS)


async def _body(request: Request, *, empty: bool = True):
    if (request.url.query or request.headers.getlist('content-type') != ['application/json']
            or request.headers.getlist('content-encoding')):
        _refuse(415, 'text_analysis_reissue_invalid')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 256:
            _refuse(413, 'text_analysis_reissue_invalid')
        raw.extend(chunk)
    try:
        value = parse_json(bytes(raw), maximum=256)
        if type(value) is not dict or (empty and value != {}):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _refuse(422, 'text_analysis_reissue_invalid')


async def _action(request, policy, action):
    _authorize(request, policy)
    await _body(request)
    service = _service(request, policy)
    try:
        return _status(await run_in_threadpool(action, service))
    except ValueError:
        _refuse(409, 'text_analysis_reissue_refused')
    except (OSError, InstallationKeyError):
        _refuse(503, 'text_analysis_reissue_unavailable')


def _authorize(request, policy):
    require_creator(policy)
    if any(len(request.headers.getlist(name)) != 1 for name in ('host', 'origin', 'x-csrf-token')):
        _refuse(403, 'text_analysis_reissue_refused')
    verify_same_origin(request)
    verify_csrf_token(policy, request.headers.get('x-csrf-token'))


@controls.get('')
def status(request: Request, policy=Depends(get_authenticated_runtime_policy)):
    try:
        return _status(_service(request, policy).status(policy))
    except ValueError:
        _refuse(409, 'text_analysis_reissue_refused')
    except (OSError, InstallationKeyError):
        _refuse(503, 'text_analysis_reissue_unavailable')


@controls.post('')
async def begin(request: Request, policy=Depends(get_authenticated_runtime_policy)):
    return await _action(request, policy, lambda service: service.begin(policy))


@controls.post('/{flow_id}/resume')
async def resume(flow_id: str, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    key = _flow(flow_id)
    return await _action(request, policy, lambda service: service.resume(policy, key))


@controls.post('/{flow_id}/browser-entry')
async def browser_entry(flow_id: str, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy)
    await _body(request)
    service, key = _service(request, policy), _flow(flow_id)
    try:
        url = await run_in_threadpool(service.navigation, policy, key)
        if url is None:
            raise ValueError()
        code = _entries.issue((policy.identity.principal_id, key), url)
        return JSONResponse({'entry_code': code}, headers=HEADERS)
    except ValueError:
        _refuse(409, 'text_analysis_reissue_refused')
    except (OSError, InstallationKeyError):
        _refuse(503, 'text_analysis_reissue_unavailable')


@router.get('/text-analysis-reissue/entry', include_in_schema=False)
def entry(request: Request):
    _browser(request)
    if request.url.query:
        _refuse(400, 'text_analysis_reissue_invalid')
    return HTMLResponse((_assets / 'reissue_entry.html').read_text(encoding='utf-8'), headers=HEADERS | {
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    })


@router.get('/text-analysis-reissue/entry.js', include_in_schema=False)
def entry_script(request: Request):
    _browser(request)
    if request.url.query:
        _refuse(400, 'text_analysis_reissue_invalid')
    return Response((_assets / 'reissue_entry.js').read_text(encoding='utf-8'), media_type='text/javascript', headers=HEADERS)


@router.post('/api/v1/nlp/license-reissue/browser-entry/consume', include_in_schema=False)
async def consume_entry(request: Request):
    _browser(request, mutation=True)
    value = await _body(request, empty=False)
    if set(value) != {'code'}:
        _refuse(422, 'text_analysis_reissue_invalid')
    try:
        return JSONResponse({'url': _entries.consume(value['code'])}, headers=HEADERS)
    except ValueError:
        _refuse(401, 'text_analysis_reissue_entry_unavailable')


router.include_router(controls)
