"""Regression tests for the repository documentation checker."""

from __future__ import annotations

from pathlib import Path

from tools import check_docs

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_markdown_discovery_skips_only_root_pytest_basetemp_directories(
    tmp_path: Path, monkeypatch
) -> None:
    nested_document = tmp_path / "docs" / ".pytest_temp_docs" / "sample.md"
    nested_document.parent.mkdir(parents=True)
    nested_document.write_text("# Nested documentation\n", encoding="utf-8")

    root_basetemp_document = tmp_path / ".pytest_temp_docs" / "sample.md"
    root_basetemp_document.parent.mkdir()
    root_basetemp_document.write_text("# Generated test artifact\n", encoding="utf-8")

    monkeypatch.setattr(check_docs, "ROOT", tmp_path)

    discovered = check_docs.markdown_files()

    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/.pytest_temp*/" in gitignore
    assert "\n.pytest_temp*/" not in gitignore
    assert nested_document in discovered
    assert root_basetemp_document not in discovered


@pytest.mark.parametrize("artifact_directory", ["ci-tests", "browser-tests"])
def test_markdown_discovery_excludes_generated_test_reports_only(tmp_path, monkeypatch, artifact_directory):
    generated = tmp_path / "artifacts" / artifact_directory / "failed-lane/summary.md"
    document = tmp_path / "docs/artifacts" / artifact_directory / "summary.md"
    for path in (generated, document):
        path.parent.mkdir(parents=True)
        path.write_text("# Summary\n", encoding="utf-8")
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    assert check_docs.markdown_files() == [document]


@pytest.mark.parametrize("document_path", [
    "docs/tools/e2e-capture/test-results/sample.md",
    "tools/e2e-capture/docs/test-results/sample.md",
    "tools/e2e-capture/test-results-docs/sample.md",
    "tools/other/test-results/sample.md",
])
def test_markdown_discovery_excludes_only_root_browser_results(
    tmp_path, monkeypatch, capsys, document_path,
):
    generated = tmp_path / "tools/e2e-capture/test-results/failed-case/error-context.md"
    document = tmp_path / document_path
    for path in (generated, document):
        path.parent.mkdir(parents=True)
        path.write_text("# Report\n\n[Missing](missing.md)\n", encoding="utf-8")
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    assert "tools/e2e-capture/test-results/" in (
        REPOSITORY_ROOT / ".gitignore"
    ).read_text(encoding="utf-8")
    assert check_docs.markdown_files() == [document]
    assert check_docs.main() == 1
    output = capsys.readouterr().out
    assert f"{document_path}:3: broken relative link: missing.md" in output
    assert "error-context.md" not in output
