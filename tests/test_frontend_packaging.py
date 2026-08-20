from pathlib import Path
import sys
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

    assert "http://localhost:5173" not in response.text
    assert response.status_code == 500
    assert "Production frontend packaging error" in response.text


def test_template_never_renders_development_server_origin() -> None:
    rendered = frontend.templates.get_template("index.html").render(
        app_script=None,
        css_files=[],
        config={},
        csrf_token=None,
        development_script="http://localhost:5173/src/main.tsx",
    )

    assert "http://localhost:5173" not in rendered, (
        "the production template must not render a development-server origin"
    )


def test_development_missing_manifest_never_serves_development_script(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad({}, "Vite manifest is absent from the compiled frontend package"),
    )

    response = _frontend_client().get("/")

    assert response.status_code == 200
    assert "http://localhost:5173" not in response.text


def test_production_manifest_without_entry_fails_as_a_packaging_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(frontend, "_manifest_load", frontend.ManifestLoad({"chunk.js": {}}))

    response = _frontend_client().get("/")

    assert response.status_code == 500
    assert "Vite manifest has no entry" in response.text


def test_production_manifest_rejects_asset_that_escapes_dist_root(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_root = tmp_path / "bundle"
    dist_root = bundle_root / "app" / "static" / "dist"
    dist_root.mkdir(parents=True)
    escaped_asset = bundle_root / "app" / "static" / "escaped.js"
    escaped_asset.write_text("escaped", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(frontend, "DIST_DIR", dist_root)
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad({"entry": {"isEntry": True, "file": "../escaped.js"}}),
    )

    response = _frontend_client().get("/")

    assert response.status_code == 500
    assert "Vite manifest frontend asset is invalid" in response.text


def test_production_manifest_rejects_missing_css_asset(monkeypatch, tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    dist_root = bundle_root / "app" / "static" / "dist"
    dist_root.mkdir(parents=True)
    (dist_root / "entry.js").write_text("entry", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(frontend, "DIST_DIR", dist_root)
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad(
            {
                "entry": {
                    "isEntry": True,
                    "file": "entry.js",
                    "css": ["missing.css"],
                }
            }
        ),
    )

    response = _frontend_client().get("/")

    assert response.status_code == 500
    assert "Vite manifest frontend asset is invalid" in response.text


def test_production_manifest_rejects_css_asset_that_escapes_dist_root(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_root = tmp_path / "bundle"
    dist_root = bundle_root / "app" / "static" / "dist"
    dist_root.mkdir(parents=True)
    (dist_root / "entry.js").write_text("entry", encoding="utf-8")
    escaped_css = bundle_root / "app" / "static" / "escaped.css"
    escaped_css.write_text("escaped", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(frontend, "DIST_DIR", dist_root)
    monkeypatch.setattr(
        frontend,
        "_manifest_load",
        frontend.ManifestLoad(
            {
                "entry": {
                    "isEntry": True,
                    "file": "entry.js",
                    "css": ["../escaped.css"],
                }
            }
        ),
    )

    response = _frontend_client().get("/")

    assert response.status_code == 500
    assert "Vite manifest frontend asset is invalid" in response.text


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
