"""The setup presentation has its own compiled entry and restricted asset closure."""
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles

from app.onboarding import surface
from app.onboarding.admission_routes import ORIGIN

pytestmark = [pytest.mark.ci_tier("fast")]


@pytest.fixture
def compiled(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "onboarding").mkdir()
    (tmp_path / "browser-tasks").mkdir()
    (tmp_path / "browser-tasks/index.html").write_text("<h1>Browser task</h1>", encoding="utf-8")
    (tmp_path / "assets/browser.js").write_text("export {};", encoding="utf-8")
    (tmp_path / "onboarding/index.html").write_text('<h1>Setup</h1><script src="/static/dist/assets/setup.js"></script>', encoding="utf-8")
    (tmp_path / "assets/setup.js").write_text("export {};", encoding="utf-8")
    (tmp_path / "assets/dashboard.js").write_text("export {};", encoding="utf-8")
    (tmp_path / "setup-theme-init.js").write_text("document.documentElement.dataset.colorScheme = 'dark';", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "index.html": {"isEntry": True, "file": "assets/dashboard.js"},
        "browser-tasks/index.html": {"isEntry": True, "file": "assets/browser.js"},
        "onboarding/index.html": {"isEntry": True, "file": "assets/setup.js"}}), encoding="utf-8")
    monkeypatch.setattr(surface, "resource_path", lambda _: tmp_path)
    app = FastAPI()
    app.include_router(surface.setup_surface_router())
    return TestClient(app, base_url=ORIGIN), tmp_path


def test_setup_serves_only_its_generated_entry_and_dependency_closure(compiled):
    client, _ = compiled
    response = client.get("/onboarding")
    assert response.status_code == 200
    assert "assets/setup.js" in response.text and "dashboard.js" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    assert client.get("/static/dist/assets/setup.js").status_code == 200
    assert client.get("/static/dist/assets/dashboard.js").status_code == 404
    assert client.get("/static/dist/manifest.json").status_code == 404
    assert client.get("/onboarding", headers={"Host": "localhost:17871"}).status_code == 421


def test_browser_task_entry_has_no_native_channel_or_cookie(compiled):
    client, _ = compiled
    response = client.get("/browser-tasks")
    assert response.status_code == 200 and "Browser task" in response.text
    assert "ipc" not in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    assert client.get("/static/dist/assets/browser.js").status_code == 200
    assert client.get("/browser-tasks?task=1").status_code == 400


@pytest.mark.parametrize("missing", ["manifest.json", "onboarding/index.html", "assets/setup.js", "setup-theme-init.js"])
def test_missing_generated_setup_assets_are_unavailable(compiled, missing):
    client, directory = compiled
    (directory / missing).unlink()
    response = client.get("/onboarding")
    assert response.status_code == 503
    assert response.json() == {"detail": "Setup assets are unavailable"}


def test_manifest_dependency_cannot_escape_generated_assets(compiled):
    client, directory = compiled
    (directory / "manifest.json").write_text(json.dumps({"onboarding/index.html": {
        "isEntry": True, "file": "../outside.js"}}), encoding="utf-8")
    assert client.get("/onboarding").status_code == 503


def test_packaging_requires_both_named_frontend_entries(tmp_path):
    from tools.packaging_policy import PackagingFinding, _check_frontend_closure
    (tmp_path / "manifest.json").write_text(json.dumps({"index.html": {
        "isEntry": True, "file": "dashboard.js"}}), encoding="utf-8")
    (tmp_path / "dashboard.js").write_text("export {};", encoding="utf-8")
    findings = []
    _check_frontend_closure(tmp_path, {"frontend": {"dist_path": ".", "manifest_paths": ["manifest.json"],
        "required_entries": ["index.html", "onboarding/index.html"]}}, findings)
    assert PackagingFinding("frontend_entry_missing", "onboarding/index.html", "required frontend entry is absent") in findings


@pytest.mark.parametrize("entry", ["/onboarding", "/browser-tasks"])
@pytest.mark.parametrize("configured", [False, True], ids=["provisioning", "configured"])
def test_real_compiled_entries_load_before_admission(entry, configured):
    # Provisioning admits a closed asset set; configured Brain mounts /static first.
    # Public Vite files are referenced by HTML but absent from its manifest.
    app = FastAPI()
    if configured:
        app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "app/static"))
    app.include_router(surface.setup_surface_router())
    client = TestClient(app, base_url=ORIGIN)
    page = client.get(entry)
    assert page.status_code == 200
    paths = re.findall(r'(?:src|href)="(/static/[^\"]+)"', page.text)
    assert paths
    if entry == "/onboarding":
        assert "/static/dist/setup-theme-init.js" in paths
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "html" not in response.headers["content-type"], path
    if not configured:
        theme = client.get("/static/dist/setup-theme-init.js")
        assert "javascript" in theme.headers["content-type"]
        assert theme.headers["cache-control"] == "no-store"
        assert theme.headers["x-content-type-options"] == "nosniff"
        assert "set-cookie" not in theme.headers
        assert client.get("/static/dist/setup-theme-init.js", headers={"Host": "outside.example"}).status_code == 421
        assert client.get("/static/dist/manifest.json").status_code == 404
