"""Generated setup presentation, independent of runtime settings and analytics."""
import json
from html import escape
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.core.customer_release import CustomerReleaseConfigurationError, load_customer_release_config
from app.core.resource_paths import ResourcePathError, resource_path
from app.onboarding.admission_routes import HEADERS, _host

_SUPPORT_TEMPLATE = '<template id="support-channels"></template>'
_PICKER_MARKER = 'data-microsoft-picker="false"'


def _with_release_presentation(document: str) -> str:
    if _SUPPORT_TEMPLATE not in document and _PICKER_MARKER not in document:
        return document
    try:
        release = load_customer_release_config()
    except CustomerReleaseConfigurationError:
        return document
    if release.has_microsoft_picker:
        document = document.replace(_PICKER_MARKER, 'data-microsoft-picker="true"', 1)
    entries = "".join(f'<span data-channel="{channel}">{escape(address)}</span>' for channel, address in release.support_channels)
    return document.replace(_SUPPORT_TEMPLATE, f'<template id="support-channels">{entries}</template>', 1)


def _assets(entry_name="onboarding/index.html"):
    try:
        root = resource_path("app/static/dist")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest[entry_name]
        if entry.get("isEntry") is not True:
            raise ValueError()
        # Vite copies this blocking public script without a manifest entry.
        # Keep it explicit so provisioning admits it before theme hydration.
        pending, seen, paths = [entry_name], set(), {"setup-theme-init.js"}
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            item = manifest[name]
            paths.update([item["file"], *item.get("css", []), *item.get("assets", [])])
            pending.extend(item.get("imports", []) + item.get("dynamicImports", []))
        for path in paths:
            parsed = PurePosixPath(path)
            if (parsed.is_absolute() or ".." in parsed.parts or "\\" in path
                    or not (root / path).is_file() or not (root / path).resolve().is_relative_to(root.resolve())):
                raise ValueError()
        return root, paths
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ResourcePathError):
        raise HTTPException(503, "Setup assets are unavailable") from None


def setup_shell(request: Request, *, browser=False, receiving_origin=None):
    _host(request)
    if request.url.query:
        raise HTTPException(400, "Invalid setup request")
    entry = "setup-code/index.html" if receiving_origin else "browser-tasks/index.html" if browser else "onboarding/index.html"
    root, _ = _assets(entry)
    try:
        document = (root / entry).read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(503, "Setup assets are unavailable") from None
    if not receiving_origin:
        document = _with_release_presentation(document)
    return HTMLResponse(document, headers=HEADERS | {"Content-Security-Policy":
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self' data:; "
        "img-src 'self' data:; connect-src 'self' " + ("; " if browser or receiving_origin else "ipc: http://ipc.localhost; ") +
        "frame-ancestors 'none'; base-uri 'none'; form-action " + (receiving_origin or "'none'")})


def setup_surface_router():
    router = APIRouter()

    @router.get("/onboarding", include_in_schema=False)
    def shell(request: Request):
        return setup_shell(request)

    @router.get("/browser-tasks", include_in_schema=False)
    def browser_shell(request: Request):
        return setup_shell(request, browser=True)

    @router.get("/static/dist/{path:path}", include_in_schema=False)
    def asset(request: Request, path: str):
        _host(request)
        root, allowed = _assets()
        _, browser_allowed = _assets("browser-tasks/index.html")
        allowed |= browser_allowed
        if getattr(request.app.state, "receiving_sessions", None) is not None:
            _, receiving_allowed = _assets("setup-code/index.html")
            allowed |= receiving_allowed
        if path not in allowed:
            raise HTTPException(404, "Not found")
        return FileResponse(root / path, headers=HEADERS)

    return router
