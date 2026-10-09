"""Stateless return bootstrap for a SameSite=Strict browser session.

The top-level hosted return cannot carry a Strict cookie. This public document
contains no journey or authentication state. Its same-origin script proves the
existing cookie through the authenticated snapshot endpoint before continuing.
"""

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.core.resource_paths import resource_path
from app.provisioning.session import PROVISIONING_HOST


def resume_shell() -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Continue setup</title><main data-onboarding-resume>'
        '<p id="resume-status" role="status">Opening setup…</p></main>'
        '<script type="module" src="/provisioning/resume.js"></script></html>',
        status_code=401,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"},
    )


def resume_script(request: Request) -> Response:
    if request.headers.get("host") != PROVISIONING_HOST:
        raise HTTPException(421, "Local host is required")
    return Response(resource_path("app/provisioning/resume.js").read_bytes(),
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
