"""Authenticated local acquisition controls and one-use browser handoff."""
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.activation import require_activated_runtime
from app.api.security import get_authenticated_runtime_policy, require_creator, verify_csrf_token, verify_same_origin
from app.onboarding.admission_routes import HEADERS, _browser
from app.security.installation_key import InstallationKeyError
from app.security.text_analysis_contract import parse_json
from app.services.text_analysis_native_acquisition import native_text_analysis_acquisition
from app.services.text_analysis_reissue_navigation import ReissueNavigationEntries

Kind = Literal["paid", "preview"]
_PREFIX = "/api/v1/nlp/text-analysis-acquisition"
_ASSETS = Path(__file__).parents[2] / "onboarding"
_ENTRIES = {kind: ReissueNavigationEntries(
    path_pattern=rf"/onboarding/text-analysis-{kind}-acquisitions/[A-Za-z0-9_-]{{43}}")
    for kind in ("paid", "preview")}
router = APIRouter(dependencies=[Depends(require_activated_runtime)])


def _refuse(status, reason):
    raise HTTPException(status, detail={"code": "text_analysis_acquisition_" + reason}, headers=HEADERS)


def _id(value):
    try:
        identifier = UUID(value)
        if identifier.version == 4 and str(identifier) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    _refuse(422, "invalid")


def _authorize(request, policy, *, mutation=False):
    require_creator(policy)
    if request.url.query:
        _refuse(422, "invalid")
    if mutation:
        if any(len(request.headers.getlist(name)) != 1 for name in ("host", "origin", "x-csrf-token")):
            _refuse(403, "refused")
        verify_same_origin(request)
        verify_csrf_token(policy, request.headers.get("x-csrf-token"))


async def _body(request, fields=()):
    if (request.url.query or request.headers.getlist("content-type") != ["application/json"]
            or request.headers.getlist("content-encoding")):
        _refuse(415, "invalid")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 256:
            _refuse(413, "invalid")
        raw.extend(chunk)
    try:
        value = parse_json(bytes(raw), maximum=256)
        if type(value) is dict and set(value) == set(fields):
            return value
    except ValueError:
        pass
    _refuse(422, "invalid")


def _service():
    value = native_text_analysis_acquisition()
    if value is None:
        _refuse(503, "unavailable")
    return value


async def _call(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except ValueError:
        _refuse(409, "refused")
    except (OSError, InstallationKeyError):
        _refuse(503, "unavailable")


@router.get(_PREFIX + "/{kind}")
async def status(kind: Kind, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy)
    service = native_text_analysis_acquisition()
    value = {"current": None, "eligible": False, "reason": "disabled"}
    if service is not None:
        value = await _call(service.status, policy, kind)
    return JSONResponse(value, headers=HEADERS)


@router.post(_PREFIX + "/{kind}/prepare")
async def prepare(kind: Kind, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy, mutation=True)
    await _body(request)
    result = await _call(_service().prepare, policy, kind)
    return JSONResponse({"current": result}, headers=HEADERS)


@router.post(_PREFIX + "/{kind}/{flow_id}/read")
async def read(kind: Kind, flow_id: str, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy, mutation=True)
    await _body(request)
    result = await _call(_service().read, policy, kind, _id(flow_id))
    return JSONResponse({"current": result}, headers=HEADERS)


@router.post(_PREFIX + "/{kind}/{flow_id}/submit")
async def submit(kind: Kind, flow_id: str, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy, mutation=True)
    value = await _body(request, ("review_id",))
    result = await _call(_service().submit, policy, kind, _id(flow_id), _id(value["review_id"]))
    return JSONResponse({"current": result}, headers=HEADERS)


@router.post(_PREFIX + "/{kind}/{flow_id}/browser-entry")
async def browser_entry(kind: Kind, flow_id: str, request: Request, policy=Depends(get_authenticated_runtime_policy)):
    _authorize(request, policy, mutation=True)
    await _body(request)
    flow = _id(flow_id)
    url = await _call(_service().navigation, policy, kind, flow)
    code = await _call(_ENTRIES[kind].issue, (policy.identity.principal_id, flow), url)
    return JSONResponse({"entry_code": code}, headers=HEADERS)


@router.get("/text-analysis-acquisition/{kind}/entry", include_in_schema=False)
def entry(kind: Kind, request: Request):
    _browser(request)
    if request.url.query:
        _refuse(422, "invalid")
    return HTMLResponse((_ASSETS / "text_analysis_acquisition_entry.html").read_text(encoding="utf-8"),
        headers=HEADERS | {"Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})


@router.get("/text-analysis-acquisition/{kind}/entry.js", include_in_schema=False)
def script(kind: Kind, request: Request):
    _browser(request)
    if request.url.query:
        _refuse(422, "invalid")
    return Response((_ASSETS / "text_analysis_acquisition_entry.js").read_text(encoding="utf-8"),
                    media_type="text/javascript", headers=HEADERS)


@router.post(_PREFIX + "/{kind}/browser-entry/consume", include_in_schema=False)
async def consume(kind: Kind, request: Request):
    _browser(request, mutation=True)
    value = await _body(request, ("code",))
    try:
        return JSONResponse({"url": _ENTRIES[kind].consume(value["code"])}, headers=HEADERS)
    except ValueError:
        _refuse(401, "entry_unavailable")
