"""Regression tests for the repository documentation checker."""

from __future__ import annotations

from pathlib import Path

from tools import check_docs


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
