"""Boot dispatch coverage at the import boundary."""

from __future__ import annotations

import builtins
import sys
import types

from app import packaged_entry


def test_missing_runtime_configuration_selects_provisioning_without_main_import(
    tmp_path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def fail_if_runtime_is_imported(name, *args, **kwargs):
        if name == "app.main":
            raise AssertionError("app.main import boundary was crossed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_if_runtime_is_imported)

    application = packaged_entry.select_brain_application(tmp_path)

    assert application.openapi_url is None


def test_runtime_configuration_presence_selects_runtime_application(tmp_path, monkeypatch) -> None:
    configuration = tmp_path / "runtime.env"
    configuration.touch()
    sentinel = object()
    main_module = types.ModuleType("app.main")
    main_module.app = sentinel

    monkeypatch.setitem(sys.modules, "app.main", main_module)

    assert packaged_entry.select_brain_application(tmp_path) is sentinel
