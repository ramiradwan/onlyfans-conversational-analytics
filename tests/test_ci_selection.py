"""Counterexamples for loss of CI ownership and local/hosted selector drift."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import ci_selection as selection
from tools import test_backend as runner

pytestmark = pytest.mark.ci_tier("fast")


def manifest():
    return {"schema_version": 1, "assignment_basis": {"status": "provisional_unmeasured"},
            "integration_shards": {"tests/test_storage.py": 1, "tests/test_other.py": 2},
            "windows_contracts": {"platform": [], "analytics": [
                {"selector": "tests/test_storage.py::test_restart", "reason": "File reopening on Windows"}]},
            "stateful_profiles": {"tier_b_general": {"hypothesis_profile": "tier_b_general", "selectors": [
                "tests/test_generated.py::TestPersistentGeneral"]},
                "analytics_oracle_falsifiers": {"hypothesis_profile": None, "selectors": [
                    "tests/test_generated.py::test_oracle"]}}}


class Item:
    def __init__(self, nodeid, *scopes):
        self.nodeid = nodeid
        self.scopes = [SimpleNamespace(own_markers=list(scope)) for scope in scopes]

    def iter_markers(self):
        return (mark for owner in reversed(self.scopes) for mark in owner.own_markers)

    def get_closest_marker(self, name):
        return next((mark for mark in self.iter_markers() if mark.name == name), None)

    def listchain(self):
        return self.scopes


def mark(name, *args):
    return getattr(pytest.mark, name)(*args).mark if args else getattr(pytest.mark, name).mark


def test_required_large_case_cannot_be_demoted_to_scale():
    item = Item("tests/test_storage.py::test_large_bounded_read", [mark("ci_tier", "scale")])
    with pytest.raises(selection.SelectionError, match="moving an existing default test"):
        selection.describe_item(item, manifest())


def test_nearest_tier_overrides_module_but_never_overrides_frozen_legacy_scope():
    item = Item("tests/test_storage.py::test_qualification", [mark("ci_tier", "integration")],
                [mark("ci_tier", "scale"), mark("slow")])
    result = selection.describe_item(item, manifest())
    assert result["tier"] == "scale"
    assert not result["legacy_linux"] and not result["legacy_windows"]
    assert selection.select_item(result, "scale")
    assert not selection.select_item(result, "integration", shard=1)


@pytest.mark.parametrize("marks,error", [([], "declare exactly one"),
    ([mark("ci_tier", "fast"), mark("ci_tier", "integration")], "conflicting")])
def test_missing_or_conflicting_declarations_fail_collection(marks, error):
    with pytest.raises(selection.SelectionError, match=error):
        selection.describe_item(Item("tests/test_storage.py::test_new", marks), manifest())


def test_new_integration_file_cannot_silently_land_in_fast_lane():
    with pytest.raises(selection.SelectionError, match="unassigned integration"):
        selection.describe_item(Item("tests/test_new.py::test_work", [mark("ci_tier", "integration")]), manifest())


def test_windows_native_case_needs_positive_contract_and_keeps_platform_obligation():
    item = Item("tests/test_storage.py::test_restart", [mark("ci_tier", "integration"), mark("windows_compat")])
    metadata = selection.describe_item(item, manifest())
    assert selection.select_item(metadata, "windows-analytics")
    assert selection.select_item(metadata, "integration", shard=1)
    assert not selection.select_item(metadata, "integration", shard=2)
    changed = manifest()
    changed["windows_contracts"]["analytics"].clear()
    with pytest.raises(selection.SelectionError, match="missing a Windows contract"):
        selection.describe_item(item, changed)


def test_windows_production_does_not_silently_enter_linux_lane():
    item = Item("tests/test_storage.py::test_restart", [mark("ci_tier", "integration"), mark("windows_production")])
    metadata = selection.describe_item(item, manifest())
    assert metadata["legacy_windows"] and not metadata["legacy_linux"]
    assert selection.select_item(metadata, "windows-analytics")
    assert not selection.select_item(metadata, "integration")


def test_selectors_match_parameterized_cases_without_similar_name_collisions():
    assert selection.matches("tests/test_storage.py::test_restart[sqlite]", "tests/test_storage.py::test_restart")
    assert not selection.matches("tests/test_storage.py::test_restart_without_write", "tests/test_storage.py::test_restart")


def test_parameter_byte_escapes_remain_exact_while_path_separators_normalize():
    metadata = selection.describe_item(Item(r"tests\test_storage.py::test_bytes[\xff]", [mark("ci_tier", "integration")]), manifest())
    assert metadata["nodeid"] == r"tests/test_storage.py::test_bytes[\xff]"


def test_checked_in_windows_contract_keeps_platform_dependent_parameter_identities():
    real = selection.load_manifest(runner.ROOT)
    cases = {
        "tests/test_grant_verifier.py::test_grant_profile_vectors_match_expected_outcomes":
            ("creator_account_binding/valid-current", r"creator_account_binding\valid-current"),
        "tests/test_engineering_attestation.py::test_zip_reader_rejects_unsafe_duplicate_encrypted_and_symlink_entries":
            (r"PK\x01\x02\x14\x03", r"PK\x01\x02\x14\x00"),
    }
    for selector, platform_ids in cases.items():
        for parameter_id in platform_ids:
            nodeid = f"{selector}[{parameter_id}]"
            item = Item(nodeid, [mark("ci_tier", "fast")], [mark("windows_compat")])
            metadata = selection.describe_item(item, real)
            assert metadata["nodeid"] == nodeid
            assert selection.select_item(metadata, "windows-platform")
            assert selection.select_item(metadata, "fast")
        weakened = json.loads(json.dumps(real))
        weakened["windows_contracts"]["platform"] = [
            entry for entry in weakened["windows_contracts"]["platform"]
            if entry["selector"] != selector
        ]
        with pytest.raises(selection.SelectionError, match="missing a Windows contract"):
            selection.describe_item(item, weakened)


def test_stateful_profile_remains_an_explicit_obligation():
    item = Item("tests/test_generated.py::TestPersistentGeneral::runTest", [
        mark("ci_tier", "stateful"), mark("stateful_tier_b")])
    metadata = selection.describe_item(item, manifest())
    assert metadata["stateful_profiles"] == ["tier_b_general"]
    assert not metadata["legacy_default"]
    assert selection.select_item(metadata, "stateful", profile="tier_b_general")
    with pytest.raises(selection.SelectionError, match="requires --profile"):
        selection.select_item(metadata, "stateful")


def test_manifest_update_preserves_existing_assignments_and_only_assigns_new_files():
    before = manifest()
    files = {"tests/test_storage.py": 2, "tests/test_other.py": 20, "tests/test_new.py": 6}
    after = runner.updated_manifest(before, files)
    assert after["integration_shards"]["tests/test_storage.py"] == 1
    assert after["integration_shards"]["tests/test_other.py"] == 2
    assert after["integration_shards"]["tests/test_new.py"] == 3
    assert runner.updated_manifest(after, files) == after
    assert before == manifest()


def test_rebalance_requires_complete_measured_timings_and_is_deterministic():
    files = {"tests/test_storage.py": 2, "tests/test_other.py": 20}
    with pytest.raises(selection.SelectionError, match="every integration file"):
        runner.updated_manifest(manifest(), files, timings={"tests/test_storage.py": 8}, rebalance=True)
    timings = {"tests/test_storage.py": 10.0, "tests/test_other.py": 50.0}
    result = runner.updated_manifest(manifest(), files, timings=timings, rebalance=True)
    assert result["integration_shards"] == {"tests/test_other.py": 1, "tests/test_storage.py": 2}
    assert result["assignment_basis"]["status"] == "measured"


def test_runner_uses_exact_profile_and_only_collects_its_declared_targets(monkeypatch):
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "unrelated_profile")
    args = runner.parser().parse_args(["stateful", "--profile", "tier_b_general"])
    command, environment = runner.build_command(args, ["-x"], manifest())
    assert environment["HYPOTHESIS_PROFILE"] == "tier_b_general"
    assert "tests/test_generated.py::TestPersistentGeneral" in command
    assert command[-1] == "-x"
    assert "--override-ini=addopts=" in command
    assert command[command.index("--ci-profile") + 1] == "tier_b_general"
    args.profile = "analytics_oracle_falsifiers"
    _, environment = runner.build_command(args, [], manifest())
    assert "HYPOTHESIS_PROFILE" not in environment


def test_runner_list_is_collection_only_and_workflow_alias_uses_same_selector():
    args = runner.parser().parse_args(["list", "integration", "--shard", "3"])
    listed, _ = runner.build_command(args, [], manifest())
    args = runner.parser().parse_args(["--lane", "analytics-integration", "--shard", "3"])
    executed, _ = runner.build_command(args, [], manifest())
    assert listed[:-2] == executed
    assert listed[-2:] == ["--collect-only", "-q"]
    args = runner.parser().parse_args(["integration", "--shard", "3", "--list"])
    aliased, _ = runner.build_command(args, [], manifest())
    assert aliased == listed


def test_runner_targeted_profile_reproduction_does_not_union_all_profile_targets():
    args = runner.parser().parse_args(["stateful", "--profile", "tier_b_general"])
    target = "tests/test_generated.py::TestPersistentGeneral::runTest"
    command, _ = runner.build_command(args, [target, "-vv"], manifest())
    assert target in command
    assert "tests/test_generated.py::TestPersistentGeneral" not in command


def test_runner_clears_inherited_profile_for_normal_lanes(monkeypatch):
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "tier_b_general")
    args = runner.parser().parse_args(["fast"])
    _, environment = runner.build_command(args, [], manifest())
    assert "HYPOTHESIS_PROFILE" not in environment


def test_prerequisite_errors_give_exact_bootstrap_commands_without_building(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    errors = runner.prerequisite_errors("fast", None, [], root=tmp_path)
    assert any("npm run build --prefix frontend" in error for error in errors)
    assert any("npm run build --prefix extension" in error for error in errors)
    assert any("Node.js 22" in error for error in errors)
    assert runner.prerequisite_errors("fast", None, ["tests/test_pure.py"], root=tmp_path) == []
    assert any("npm ci --prefix extension" in error for error in runner.prerequisite_errors(
        "stateful", "agent_tier_a_general", [], root=tmp_path))
    assert any("OpenSSL" in error for error in errors)
    assert not any("OpenSSL" in error for error in runner.prerequisite_errors(
        "fast", None, [], root=tmp_path, execute=False))


def test_report_timings_include_setup_teardown_and_ignore_other_platforms(tmp_path):
    for number, (platform, durations, selection_profile, targets, selected) in enumerate([
        ("Linux", [1, 2, 3], "", [], ["tests/test_storage.py::test_restart"]),
        ("Linux", [2, 4, 6], "", [], ["tests/test_storage.py::test_restart"]),
        ("Windows", [100, 200, 300], "", [], ["tests/test_storage.py::test_restart"]),
        ("Linux", [100, 200, 300], "analytics_oracle_falsifiers", [], ["tests/test_storage.py::test_restart"]),
        ("Linux", [100, 200, 300], "", ["tests/test_storage.py::test_restart"], ["tests/test_storage.py::test_restart"]),
        ("Linux", [100, 200, 300], "", [], []),
    ]):
        target = tmp_path / str(number) / "report.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"schema": "ci-test-report/v1", "platform": platform,
            "lane": "analytics-integration-1", "collect_only": False, "exit_code": 0,
            "complete": True, "profile": "", "collection_errors": [], "partial": False,
            "selection_profile": selection_profile, "selection": {"targets": targets},
            "collected": [{"nodeid": "tests/test_storage.py::test_restart", "markers": []}], "selected": selected,
            "reports": [{"nodeid": "tests/test_storage.py::test_restart", "when": phase, "duration": duration}
                        for phase, duration in zip(("setup", "call", "teardown"), durations)]}), encoding="utf-8")
    assert runner.read_timings(tmp_path) == {"tests/test_storage.py": 9}


def test_streamed_log_keeps_failure_output_and_exit_code(tmp_path, capsys):
    import os
    import sys
    environment = os.environ.copy()
    environment.pop("PYTHONIOENCODING", None)
    environment["PYTHONUTF8"] = "0"
    # A subprocess inside pytest must retain normal locale decoding. Forcing
    # only PYTHONIOENCODING changes its writer to UTF-8 and breaks this on
    # Windows cp1252 (the real Import Linter regression returned stdout=None).
    source = ("import subprocess,sys; "
              "result=subprocess.run([sys.executable,'-c','print(chr(233))'],capture_output=True,text=True,check=True); "
              "assert result.stdout.strip()==chr(233),repr(result.stdout); "
              "print('failure context '+chr(233)); sys.exit(3)")
    status = runner.run_streaming([sys.executable, "-c", source], environment, tmp_path)
    assert status == 3
    assert "failure context \u00e9" in capsys.readouterr().out
    assert "failure context \u00e9" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_full_validation_detects_stale_selectors_but_local_narrowing_is_supported():
    value = manifest()
    value["integration_shards"] = {"tests/test_storage.py": 1}
    metadata = selection.describe_item(Item("tests/test_storage.py::test_restart", [mark("ci_tier", "integration")]), value)
    selection.validate_inventory([metadata], value)
    with pytest.raises(selection.SelectionError, match="Stale stateful"):
        selection.validate_inventory([metadata], value, complete=True)


def test_checked_in_manifest_assignments_match_explicit_module_declarations():
    real = selection.load_manifest(runner.ROOT)
    assert set(runner.integration_files(runner.ROOT)) == set(real["integration_shards"])
