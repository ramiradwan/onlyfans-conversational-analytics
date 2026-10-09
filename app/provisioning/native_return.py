"""Credential-free native launch return; navigation never grants runtime access."""

import html
import re

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.core.resource_paths import resource_path
from app.persistence.onboarding import require_journey
from app.provisioning.session import PROVISIONING_HOST

NATIVE_RETURN_PATH = "/provisioning/native-return"


def native_journey(request: Request) -> str | None:
    """Read only the optional, nonauthorizing native-launch locator."""
    values = request.query_params.getlist("native_journey")
    if not values:
        return None
    try:
        if len(values) != 1:
            raise ValueError("Ambiguous journey")
        return require_journey(values[0])
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Journey is invalid") from None


def _host(request: Request) -> None:
    if request.headers.get("host") != PROVISIONING_HOST:
        raise HTTPException(421, "Local host is required")
    if request.query_params:
        raise HTTPException(400, "Unexpected return parameters")


def native_return_shell(request: Request, extension_id: str) -> HTMLResponse:
    _host(request)
    identity = extension_id if re.fullmatch(r"[a-p]{32}", extension_id) else ""
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Continue setup</title><main data-native-return '
        f'data-extension-id="{html.escape(identity, quote=True)}">'
        '<h1>Continue setup</h1><p id="native-return-status" role="status">Opening your setup tab…</p>'
        '<button id="native-return-retry" type="button" hidden>Try again</button>'
        '<button id="native-return-focus" type="button" hidden>Go to setup</button></main>'
        '<script type="module" src="/provisioning/native-return.js"></script></html>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"},
    )


def native_return_script(request: Request) -> Response:
    _host(request)
    path = ("shared/onboarding/json.mjs" if request.url.path == "/provisioning/native-json.mjs"
            else "app/provisioning/native-return.js")
    return Response(resource_path(path).read_bytes(),
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
