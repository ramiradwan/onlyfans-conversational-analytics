"""Tests for the declarative clean-machine package policy."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from tools.packaging_policy import load_runtime_policy, verify_runtime_files


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "packaging" / "runtime-files.json"
SQL_CATALOGS = (
    "app/persistence/sql",
    "app/persistence/auth_sql",
    "app/persistence/projection_sql",
    "app/analytics/sql",
)


def _stage_runtime_tree(tmp_path: Path) -> Path:
    """Make a minimal onedir layout from immutable checkout resources."""

    stage = tmp_path / "stage"
    internal = stage / "_internal"
    (stage / "Brain.exe").parent.mkdir(parents=True)
    (stage / "Brain.exe").write_bytes(b"frozen-entry")
    (stage / "release-manifest.json").write_text("{}", encoding="utf-8")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", stage / "THIRD_PARTY_NOTICES.md")
    (stage / "Agent").mkdir()
    shutil.copytree(ROOT / "app" / "templates", internal / "app" / "templates")
    shutil.copytree(ROOT / "app" / "static", internal / "app" / "static")
    shutil.copytree(ROOT / "contracts", internal / "contracts")
    for catalog in SQL_CATALOGS:
        source = ROOT / catalog
        destination = internal / catalog
        shutil.copytree(source, destination)
    return stage


def _codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def _source_sql_digests() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for catalog in SQL_CATALOGS
        for path in (ROOT / catalog).rglob("*.sql")
    }


def _declared_sql_digests(policy: dict) -> dict[str, str]:
    return {
        f"{catalog['path'].removeprefix('_internal/')}/{name}": digest
        for catalog in policy["sql_catalogs"]
        for name, digest in catalog["files"].items()
    }


def test_verifier_module_is_importable_without_shadowing_installed_packaging() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from packaging.version import Version; assert Version('1.0') > Version('0.9')",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_complete_synthetic_staging_tree_satisfies_policy(tmp_path: Path) -> None:
    assert verify_runtime_files(_stage_runtime_tree(tmp_path)) == ()


def test_required_paths_cover_the_single_entry_for_both_boot_modes() -> None:
    policy = load_runtime_policy(POLICY_PATH)

    assert "Brain.exe" in policy["required_files"]
    assert "_internal/app/static/dist" in policy["required_directories"]
    assert "_internal/contracts" in policy["required_directories"]


def test_missing_required_file_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "Brain.exe").unlink()

    assert "required_file_missing" in _codes(verify_runtime_files(stage))


def test_missing_required_directory_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "Agent").rmdir()

    assert "required_directory_missing" in _codes(verify_runtime_files(stage))


def test_forbidden_app_env_example_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    forbidden = stage / "_internal" / "app" / ".env.example"
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_text("ENVIRONMENT=development\n", encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert "forbidden_path_present" in _codes(findings)
    assert any(finding.path == "_internal/app/.env.example" for finding in findings)


def test_sql_catalog_declaration_is_a_complete_derived_closure() -> None:
    """Removing any declared SQL file makes this exact-set assertion fail."""

    assert _declared_sql_digests(load_runtime_policy(POLICY_PATH)) == _source_sql_digests()


def test_byte_preservation_scope_covers_each_immutable_loader_tree() -> None:
    policy = load_runtime_policy(POLICY_PATH)

    assert set(policy["byte_preserved_paths"]) == {
        "_internal/app/persistence/sql/**",
        "_internal/app/persistence/auth_sql/**",
        "_internal/app/persistence/projection_sql/**",
        "_internal/app/analytics/sql/**",
        "_internal/contracts/**",
    }


def test_contract_anchor_hashes_match_the_derived_contract_closure() -> None:
    policy = load_runtime_policy(POLICY_PATH)
    contracts = policy["contracts"]

    assert contracts["manifest_sha256"] == hashlib.sha256(
        (ROOT / "contracts" / "manifest.json").read_bytes()
    ).hexdigest()
    assert contracts["consumer_pin_sha256"] == hashlib.sha256(
        (ROOT / "contracts" / "consumer-pin.json").read_bytes()
    ).hexdigest()


def test_missing_sql_catalog_file_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    removed = stage / "_internal" / "app" / "analytics" / "sql" / "0004_topic_property_pair.sql"
    removed.unlink()

    findings = verify_runtime_files(stage)

    assert "sql_catalog_file_missing" in _codes(findings)
    assert any(finding.path.endswith("0004_topic_property_pair.sql") for finding in findings)


def test_sql_byte_rewrite_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    sql_file = stage / "_internal" / "app" / "persistence" / "sql" / "0001_canonical_plane.sql"
    sql_file.write_bytes(sql_file.read_bytes().replace(b"\n", b"\r\n"))

    assert "byte_preservation_failed" in _codes(verify_runtime_files(stage))


def test_unexpected_sql_catalog_file_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    unexpected = stage / "_internal" / "app" / "analytics" / "sql" / "0005_unreviewed.sql"
    unexpected.write_text("select 1;\n", encoding="utf-8")

    assert "sql_catalog_file_unexpected" in _codes(verify_runtime_files(stage))


def test_contract_closure_is_derived_from_the_staged_manifest(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    manifest = json.loads((ROOT / "contracts" / "manifest.json").read_text(encoding="utf-8"))
    missing = stage / "_internal" / "contracts" / manifest["files"][0]["path"]
    missing.unlink()

    assert "contracts_closure_failed" in _codes(verify_runtime_files(stage))


def test_frontend_without_a_manifest_fails_closed(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "_internal" / "app" / "static" / "dist" / "manifest.json").unlink()

    assert "frontend_manifest_missing" in _codes(verify_runtime_files(stage))


def test_malformed_frontend_manifest_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "_internal" / "app" / "static" / "dist" / "manifest.json").write_text(
        "not-json", encoding="utf-8"
    )

    assert "frontend_manifest_invalid" in _codes(verify_runtime_files(stage))


def test_missing_manifest_referenced_frontend_asset_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    manifest = json.loads(
        (stage / "_internal" / "app" / "static" / "dist" / "manifest.json").read_text(encoding="utf-8")
    )
    asset = manifest["index.html"]["file"]
    (stage / "_internal" / "app" / "static" / "dist" / asset).unlink()

    assert "frontend_asset_missing" in _codes(verify_runtime_files(stage))


def test_missing_staging_root_returns_a_finding(tmp_path: Path) -> None:
    assert _codes(verify_runtime_files(tmp_path / "absent")) == {"staging_root_missing"}
