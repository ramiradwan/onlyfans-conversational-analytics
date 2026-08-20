import importlib
from pathlib import Path
import sys

from fastapi.staticfiles import StaticFiles
import pytest

from app.core import resource_paths


ROOT = Path(__file__).resolve().parents[1]


def test_static_mount_is_absolute_and_independent_of_current_working_directory(
    monkeypatch, tmp_path: Path
) -> None:
    """The application must not bind its static mount to its launch directory."""

    (tmp_path / "app" / "static").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("app.main", None)

    main = importlib.import_module("app.main")
    static_mount = next(
        route.app
        for route in main.app.routes
        if isinstance(getattr(route, "app", None), StaticFiles)
    )

    assert Path(static_mount.directory) == ROOT / "app" / "static", (
        "the static mount must use the source resource root, not the current directory"
    )
    assert Path(static_mount.directory).is_absolute(), (
        "the static mount directory must be absolute"
    )


def test_resource_path_resolves_from_source_root() -> None:
    assert resource_paths.resource_path("app/templates") == ROOT / "app" / "templates"


def test_resource_path_resolves_from_frozen_bundle_root(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_root = tmp_path / "bundle"
    resource = bundle_root / "app" / "templates" / "index.html"
    resource.parent.mkdir(parents=True)
    resource.write_text("template", encoding="utf-8")
    monkeypatch.setattr(resource_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resource_paths.sys, "_MEIPASS", str(bundle_root), raising=False)

    assert resource_paths.resource_path("app/templates/index.html") == resource


def test_resource_path_rejects_existing_reference_that_escapes_resource_root(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    outside_resource = tmp_path / "outside.txt"
    outside_resource.write_text("outside", encoding="utf-8")
    monkeypatch.setattr(resource_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resource_paths.sys, "_MEIPASS", str(bundle_root), raising=False)

    with pytest.raises(
        resource_paths.ResourcePathError,
        match="resource reference escapes resource root",
    ):
        resource_paths.resource_path("../outside.txt")


def test_resource_path_rejects_missing_resource() -> None:
    with pytest.raises(FileNotFoundError, match="resource does not exist"):
        resource_paths.resource_path("app/missing-resource")


def test_resource_path_rejects_reference_that_escapes_declared_resource_root(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_root = tmp_path / "bundle"
    declared_root = bundle_root / "app" / "static" / "dist"
    declared_root.mkdir(parents=True)
    outside_resource = bundle_root / "app" / "static" / "outside.js"
    outside_resource.write_text("outside", encoding="utf-8")
    monkeypatch.setattr(resource_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resource_paths.sys, "_MEIPASS", str(bundle_root), raising=False)

    with pytest.raises(
        resource_paths.ResourcePathError,
        match="resource reference escapes resource root",
    ):
        resource_paths.resource_path("../outside.js", root=declared_root)
