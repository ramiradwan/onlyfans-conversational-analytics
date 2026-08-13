from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import frontend
from app.core.config import settings


def _frontend_client() -> TestClient:
    application = FastAPI()
    application.include_router(frontend.router)
    application.dependency_overrides[frontend.get_runtime_policy] = (
        lambda: SimpleNamespace(identity=None)
    )
    return TestClient(application, raise_server_exceptions=False)


def test_production_missing_manifest_never_serves_development_script(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad({}, "Vite manifest is absent from the compiled frontend package"),
    )

    response = _frontend_client().get("/")

    assert frontend.DEVELOPMENT_SCRIPT_URL not in response.text
    assert response.status_code == 500
    assert "Production frontend packaging error" in response.text


def test_development_missing_manifest_keeps_vite_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad({}, "Vite manifest is absent from the compiled frontend package"),
    )

    response = _frontend_client().get("/")

    assert response.status_code == 200
    assert frontend.DEVELOPMENT_SCRIPT_URL in response.text


def test_production_manifest_without_entry_fails_as_a_packaging_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(frontend, "_manifest_load", frontend.ManifestLoad({"chunk.js": {}}))

    response = _frontend_client().get("/")

    assert response.status_code == 500
    assert "Vite manifest has no entry" in response.text


def test_migration_catalogs_are_lf_only() -> None:
    root = Path(__file__).resolve().parents[1]
    catalogs = (
        "app/persistence/sql",
        "app/persistence/auth_sql",
        "app/persistence/projection_sql",
        "app/analytics/sql",
    )
    for catalog in catalogs:
        for sql_file in (root / catalog).rglob("*.sql"):
            assert b"\r\n" not in sql_file.read_bytes(), sql_file.relative_to(root)
