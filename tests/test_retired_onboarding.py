"""The current package cannot serve or restore the retired browser wizard."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.onboarding.admission_routes import ORIGIN
from app.onboarding.surface import setup_surface_router
from app.provisioning.app import create_provisioning_app

pytestmark = [pytest.mark.ci_tier("fast")]
ROOT = Path(__file__).resolve().parents[1]
RETIRED_ASSETS = [
    "provisioning.html",
    "provisioning.js",
    "native-return.js",
    "native-workspace.mjs",
    "native-return.css",
    "native-tokens.css",
    "resume.js",
]
RETIRED_PATHS = [
    "/provisioning",
    "/provisioning/native-return",
    "/provisioning/resume",
    "/provisioning/activation",
    "/api/v1/provisioning/native-entry",
    "/api/v1/provisioning/native-workspace",
    "/api/v1/provisioning/claim",
    "/api/v1/provisioning/status",
    "/api/v1/provisioning/finalize",
    "/api/v1/onboarding/session",
    "/api/v1/session/handoff",
    "/api/v1/session/bootstrap",
    "/api/v1/desktop/recovery",
]


def test_retired_assets_are_absent_from_source_and_forbidden_by_package_policy():
    policy = json.loads(
        (ROOT / "packaging/runtime-files.json").read_text(encoding="utf-8")
    )
    for asset in RETIRED_ASSETS:
        assert not (ROOT / "app/provisioning" / asset).exists()
        name = "_internal/app/provisioning/" + asset
        assert name in policy["forbidden_paths"]
        assert name not in policy["required_files"]
    manifest = json.loads(
        (ROOT / "app/static/dist/manifest.json").read_text(encoding="utf-8")
    )
    assert {
        "index.html",
        "onboarding/index.html",
        "browser-tasks/index.html",
        "setup-code/index.html",
    } <= manifest.keys()
    assert not any(any(asset in key for asset in RETIRED_ASSETS) for key in manifest)


@pytest.mark.parametrize(
    "path", RETIRED_PATHS + ["/provisioning/" + asset for asset in RETIRED_ASSETS]
)
def test_restricted_application_has_no_retired_alias(path):
    app = create_provisioning_app()
    app.include_router(setup_surface_router())
    client = TestClient(app, base_url=ORIGIN)
    assert client.get(path).status_code == 404
    assert client.post(path, json={}).status_code in {404, 405}


@pytest.mark.parametrize("path", RETIRED_PATHS)
def test_runtime_frontend_cannot_turn_retired_paths_into_dashboard_aliases(path):
    from app.api.endpoints.frontend import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url=ORIGIN)
    assert client.get(path).status_code in {401, 404}


def test_extension_has_no_native_messaging_or_retired_full_state_channel():
    manifest = json.loads(
        (ROOT / "extension/manifest.json").read_text(encoding="utf-8")
    )
    assert "nativeMessaging" not in manifest.get("permissions", []) + manifest.get(
        "optional_permissions", []
    )
    assert not (ROOT / "extension/runtime/onboarding-port.mjs").exists()
    assert not (ROOT / "extension/runtime/onboarding-workspace-probe.mjs").exists()
    for folder in [
        "extension/runtime",
        "extension/transport",
        "extension/ui",
        "frontend/src",
        "frontend/onboarding",
    ]:
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in {".js", ".mjs", ".ts", ".tsx"}:
                source = path.read_text(encoding="utf-8")
                assert (
                    "connectNative(" not in source
                    and "sendNativeMessage(" not in source
                ), path
