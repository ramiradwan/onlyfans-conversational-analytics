"""Tests for the declarative clean-machine package policy."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.staticfiles import StaticFiles

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


def test_declared_wildcard_rejects_runtime_environment_file(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    forbidden = stage / "_internal" / "app" / "runtime.env"
    forbidden.write_text("ENVIRONMENT=development\n", encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "forbidden_path_present"
        and finding.path == "_internal/app/runtime.env"
        and finding.detail == "matches _internal/**/runtime.env"
        for finding in findings
    ), "declared runtime.env wildcard must reject the staged file"


def test_missing_required_file_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "Brain.exe").unlink()

    assert "required_file_missing" in _codes(verify_runtime_files(stage))


def test_required_paths_checker_reports_a_missing_required_directory(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "Agent").rmdir()

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "required_directory_missing" and finding.path == "Agent"
        for finding in findings
    ), "required-path checker must report a declared missing directory"


def test_required_directories_cover_runtime_static_mount() -> None:
    from app.main import app

    runtime_directories = {
        f"_internal/{Path(static_app.directory).resolve().relative_to(ROOT).as_posix()}"
        for route in app.routes
        if isinstance(static_app := getattr(route, "app", None), StaticFiles)
    }
    declared_directories = set(load_runtime_policy(POLICY_PATH)["required_directories"])

    assert runtime_directories, "runtime must mount at least one static directory"
    assert runtime_directories <= declared_directories, (
        "required_directories must include every directory mounted by the runtime"
    )


def test_forbidden_development_configuration_examples_are_reported_from_multiple_locations(
    tmp_path: Path,
) -> None:
    stage = _stage_runtime_tree(tmp_path)
    first_forbidden = stage / "_internal" / "app" / ".env.example"
    second_forbidden = stage / "_internal" / "app" / "config" / ".env.example"
    first_forbidden.write_text("ENVIRONMENT=development\n", encoding="utf-8")
    second_forbidden.parent.mkdir(parents=True, exist_ok=True)
    second_forbidden.write_text("ENVIRONMENT=development\n", encoding="utf-8")

    findings = verify_runtime_files(stage)

    reported = {
        finding.path
        for finding in findings
        if finding.code == "forbidden_path_present"
    }
    assert {
        "_internal/app/.env.example",
        "_internal/app/config/.env.example",
    } <= reported, "development configuration examples must be forbidden at every depth"


@pytest.mark.parametrize(
    ("material", "payload"),
    [
        ("installation_claim", "installation_claim=claim-package-with-secret"),
        ("claim_secret", "claim_secret=secret-value"),
        ("bearer_or_session_token", "Authorization: Bearer opaque-token-value-123"),
        ("bearer_or_session_token", "session_token=session-token-value-123"),
        ("generated_runtime_secret", "runtime_secret=generated-value"),
        ("user_profile_path", r"C:\Users\installer-test\profile"),
    ],
)
def test_per_user_material_in_a_staged_payload_produces_a_named_finding(
    tmp_path: Path, material: str, payload: str
) -> None:
    """The material itself, rather than a fixture boundary, causes the finding."""

    stage = _stage_runtime_tree(tmp_path)
    seeded = stage / "_internal" / "app" / "staged-data.txt"
    seeded.write_text(payload, encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "forbidden_material_present"
        and finding.path == "_internal/app/staged-data.txt"
        and finding.detail == f"matches forbidden material declaration: {material}"
        for finding in findings
    ), f"the {material} payload must produce its named per-user-material finding"


def test_text_payload_with_windows_profile_path_is_rejected_after_binary_narrowing(
    tmp_path: Path,
) -> None:
    """A user profile path in a UTF-8 payload remains forbidden material."""

    stage = _stage_runtime_tree(tmp_path)
    seeded = stage / "_internal" / "app" / "claim.json"
    seeded.write_text(r"C:\Users\somebody\claim.json", encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "forbidden_material_present"
        and finding.path == "_internal/app/claim.json"
        and finding.detail == "matches forbidden material declaration: user_profile_path"
        for finding in findings
    ), "the text profile-path falsifier must reach the forbidden-material assertion"


def test_posix_profile_path_is_rejected_but_rest_users_route_is_clean(tmp_path: Path) -> None:
    """Absolute POSIX profiles remain forbidden without treating API routes as profiles."""

    stage = _stage_runtime_tree(tmp_path)
    profile = stage / "_internal" / "app" / "profile-path.txt"
    route = stage / "_internal" / "app" / "api-route.txt"
    profile.write_text("/home/someone/.config/secret", encoding="utf-8")
    route.write_text("/api2/v2/users/me", encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "forbidden_material_present"
        and finding.path == "_internal/app/profile-path.txt"
        and finding.detail == "matches forbidden material declaration: user_profile_path"
        for finding in findings
    ), "the POSIX profile-path falsifier must reach the forbidden-material assertion"
    assert not any(
        finding.code == "forbidden_material_present"
        and finding.path == "_internal/app/api-route.txt"
        and finding.detail == "matches forbidden material declaration: user_profile_path"
        for finding in findings
    ), "an interior REST users segment is not an absolute POSIX profile path"


def test_user_profile_path_delimiter_and_url_tables_are_constrained(tmp_path: Path) -> None:
    must_be_reported = (
        ("windows-backslash", r"C:\Users\rami\claim.json"),
        ("windows-slash", "C:/Users/rami/claim.json"),
        ("single-quoted-home", "'/home/rami/.config'"),
        ("double-quoted-home", '"/home/rami/.config"'),
        ("environment-assignment", "HOME=/home/rami"),
        ("call-argument", "open(/home/rami/x)"),
        ("scheme-like-prefix", "datadir:/home/rami/data"),
        ("json-value", '{"p":"/home/rami"}'),
        ("comma-delimited", "a,/home/rami,b"),
        ("bare-home", "/home/rami/.config"),
    )
    must_be_clean = (
        ("fetch-users-route", 'fetch("/api2/v2/users/me")'),
        ("https-users-route", "https://example.com/users/me"),
        ("http-users-route", "http://h2/users/me"),
        ("relative-users-route", "api/v2/users/me"),
    )

    stage = _stage_runtime_tree(tmp_path)
    for case, payload in (*must_be_reported, *must_be_clean):
        (stage / "_internal" / "app" / f"{case}.txt").write_text(
            payload, encoding="utf-8"
        )

    findings = verify_runtime_files(stage)

    for case, _ in must_be_reported:
        assert any(
            finding.code == "forbidden_material_present"
            and finding.path == f"_internal/app/{case}.txt"
            and finding.detail == "matches forbidden material declaration: user_profile_path"
            for finding in findings
        ), f"must-be-reported case {case!r} must reach the user_profile_path assertion"
    for case, _ in must_be_clean:
        assert not any(
            finding.code == "forbidden_material_present"
            and finding.path == f"_internal/app/{case}.txt"
            and finding.detail == "matches forbidden material declaration: user_profile_path"
            for finding in findings
        ), f"must-be-clean case {case!r} must remain outside user_profile_path"


def test_binary_payload_is_not_scanned_but_its_forbidden_path_is_rejected(tmp_path: Path) -> None:
    """An undecodable binary skips payload inspection but still receives path inspection."""

    stage = _stage_runtime_tree(tmp_path)
    binary = stage / "_internal" / "app" / "blocked-native.pyd"
    binary.write_bytes(b"\xffC:\\Users\\upstream-builder\\wheel-source")
    policy = load_runtime_policy(POLICY_PATH)
    policy["forbidden_material"] = [
        {"name": "forbidden_binary_path", "pattern": r"blocked-native\.pyd$"}
    ]

    findings = verify_runtime_files(stage, policy)

    assert any(
        finding.code == "forbidden_material_present"
        and finding.path == "_internal/app/blocked-native.pyd"
        and finding.detail == "matches forbidden material declaration: forbidden_binary_path"
        for finding in findings
    ), "an undecodable binary must still be rejected when its staged path matches"


def test_per_user_material_declarations_cover_every_required_category() -> None:
    declarations = load_runtime_policy(POLICY_PATH)["forbidden_material"]

    assert {declaration["name"] for declaration in declarations} == {
        "installation_claim",
        "claim_secret",
        "bearer_or_session_token",
        "generated_runtime_secret",
        "user_profile_path",
    }, "the per-user-material policy must name every prohibited category"


def test_sql_catalog_declaration_is_a_complete_derived_closure() -> None:
    """Removing any declared SQL file makes this exact-set assertion fail."""

    assert _declared_sql_digests(load_runtime_policy(POLICY_PATH)) == _source_sql_digests()


def test_contract_anchor_hashes_match_the_derived_contract_closure() -> None:
    policy = load_runtime_policy(POLICY_PATH)
    contracts = policy["contracts"]

    assert contracts["manifest_sha256"] == hashlib.sha256(
        (ROOT / "contracts" / "manifest.json").read_bytes()
    ).hexdigest()
    assert contracts["consumer_pin_sha256"] == hashlib.sha256(
        (ROOT / "contracts" / "consumer-pin.json").read_bytes()
    ).hexdigest()


def test_contract_support_file_hashes_match_the_derived_contract_closure() -> None:
    policy = load_runtime_policy(POLICY_PATH)
    declared_digests = policy["contracts"]["root_file_sha256"]
    assert set(declared_digests) == {"loader.py", "verify.py"}, (
        "both executable contract support files must have digest anchors"
    )
    derived_digests = {
        filename: hashlib.sha256((ROOT / "contracts" / filename).read_bytes()).hexdigest()
        for filename in declared_digests
    }

    assert declared_digests == derived_digests, (
        "contract support-file digest anchors must match the immutable source files"
    )


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


@pytest.mark.parametrize("position", ("first", "middle", "last"))
def test_contract_closure_is_derived_from_the_staged_manifest(
    tmp_path: Path, position: str
) -> None:
    stage = _stage_runtime_tree(tmp_path)
    manifest = json.loads((ROOT / "contracts" / "manifest.json").read_text(encoding="utf-8"))
    indices = {
        "first": 0,
        "middle": len(manifest["files"]) // 2,
        "last": len(manifest["files"]) - 1,
    }
    missing = stage / "_internal" / "contracts" / manifest["files"][indices[position]]["path"]
    missing.unlink()

    assert "contracts_closure_failed" in _codes(verify_runtime_files(stage)), (
        f"contract closure must reject a missing {position} manifest entry"
    )


def test_contract_loader_byte_rewrite_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    loader = stage / "_internal" / "contracts" / "loader.py"
    loader.write_bytes(loader.read_bytes() + b"\n# staged rewrite\n")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "byte_preservation_failed"
        and finding.path == "_internal/contracts/loader.py"
        for finding in findings
    ), "contract loader byte rewrite must be reported by its digest anchor"


def test_missing_declared_contract_root_file_is_reported(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "_internal" / "contracts" / "loader.py").unlink()

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "contracts_root_file_missing"
        and finding.path == "_internal/contracts/loader.py"
        for finding in findings
    ), "declared contract root files must be reported when absent"


def test_contract_verifier_rejects_an_unlisted_contract_root_file(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    unlisted = stage / "_internal" / "contracts" / "unlisted-contract.json"
    unlisted.write_text("{}\n", encoding="utf-8")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "contracts_root_file_unexpected"
        and finding.path == "_internal/contracts/unlisted-contract.json"
        for finding in findings
    ), "contract verification must reject an unlisted contracts-root file"


def test_contract_root_file_declaration_exactly_matches_the_staged_root(tmp_path: Path) -> None:
    findings = verify_runtime_files(_stage_runtime_tree(tmp_path))

    assert not any(
        finding.code in {"contracts_root_file_missing", "contracts_root_file_unexpected"}
        for finding in findings
    ), "contracts-root declaration must exactly match the staged root files"


def test_frontend_without_a_manifest_fails_closed(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    (stage / "_internal" / "app" / "static" / "dist" / "manifest.json").unlink()

    assert "frontend_manifest_missing" in _codes(verify_runtime_files(stage))


def test_frontend_without_a_dist_directory_fails_closed(tmp_path: Path) -> None:
    stage = _stage_runtime_tree(tmp_path)
    shutil.rmtree(stage / "_internal" / "app" / "static" / "dist")

    findings = verify_runtime_files(stage)

    assert any(
        finding.code == "frontend_manifest_missing"
        and finding.path == "_internal/app/static/dist"
        for finding in findings
    ), "an absent frontend dist directory must fail closed"


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


def test_complete_synthetic_staging_tree_satisfies_policy(tmp_path: Path) -> None:
    findings = verify_runtime_files(_stage_runtime_tree(tmp_path))

    assert findings == (), "a complete synthetic staging tree must satisfy the policy"
