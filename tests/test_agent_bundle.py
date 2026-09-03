"""Behavioural tests for the exact deterministic Chrome ZIP release boundary."""

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_SCRIPT = ROOT / "packaging" / "new-agent-bundle.ps1"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _run_bundle(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUNDLE_SCRIPT),
            "-SourceDirectory",
            str(source),
            "-BundlePath",
            str(destination),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_tree(root: Path, *, reverse: bool, mtime: int) -> None:
    entries = [
        ("manifest.json", b'{"manifest_version":3,"version":"2.0.0"}\n'),
        ("build-meta.json", b'{"extension_version":"2.0.0"}\n'),
        ("icons/icon48.png", b"\x89PNG\r\nagent-icon"),
    ]
    if reverse:
        entries.reverse()
    for relative, content in entries:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))


@pytest.mark.skipif(os.name != "nt", reason="drives the PowerShell ZIP writer")
def test_agent_bundle_is_byte_reproducible_and_normalizes_metadata(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    _write_tree(first_source, reverse=False, mtime=1_700_000_000)
    _write_tree(second_source, reverse=True, mtime=1_800_000_000)

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_result = _run_bundle(first_source, first)
    second_result = _run_bundle(second_source, second)

    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()

    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == [
            "build-meta.json",
            "icons/icon48.png",
            "manifest.json",
        ]
        assert all(info.date_time == FIXED_ZIP_TIMESTAMP for info in infos)
        assert all(info.external_attr == 0 for info in infos)
        assert all(not info.is_dir() for info in infos)
        assert all("\\" not in info.filename for info in infos)
        assert archive.read("icons/icon48.png") == b"\x89PNG\r\nagent-icon"


@pytest.mark.skipif(os.name != "nt", reason="drives the PowerShell ZIP writer")
def test_agent_bundle_fails_closed_without_overwriting_existing_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    destination = tmp_path / "agent.zip"
    destination.write_bytes(b"do-not-overwrite")

    result = _run_bundle(source, destination)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert destination.read_bytes() == b"do-not-overwrite"


@pytest.mark.skipif(os.name != "nt", reason="drives the PowerShell ZIP writer")
def test_agent_bundle_rejects_empty_and_self_containing_sources(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    empty_bundle = tmp_path / "empty.zip"
    empty_result = _run_bundle(empty, empty_bundle)
    assert empty_result.returncode != 0
    assert "contains no files" in empty_result.stderr
    assert not empty_bundle.exists()

    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    nested_bundle = source / "agent.zip"
    nested_result = _run_bundle(source, nested_bundle)
    assert nested_result.returncode != 0
    assert "outside its source directory" in nested_result.stderr
    assert not nested_bundle.exists()


@pytest.mark.skipif(os.name != "nt", reason="drives the PowerShell ZIP writer")
def test_agent_bundle_refuses_the_store_candidate_filename(tmp_path: Path) -> None:
    """The same source tree bundles under a development name and never the Store one."""

    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    store_candidate = tmp_path / "OnlyFans-Conversational-Analytics-Agent-2.0.0-chrome.zip"

    refused = _run_bundle(source, store_candidate)

    assert refused.returncode != 0
    assert "must not be named as the Store candidate" in refused.stderr
    assert not store_candidate.exists()

    development = tmp_path / "agent-development-unpacked-2.0.0.zip"
    accepted = _run_bundle(source, development)

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert development.exists()
