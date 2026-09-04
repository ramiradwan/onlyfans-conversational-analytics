"""Behavioural falsifiers for the Windows packaging policy build gate."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import inno_setup_compiler
import visible_windows
from app.core.config import Settings
from app.core.runtime_paths import runtime_data_directory


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"
EXTENSION_ROOT = ROOT / "extension"
EXTENSION_DIST = EXTENSION_ROOT / "dist"
DIGEST_FILE_NAME = "sha256sums.txt"
STORE_CANDIDATE_SUFFIX = "-chrome.zip"
SIGNING_RULE_FIXTURE = EXTENSION_ROOT / "tests" / "fixtures" / "packaged-signing-rule.json"
LEGAL_BINDINGS_FIXTURE = (
    EXTENSION_ROOT / "tests" / "fixtures" / "legal-instrument-bindings.synthetic.json"
)
SYNTHETIC_PRIVACY_POLICY_URL = "https://legal-evidence.example.com/legal/privacy"

# The publication step a falsifier removes: the byte-preserving copy of the
# packaged archive to the Store candidate name, its audit, its digest entry and
# its announcement. The block spans every line that reads $storeCandidate, so
# the mutated script stays valid under Set-StrictMode.
_STORE_PUBLICATION = """\
    $storeCandidate = Join-Path $installerOutput $storeCandidateName
    Copy-Item -LiteralPath $storePackage.Archive -Destination $storeCandidate
    $storeCandidateDigest = Get-Sha256Digest -Path $storeCandidate
    if ($storeCandidateDigest -ne $storePackage.Sha256) {
        Remove-Item -LiteralPath $storeCandidate -Force
        throw "The Store candidate is not the packaged archive: $($storePackage.Sha256) then $storeCandidateDigest"
    }
    Invoke-ExtensionBuild `
        -Arguments ((Get-ExtensionReleaseArguments -Verb "--audit-package") + "--artifact=$storeCandidate") `
        -FailureMessage "The published Store candidate failed its package audit"
    Write-Sha256Sums -Directory $installerOutput -RelativePaths @($installerName, $storeCandidateName)
    Write-Host "Windows installer ready: $installerPath"
    Write-Host "Store candidate ready: $storeCandidate (sha256:$storeCandidateDigest)"
"""
_STORE_PUBLICATION_REMOVED = """\
    Write-Sha256Sums -Directory $installerOutput -RelativePaths @($installerName)
    Write-Host "Windows installer ready: $installerPath"
"""
_STORE_COPY = """\
    Copy-Item -LiteralPath $storePackage.Archive -Destination $storeCandidate
"""
_STORE_COPY_THAT_CHANGES_BYTES = """\
    Copy-Item -LiteralPath $storePackage.Archive -Destination $storeCandidate
    Add-Content -LiteralPath $storeCandidate -Value "appended" -Encoding ascii
"""
_STORE_DIGEST_CHECK = """\
    $storeCandidateDigest = Get-Sha256Digest -Path $storeCandidate
    if ($storeCandidateDigest -ne $storePackage.Sha256) {
        Remove-Item -LiteralPath $storeCandidate -Force
        throw "The Store candidate is not the packaged archive: $($storePackage.Sha256) then $storeCandidateDigest"
    }
"""
_STORE_DIGEST_CHECK_REMOVED = """\
    $storeCandidateDigest = Get-Sha256Digest -Path $storeCandidate
"""
_STORE_AUDIT = """\
    Invoke-ExtensionBuild `
        -Arguments ((Get-ExtensionReleaseArguments -Verb "--audit-package") + "--artifact=$storeCandidate") `
        -FailureMessage "The published Store candidate failed its package audit"
"""


@pytest.fixture(autouse=True)
def _no_installer_window() -> Iterator[None]:
    """Fail a test whose installer or uninstaller puts a window on the desktop.

    Every install and uninstall in this module drives a real Inno Setup
    artifact, so one recording per test covers each of them.
    """

    if os.name != "nt":
        yield
        return
    with visible_windows.recording_windows(
        lambda window: visible_windows.is_inno_setup_image(window.process_image)
    ) as observed:
        yield
    displayed = sorted(
        (window.class_name, window.title)
        for window in observed
        if window.steals_focus()
    )
    assert displayed == [], (
        f"the installer displayed {displayed}; a default-tier run must not open "
        "an interactive installer window"
    )


def _authoritative_version() -> str:
    version = Settings.model_fields["version"].default
    assert isinstance(version, str) and version
    return version


def _write_pyinstaller_standin(tmp_path: Path) -> Path:
    """Provide a valid policy staging tree without installing PyInstaller in .venv."""

    standin = tmp_path / "pyinstaller_standin.py"
    standin.write_text(
        f"""
import shutil
import sys
from pathlib import Path

root = Path({str(ROOT)!r})
arguments = sys.argv[1:]
dist = Path(arguments[arguments.index('--distpath') + 1])
stage = dist / 'Brain'
(stage / '_internal').mkdir(parents=True)
(stage / 'Brain.exe').write_bytes(b'frozen-brain')
for relative in (
    'app/templates',
    'app/static/dist',
    'app/persistence/sql',
    'app/persistence/auth_sql',
    'app/persistence/projection_sql',
    'app/analytics/sql',
    'contracts',
):
    source = root / relative
    shutil.copytree(source, stage / '_internal' / relative)
provisioning = stage / '_internal' / 'app' / 'provisioning'
provisioning.mkdir()
for name in (
    'provisioning.html',
    'creator-platform-data-risk-disclosure.html',
    'provisioning.js',
):
    shutil.copyfile(root / 'app' / 'provisioning' / name, provisioning / name)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "pyinstaller.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _write_inno_setup_standin(tmp_path: Path) -> Path:
    """Provide a compiler seam that emits the installer named by the build script."""

    standin = tmp_path / "inno_setup_standin.py"
    standin.write_text(
        """
import sys
from pathlib import Path

arguments = sys.argv[1:]
output = Path(
    next(
        argument.split("=", 1)[1]
        for argument in arguments
        if argument.startswith("/DOutputRoot=")
    )
)
version = next(
    argument.split("=", 1)[1]
    for argument in arguments
    if argument.startswith("/DAppVersion=")
)
output.mkdir(parents=True, exist_ok=True)
(output / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe").write_bytes(b"installer")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "iscc.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _run_build(
    script: Path,
    pyinstaller: Path,
    output: Path,
    *,
    test_injection: str = "DevelopmentConfiguration",
    inno_setup_compiler: Path | None = None,
    packaged_signing_rule: Path | None = SIGNING_RULE_FIXTURE,
    legal_release_bindings: Path | None = LEGAL_BINDINGS_FIXTURE,
    privacy_policy_url: str | None = SYNTHETIC_PRIVACY_POLICY_URL,
    development_agent_bundle: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Drive the build script.

    The release inputs default to the checked-in synthetic fixtures because a
    release build refuses to run without them; passing None omits the switch.
    """

    environment = os.environ | {"BRAIN_PROJECT_ROOT": str(ROOT)}
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-BuildPython",
        sys.executable,
        "-PyInstallerExecutable",
        str(pyinstaller),
        "-OutputRoot",
        str(output),
        "-SkipAssetBuild",
    ]
    if inno_setup_compiler is not None:
        command.extend(("-InnoSetupCompiler", str(inno_setup_compiler)))
    if test_injection:
        command.extend(("-TestInjection", test_injection))
    if packaged_signing_rule is not None:
        command.extend(("-PackagedSigningRule", str(packaged_signing_rule)))
    if legal_release_bindings is not None:
        command.extend(("-LegalReleaseBindings", str(legal_release_bindings)))
    if privacy_policy_url is not None:
        command.extend(("-PrivacyPolicyUrl", privacy_policy_url))
    if development_agent_bundle:
        command.append("-DevelopmentAgentBundle")
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_policy_gate_rejects_development_configuration_and_is_load_bearing(
    tmp_path: Path,
) -> None:
    """The same staged runtime.env is rejected only while the policy call exists."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)

    gated = _run_build(BUILD_SCRIPT, pyinstaller, tmp_path / "gated")

    assert gated.returncode != 0, gated.stdout + gated.stderr
    assert '"code": "forbidden_path_present"' in gated.stdout
    assert "_internal/app/runtime.env" in gated.stdout
    assert not (tmp_path / "gated" / "installer").exists(), (
        "generic-artifact falsifier must stop before an installer is produced"
    )

    without_gate = tmp_path / "build-windows-without-policy.ps1"
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    policy_call = "Invoke-PackagingPolicy -BuildPython $BuildPython -ProjectRoot $ProjectRoot -StagingRoot $stagingRoot"
    assert policy_call in source, "the executable falsifier must remove the real policy invocation"
    without_gate.write_text(source.replace(policy_call, "# policy invocation removed for falsifier"), encoding="utf-8")

    ungated = _run_build(without_gate, pyinstaller, tmp_path / "ungated")

    assert ungated.returncode == 0, ungated.stdout + ungated.stderr
    assert (tmp_path / "ungated" / "dist" / "Brain" / "_internal" / "app" / "runtime.env").is_file()


def _run_installer(installer: Path, prefix: Path, environment: dict[str, str]) -> None:
    _run_installer_with_directory(installer, environment, prefix)


def _run_installer_with_directory(
    installer: Path, environment: dict[str, str], prefix: Path | None = None
) -> None:
    # /VERYSILENT also suppresses the installation progress window, which
    # /SILENT still displays and which steals desktop focus.
    command = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    if prefix is not None:
        command.append(f"/DIR={prefix}")
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_uninstaller(prefix: Path, environment: dict[str, str]) -> None:
    uninstaller = prefix / "unins000.exe"
    assert uninstaller.is_file(), f"installer did not create an uninstaller: {uninstaller}"
    result = subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_user_data_retained(data_file: Path) -> None:
    assert data_file.is_file(), "uninstaller must retain the redirected per-user data file"


def _assert_program_payload_removed(prefix: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            remaining = list(prefix.iterdir())
        except FileNotFoundError:
            return
        if not remaining:
            return
        time.sleep(0.05)
    assert not remaining, f"uninstall must leave no program payload behind: {remaining}"


def _inno_setup_compiler() -> Path:
    return inno_setup_compiler.require_inno_setup_compiler(
        "Inno Setup compiler is required for installer behavior tests"
    )


def _compile_installer(
    script: Path, staging_root: Path, output: Path, version: str
) -> Path:
    compiled = subprocess.run(
        [
            str(_inno_setup_compiler()),
            f"/DStagingRoot={staging_root}",
            f"/DOutputRoot={output}",
            f"/DAppVersion={version}",
            str(script),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    installer = output / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe"
    assert installer.is_file(), "Inno Setup must produce the named installer"
    return installer


def _higher_version(version: str) -> str:
    parts = version.split(".")
    assert len(parts) == 3 and all(part.isdecimal() for part in parts)
    return ".".join((*parts[:2], str(int(parts[2]) + 1)))


def _uninstall_registrations(prefixes: tuple[Path, ...]) -> dict[Path, list[str]]:
    import winreg

    registrations = {prefix: [] for prefix in prefixes}
    uninstall_root = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_root) as root:
        index = 0
        while True:
            try:
                key_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            with winreg.OpenKey(root, key_name) as key:
                try:
                    location, _ = winreg.QueryValueEx(key, "InstallLocation")
                except FileNotFoundError:
                    continue
            if not isinstance(location, str):
                continue
            for prefix in prefixes:
                if Path(location).resolve() == prefix.resolve():
                    registrations[prefix].append(key_name)
    return registrations


def _assert_upgrade_installation(
    installed_prefix: Path, unexpected_prefix: Path
) -> None:
    registrations = _uninstall_registrations((installed_prefix, unexpected_prefix))
    assert (installed_prefix / "Brain.exe").is_file(), "the upgrade must retain one install directory"
    assert not unexpected_prefix.exists(), "the upgrade must not create a parallel install directory"
    assert len(registrations[installed_prefix]) == 1, "the upgrade must retain one uninstall registration"
    assert not registrations[unexpected_prefix], "the upgrade must not register a parallel uninstall"


def _assert_parallel_installations(first_prefix: Path, second_prefix: Path) -> None:
    registrations = _uninstall_registrations((first_prefix, second_prefix))
    assert (first_prefix / "Brain.exe").is_file(), "the first installation must remain present"
    assert (second_prefix / "Brain.exe").is_file(), "the second installation must be created"
    assert len(registrations[first_prefix]) == 1, "the first installation must retain its uninstall registration"
    assert len(registrations[second_prefix]) == 1, "the second installation must register separately"


def _uninstall_all(prefixes: tuple[Path, ...], environment: dict[str, str]) -> None:
    for prefix in prefixes:
        if (prefix / "unins000.exe").is_file():
            _run_uninstaller(prefix, environment)
        _assert_program_payload_removed(prefix)
    registrations = _uninstall_registrations(prefixes)
    assert not any(registrations.values()), "installer test cleanup must remove every uninstall registration"


@pytest.mark.skipif(os.name != "nt", reason="Inno Setup installer behavior is Windows-only")
def test_installer_excludes_agent_and_uninstall_retains_redirected_user_data(
    tmp_path: Path,
) -> None:
    """Exercise the generated installer and the red retention mutation end to end."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)
    build_output = tmp_path / "build"
    built = _run_build(
        BUILD_SCRIPT,
        pyinstaller,
        build_output,
        test_injection="",
    )
    assert built.returncode == 0, built.stdout + built.stderr

    version = _authoritative_version()
    installer = (
        build_output
        / "installer"
        / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe"
    )
    assert installer.is_file(), "the passed packaging gate must produce the named installer"

    prefix = tmp_path / "installed"
    data_directory = tmp_path / "runtime-data"
    environment = os.environ | {"LOCAL_ANALYTICS_DATA_DIR": str(data_directory)}
    assert runtime_data_directory(environ=environment) == data_directory.resolve()
    _run_installer(installer, prefix, environment)
    try:
        assert (prefix / "Brain.exe").is_file()
        assert not (prefix / "Agent").exists(), "the installer must not install the Agent"
        data_directory.mkdir()
        retained_file = data_directory / "canonical.sqlite3"
        retained_file.write_bytes(b"authoritative local data")
    finally:
        _run_uninstaller(prefix, environment)
    assert not (prefix / "Brain.exe").exists(), "uninstall must remove program files"
    _assert_program_payload_removed(prefix)
    _assert_user_data_retained(retained_file)

    mutated_script = tmp_path / "brain-removes-data.iss"
    original = (ROOT / "packaging" / "inno" / "brain.iss").read_text(encoding="utf-8")
    deletion = 'Type: filesandordirs; Name: "{app}"'
    mutated = original.replace(
        deletion,
        deletion + f'\nType: filesandordirs; Name: "{data_directory}"',
        1,
    )
    assert mutated != original, "retention falsifier must mutate the actual uninstaller rule"
    mutated_script.write_text(mutated, encoding="utf-8")
    mutated_output = tmp_path / "mutated-installer"
    compiler = inno_setup_compiler.require_inno_setup_compiler(
        "Inno Setup compiler is required for the retention falsifier"
    )
    compiled = subprocess.run(
        [
            str(compiler),
            f"/DStagingRoot={build_output / 'dist' / 'Brain'}",
            f"/DOutputRoot={mutated_output}",
            f"/DAppVersion={version}",
            str(mutated_script),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    mutated_installer = mutated_output / installer.name
    assert mutated_installer.is_file()

    mutated_prefix = tmp_path / "installed-with-deletion"
    _run_installer(mutated_installer, mutated_prefix, environment)
    data_directory.mkdir(exist_ok=True)
    retained_file.write_bytes(b"authoritative local data")
    try:
        pass
    finally:
        _run_uninstaller(mutated_prefix, environment)
    assert not (mutated_prefix / "Brain.exe").exists()
    _assert_program_payload_removed(mutated_prefix)
    with pytest.raises(AssertionError, match="uninstaller must retain"):
        _assert_user_data_retained(retained_file)


@pytest.mark.skipif(os.name != "nt", reason="Inno Setup installer behavior is Windows-only")
def test_app_id_upgrade_survives_a_product_rename_and_fails_without_it(
    tmp_path: Path,
) -> None:
    """A stable AppId upgrades one install; its removal creates two real installs."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)
    build_output = tmp_path / "build"
    built = _run_build(BUILD_SCRIPT, pyinstaller, build_output, test_injection="")
    assert built.returncode == 0, built.stdout + built.stderr

    version = _authoritative_version()
    higher_version = _higher_version(version)
    staging_root = build_output / "dist" / "Brain"
    current_installer = (
        build_output
        / "installer"
        / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe"
    )
    assert current_installer.is_file(), "the current-version installer must be built"

    original_script = (ROOT / "packaging" / "inno" / "brain.iss").read_text(
        encoding="utf-8"
    )
    app_id_directive = "AppId={{a860574e-ff86-4305-be8f-93b5c91cde44}"
    original_name = '#define AppName "OnlyFans Conversational Analytics"'
    renamed_name = '#define AppName "OnlyFans Conversational Analytics Renamed Test"'
    first_prefix = tmp_path / "installed-current"
    second_prefix = tmp_path / "installed-renamed"
    renamed_script = tmp_path / "brain-renamed.iss"
    renamed_script.write_text(
        original_script.replace(original_name, renamed_name, 1).replace(
            "DefaultDirName={localappdata}\\Programs\\{#AppName}",
            f"DefaultDirName={second_prefix}",
            1,
        ),
        encoding="utf-8",
    )
    renamed_installer = _compile_installer(
        renamed_script, staging_root, tmp_path / "renamed-installer", higher_version
    )

    data_directory = tmp_path / "runtime-data"
    environment = os.environ | {"LOCAL_ANALYTICS_DATA_DIR": str(data_directory)}
    assert runtime_data_directory(environ=environment) == data_directory.resolve()
    _run_installer(current_installer, first_prefix, environment)
    try:
        _run_installer_with_directory(renamed_installer, environment)
        _assert_upgrade_installation(first_prefix, second_prefix)
    finally:
        _uninstall_all((first_prefix, second_prefix), environment)

    without_app_id = original_script.replace(app_id_directive + "\n", "", 1)
    without_app_id_script = tmp_path / "brain-without-app-id.iss"
    without_app_id_script.write_text(without_app_id, encoding="utf-8")
    without_app_id_installer = _compile_installer(
        without_app_id_script,
        staging_root,
        tmp_path / "without-app-id-current-installer",
        version,
    )
    without_app_id_renamed_script = tmp_path / "brain-renamed-without-app-id.iss"
    without_app_id_renamed_script.write_text(
        without_app_id.replace(original_name, renamed_name, 1).replace(
            "DefaultDirName={localappdata}\\Programs\\{#AppName}",
            f"DefaultDirName={second_prefix}",
            1,
        ),
        encoding="utf-8",
    )
    without_app_id_renamed_installer = _compile_installer(
        without_app_id_renamed_script,
        staging_root,
        tmp_path / "without-app-id-renamed-installer",
        higher_version,
    )

    _run_installer(without_app_id_installer, first_prefix, environment)
    try:
        _run_installer_with_directory(without_app_id_renamed_installer, environment)
        with pytest.raises(AssertionError, match="upgrade must not create a parallel"):
            _assert_upgrade_installation(first_prefix, second_prefix)
        _assert_parallel_installations(first_prefix, second_prefix)
    finally:
        _uninstall_all((first_prefix, second_prefix), environment)


def _digest_entries(digest_file: Path) -> dict[str, str]:
    """Read `<sha256> *<relative/path>` records into a path-to-digest mapping."""

    entries: dict[str, str] = {}
    for line in digest_file.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        digest, separator, relative = line.partition(" *")
        assert separator and relative, f"malformed digest record in {digest_file}: {line!r}"
        entries[relative] = digest
    return entries


def _write_digest_entries(digest_file: Path, entries: dict[str, str]) -> None:
    digest_file.write_text(
        "".join(
            f"{digest} *{relative}\n" for relative, digest in sorted(entries.items())
        ),
        encoding="ascii",
    )


def _published_names(release_directory: Path) -> list[str]:
    return sorted(path.name for path in release_directory.iterdir())


def _published_installer(release_directory: Path) -> Path:
    installers = sorted(release_directory.glob("*-Setup-*.exe"))
    assert len(installers) == 1, (
        "the published artifact set must contain one installer: "
        f"{_published_names(release_directory)}"
    )
    return installers[0]


def _assert_published_set_contains_the_agent_bundle(release_directory: Path) -> Path:
    """The published artifact set must hand the user the built Agent extension."""

    bundles = sorted(release_directory.glob("*-Agent-*.zip"))
    assert len(bundles) == 1, (
        "the published artifact set must contain the Agent extension bundle: "
        f"{_published_names(release_directory)}"
    )
    return bundles[0]


def _assert_bundle_carries_the_built_extension(bundle: Path) -> None:
    with zipfile.ZipFile(bundle) as archive:
        packed = {name: archive.read(name) for name in archive.namelist()}
    built = {
        path.relative_to(EXTENSION_DIST).as_posix(): path.read_bytes()
        for path in EXTENSION_DIST.rglob("*")
        if path.is_file()
    }
    assert built, f"the built Agent artifact is absent: {EXTENSION_DIST}"
    assert packed == built, (
        "the published Agent bundle must carry the built extension bytes"
    )


def _assert_published_digest_records_the_installer(release_directory: Path) -> None:
    """The published digest entry must equal the installer's own bytes."""

    digest_file = release_directory / DIGEST_FILE_NAME
    assert digest_file.is_file(), (
        f"the published artifact set must contain {DIGEST_FILE_NAME}: "
        f"{_published_names(release_directory)}"
    )
    installer = _published_installer(release_directory)
    entries = _digest_entries(digest_file)
    recorded = entries.get(installer.name)
    assert recorded is not None, (
        f"the published digest must record {installer.name}: {sorted(entries)}"
    )
    assert recorded == hashlib.sha256(installer.read_bytes()).hexdigest(), (
        f"the published digest for {installer.name} is not the digest of its bytes"
    )


def _assert_shipped_digest_files_list_present_files(prefix: Path) -> None:
    """Every digest file an installation ships lists only files beside it."""

    digest_files = sorted(prefix.rglob(DIGEST_FILE_NAME))
    assert digest_files, f"the installation must ship a digest file: {prefix}"
    for digest_file in digest_files:
        entries = _digest_entries(digest_file)
        assert entries, f"a shipped digest file records nothing: {digest_file}"
        for relative, digest in entries.items():
            listed = digest_file.parent / relative
            assert listed.is_file(), (
                f"{digest_file.name} lists an absent path: {relative}"
            )
            assert hashlib.sha256(listed.read_bytes()).hexdigest() == digest, (
                f"{digest_file.name} records a stale digest for {relative}"
            )


@pytest.fixture(scope="module")
def released_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real build script once and return its published artifact directory."""

    base = tmp_path_factory.mktemp("release")
    started_at = time.monotonic()
    built = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(base),
        base / "build",
        test_injection="",
    )
    assert built.returncode == 0, built.stdout + built.stderr
    print(f"release build wall-clock seconds: {time.monotonic() - started_at:.1f}")
    return base / "build" / "installer"


@pytest.mark.skipif(os.name != "nt", reason="Windows release assembly is Windows-only")
def test_release_publishes_the_built_agent_extension_bundle(
    released_build: Path, tmp_path: Path
) -> None:
    """Dropping the bundle from the published set makes the named assertion red."""

    _assert_bundle_carries_the_built_extension(
        _assert_published_set_contains_the_agent_bundle(released_build)
    )

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert _STORE_PUBLICATION in source, (
        "the bundle falsifier must remove the real Agent publication step"
    )
    mutated = source.replace(_STORE_PUBLICATION, _STORE_PUBLICATION_REMOVED, 1)
    assert mutated.count("$storeCandidate") == mutated.count("$storeCandidateName"), (
        "the mutated build script still reads a variable the excision removed; "
        "under Set-StrictMode it aborts before the assertion under test is reached"
    )
    without_bundle = tmp_path / "build-windows-without-agent-bundle.ps1"
    without_bundle.write_text(mutated, encoding="utf-8")

    output = tmp_path / "build"
    built = _run_build(
        without_bundle,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
    )
    assert built.returncode == 0, built.stdout + built.stderr

    with pytest.raises(AssertionError, match="must contain the Agent extension bundle"):
        _assert_published_set_contains_the_agent_bundle(output / "installer")
    _assert_published_digest_records_the_installer(output / "installer")


@pytest.mark.skipif(os.name != "nt", reason="Windows release assembly is Windows-only")
def test_published_digest_records_the_real_installer_bytes(
    released_build: Path, tmp_path: Path
) -> None:
    """Another artifact's digest, or one altered character, turns the entry red."""

    _assert_published_digest_records_the_installer(released_build)

    substituted = tmp_path / "substituted"
    shutil.copytree(released_build, substituted)
    bundle = _assert_published_set_contains_the_agent_bundle(substituted)
    bundle_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    entries = _digest_entries(substituted / DIGEST_FILE_NAME)
    entries[_published_installer(substituted).name] = bundle_digest
    _write_digest_entries(substituted / DIGEST_FILE_NAME, entries)

    with pytest.raises(AssertionError, match="is not the digest of its bytes"):
        _assert_published_digest_records_the_installer(substituted)

    perturbed = tmp_path / "perturbed"
    shutil.copytree(released_build, perturbed)
    entries = _digest_entries(perturbed / DIGEST_FILE_NAME)
    installer_name = _published_installer(perturbed).name
    recorded = entries[installer_name]
    entries[installer_name] = ("1" if recorded[0] == "0" else "0") + recorded[1:]
    _write_digest_entries(perturbed / DIGEST_FILE_NAME, entries)

    with pytest.raises(AssertionError, match="is not the digest of its bytes"):
        _assert_published_digest_records_the_installer(perturbed)


@pytest.mark.skipif(os.name != "nt", reason="Windows release assembly is Windows-only")
def test_shipped_digest_files_list_only_installed_files(
    released_build: Path, tmp_path: Path
) -> None:
    """Re-admitting an excluded staging path into a shipped digest turns it red."""

    data_directory = tmp_path / "runtime-data"
    environment = os.environ | {"LOCAL_ANALYTICS_DATA_DIR": str(data_directory)}
    assert runtime_data_directory(environ=environment) == data_directory.resolve()

    prefix = tmp_path / "installed"
    _run_installer(_published_installer(released_build), prefix, environment)
    try:
        _assert_shipped_digest_files_list_present_files(prefix)
    finally:
        _run_uninstaller(prefix, environment)
    _assert_program_payload_removed(prefix)

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    exclusion = '$InstallerExcludedStagingDirectories = @("Agent")'
    assert exclusion in source, (
        "the shipped-digest falsifier must empty the real exclusion declaration"
    )
    staged_only = tmp_path / "build-windows-without-installer-exclusions.ps1"
    staged_only.write_text(
        source.replace(exclusion, "$InstallerExcludedStagingDirectories = @()", 1),
        encoding="utf-8",
    )

    output = tmp_path / "build"
    built = _run_build(
        staged_only, _write_pyinstaller_standin(tmp_path), output, test_injection=""
    )
    assert built.returncode == 0, built.stdout + built.stderr

    staged_only_prefix = tmp_path / "installed-staged-only"
    _run_installer(
        _published_installer(output / "installer"), staged_only_prefix, environment
    )
    try:
        with pytest.raises(AssertionError, match="lists an absent path"):
            _assert_shipped_digest_files_list_present_files(staged_only_prefix)
    finally:
        _run_uninstaller(staged_only_prefix, environment)
    _assert_program_payload_removed(staged_only_prefix)


def _extension_version() -> str:
    """The version the built Agent artifact declares."""

    metadata = json.loads((EXTENSION_DIST / "build-meta.json").read_text(encoding="utf-8"))
    version = metadata["extension_version"]
    assert isinstance(version, str) and version
    return version


def _clear_extension_archives() -> None:
    """Remove any archive an earlier package run left in the extension tree."""

    for archive in EXTENSION_DIST.glob("conversation-analytics-*.zip"):
        archive.unlink()


def _store_candidates(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.rglob(f"*{STORE_CANDIDATE_SUFFIX}"))


def _assert_no_store_candidate(output: Path) -> None:
    """A refused release must leave no Store candidate, not merely fail."""

    published = _store_candidates(output)
    assert published == [], f"a refused release left a Store candidate: {published}"
    stranded = sorted(EXTENSION_DIST.glob("conversation-analytics-*.zip"))
    assert stranded == [], f"a refused release left a packaged archive: {stranded}"


def _audit_store_candidate(artifact: Path) -> subprocess.CompletedProcess[str]:
    """Audit an archive with the validator the release path itself runs."""

    return subprocess.run(
        [
            "node.exe",
            str(EXTENSION_ROOT / "build.mjs"),
            "--audit-package",
            f"--artifact={artifact}",
            f"--packaged-signing-rule={SIGNING_RULE_FIXTURE}",
            f"--legal-release-bindings={LEGAL_BINDINGS_FIXTURE}",
        ],
        cwd=EXTENSION_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_release_without_legal_bindings_leaves_no_store_candidate(tmp_path: Path) -> None:
    """A release missing its Legal bindings stops before any archive exists."""

    _clear_extension_archives()
    output = tmp_path / "build"

    refused = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        legal_release_bindings=None,
    )

    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "requires -LegalReleaseBindings" in refused.stdout + refused.stderr
    _assert_no_store_candidate(output)


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_release_with_invalid_legal_bindings_leaves_no_store_candidate(
    tmp_path: Path,
) -> None:
    """Bindings that do not validate stop the release before it packages."""

    _clear_extension_archives()
    bindings = json.loads(LEGAL_BINDINGS_FIXTURE.read_text(encoding="utf-8"))
    # An instrument the earlier disclosure check does not read, so the refusal
    # here is the one extension/build.mjs raises rather than that check's.
    del bindings["instruments"]["terms_of_service"]
    incomplete = tmp_path / "incomplete-bindings.json"
    incomplete.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
    output = tmp_path / "build"

    refused = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        legal_release_bindings=incomplete,
    )

    assert refused.returncode != 0, refused.stdout + refused.stderr
    combined = refused.stdout + refused.stderr
    assert "ADR 0022" in combined, combined
    assert "legal instruments contains unexpected or missing fields" in combined, combined
    _assert_no_store_candidate(output)


DISCLOSURE_INSTRUMENT = "risk_disclosure"
DISCLOSURE_ASSET = ROOT / "app" / "provisioning" / "creator-platform-data-risk-disclosure.html"


def _bindings_with_disclosure(tmp_path: Path, rendered: str | None) -> Path:
    """Write the release bindings with the bound rendering replaced or removed."""

    bindings = json.loads(LEGAL_BINDINGS_FIXTURE.read_text(encoding="utf-8"))
    instrument = bindings["instruments"][DISCLOSURE_INSTRUMENT]
    if rendered is None:
        del instrument["rendered_sha256"]
    else:
        instrument["rendered_sha256"] = rendered
    path = tmp_path / "rebound-bindings.json"
    path.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
    return path


def test_the_bound_rendering_is_the_disclosure_this_package_serves() -> None:
    """The fixture names the asset, so the release tests below can only pass honestly."""

    bindings = json.loads(LEGAL_BINDINGS_FIXTURE.read_text(encoding="utf-8"))
    bound = bindings["instruments"][DISCLOSURE_INSTRUMENT]["rendered_sha256"]
    assert bound == hashlib.sha256(DISCLOSURE_ASSET.read_bytes()).hexdigest()


def test_the_build_and_the_packaging_policy_name_one_disclosure() -> None:
    """The script's asset path and the staged path cannot drift apart silently."""

    relative = DISCLOSURE_ASSET.relative_to(ROOT).as_posix()
    windows_literal = '"' + relative.replace("/", "\\") + '"'
    assert windows_literal in BUILD_SCRIPT.read_text(encoding="utf-8"), windows_literal

    policy = json.loads(
        (ROOT / "packaging" / "runtime-files.json").read_text(encoding="utf-8")
    )
    staged = [
        entry
        for entry in policy["required_files"]
        if entry.endswith(DISCLOSURE_ASSET.name)
    ]
    assert staged == [f"_internal/{relative}"], staged


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_release_serving_an_unbound_disclosure_leaves_no_store_candidate(
    tmp_path: Path,
) -> None:
    """A disclosure that is not the bound rendering stops before any archive exists.

    Without this the package would serve one document while the Agent recorded
    acknowledgment of another, and every other check would still pass.
    """

    _clear_extension_archives()
    served = hashlib.sha256(DISCLOSURE_ASSET.read_bytes()).hexdigest()
    other = ("b" if served.startswith("a") else "a") + served[1:]
    output = tmp_path / "build"

    refused = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        legal_release_bindings=_bindings_with_disclosure(tmp_path, other),
    )

    assert refused.returncode != 0, refused.stdout + refused.stderr
    combined = refused.stdout + refused.stderr
    assert served in combined, combined
    assert other in combined, combined
    _assert_no_store_candidate(output)


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_release_binding_no_rendering_leaves_no_store_candidate(tmp_path: Path) -> None:
    """Bindings naming no rendering are refused rather than read as agreement."""

    _clear_extension_archives()
    output = tmp_path / "build"

    refused = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        legal_release_bindings=_bindings_with_disclosure(tmp_path, None),
    )

    assert refused.returncode != 0, refused.stdout + refused.stderr
    combined = refused.stdout + refused.stderr
    assert f"name no rendered {DISCLOSURE_INSTRUMENT}" in combined, combined
    _assert_no_store_candidate(output)


@pytest.fixture(scope="module")
def store_provenance_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run one controlled release with the synthetic inputs and a compiler seam."""

    base = tmp_path_factory.mktemp("store-provenance")
    _clear_extension_archives()
    built = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(base),
        base / "build",
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(base),
    )
    assert built.returncode == 0, built.stdout + built.stderr
    return base / "build"


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_controlled_release_publishes_one_store_candidate(
    store_provenance_build: Path,
) -> None:
    """A valid release publishes exactly one archive under the Store name."""

    candidates = _store_candidates(store_provenance_build)

    assert [candidate.name for candidate in candidates] == [
        f"OnlyFans-Conversational-Analytics-Agent-{_extension_version()}"
        f"{STORE_CANDIDATE_SUFFIX}"
    ]
    assert candidates[0].parent == store_provenance_build / "installer"


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_store_candidate_is_byte_identical_to_the_packaged_archive(
    store_provenance_build: Path,
) -> None:
    """The published Store name carries the packaged archive's own bytes."""

    packaged = sorted((store_provenance_build / "agent-package").glob("*.zip"))
    assert len(packaged) == 1, f"one packaged archive is expected: {packaged}"
    candidate = _store_candidates(store_provenance_build)[0]
    candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()

    assert hashlib.sha256(packaged[0].read_bytes()).hexdigest() == candidate_digest, (
        "copying the packaged archive to the Store name must preserve its bytes"
    )

    entries = _digest_entries(store_provenance_build / "installer" / DIGEST_FILE_NAME)
    assert entries[candidate.name] == candidate_digest


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_published_store_candidate_passes_its_package_audit(
    store_provenance_build: Path,
) -> None:
    """The audit accepts the archive at the exact path the release publishes."""

    audited = _audit_store_candidate(_store_candidates(store_provenance_build)[0])

    assert audited.returncode == 0, audited.stdout + audited.stderr
    assert "Chrome archive audit passed" in audited.stdout


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_mutated_store_candidate_fails_its_package_audit(
    store_provenance_build: Path, tmp_path: Path
) -> None:
    """One flipped byte in the published archive turns the same audit red."""

    candidate = _store_candidates(store_provenance_build)[0]
    payload = bytearray(candidate.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    mutated = tmp_path / candidate.name
    mutated.write_bytes(payload)
    assert mutated.read_bytes() != candidate.read_bytes()

    audited = _audit_store_candidate(mutated)

    assert audited.returncode != 0, audited.stdout + audited.stderr


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_development_packaging_publishes_no_store_candidate(
    store_provenance_build: Path, tmp_path: Path
) -> None:
    """Development mode names its bundle so a Store submission cannot use it."""

    output = tmp_path / "build"

    built = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        development_agent_bundle=True,
    )

    assert built.returncode == 0, built.stdout + built.stderr
    assert _store_candidates(output) == [], (
        "development packaging must not produce a Store candidate"
    )
    bundles = sorted((output / "development").glob("*.zip"))
    assert [bundle.name for bundle in bundles] == [
        f"agent-development-unpacked-{_extension_version()}.zip"
    ]
    assert not bundles[0].name.endswith(STORE_CANDIDATE_SUFFIX)


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_store_candidate_byte_identity_check_is_load_bearing(tmp_path: Path) -> None:
    """A publication that changes the bytes reaches the Store name only unchecked.

    Both variants copy through a step that appends to the archive and neither
    runs the package audit, so the digest comparison is the single difference
    between them.
    """

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    for fragment in (_STORE_COPY, _STORE_DIGEST_CHECK, _STORE_AUDIT):
        assert fragment in source, f"the falsifier no longer matches the script: {fragment}"
    checked_source = source.replace(
        _STORE_COPY, _STORE_COPY_THAT_CHANGES_BYTES, 1
    ).replace(_STORE_AUDIT, "", 1)
    unchecked_source = checked_source.replace(
        _STORE_DIGEST_CHECK, _STORE_DIGEST_CHECK_REMOVED, 1
    )
    assert unchecked_source != checked_source
    assert checked_source.replace(_STORE_DIGEST_CHECK, "", 1) == unchecked_source.replace(
        _STORE_DIGEST_CHECK_REMOVED, "", 1
    ), "the two scripts must differ only in the byte-identity comparison"

    unchecked_script = tmp_path / "build-windows-without-byte-identity.ps1"
    unchecked_script.write_text(unchecked_source, encoding="utf-8")
    _clear_extension_archives()
    unchecked_output = tmp_path / "unchecked"
    unchecked = _run_build(
        unchecked_script,
        _write_pyinstaller_standin(tmp_path),
        unchecked_output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
    )
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
    published = _store_candidates(unchecked_output)
    assert len(published) == 1, published
    packaged = sorted((unchecked_output / "agent-package").glob("*.zip"))
    assert hashlib.sha256(published[0].read_bytes()).hexdigest() != hashlib.sha256(
        packaged[0].read_bytes()
    ).hexdigest(), "the falsifier must actually change the published bytes"

    checked_script = tmp_path / "build-windows-with-byte-identity.ps1"
    checked_script.write_text(checked_source, encoding="utf-8")
    _clear_extension_archives()
    checked_output = tmp_path / "checked"
    checked = _run_build(
        checked_script,
        _write_pyinstaller_standin(tmp_path),
        checked_output,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
    )
    assert checked.returncode != 0, checked.stdout + checked.stderr
    assert "is not the packaged archive" in checked.stdout + checked.stderr
    _assert_no_store_candidate(checked_output)


RELEASE_GATE = ROOT / "tools" / "legal-release-bindings" / "verify.mjs"
GATE_PRODUCT_REVISION = "9988776655443322110000ffeeddccbbaa998877"
GATE_FETCH_REVISION = "1f0d2c3b4a596877665544332211ffeeddccbbaa"
GATE_DOCUMENT_PATH = "compliance/cws/releases/2.0.1/legal-release-bindings.json"
GATE_REPOSITORY = "test-owner/test-legal"
GATE_INSTALLATION_TOKEN = "ghs-synthetic-installation-token"


def _synthetic_signing_key() -> str:
    """A per-run key. The gate signs a real assertion, so it needs a real key,
    and no credential of any kind is checked in for it."""

    minted = subprocess.run(
        [
            "node.exe",
            "-e",
            "const {generateKeyPairSync}=require('node:crypto');"
            "const {privateKey}=generateKeyPairSync('rsa',{modulusLength:2048});"
            "process.stdout.write(privateKey.export({type:'pkcs8',format:'pem'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return base64.b64encode(minted.stdout.encode("ascii")).decode("ascii")


@contextmanager
def _legal_repository(document: bytes, source_revision: str) -> Iterator[str]:
    """A loopback stand-in for the Legal repository routes the gate reads."""

    contents_prefix = f"/repos/{GATE_REPOSITORY}/contents/"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server's interface
            if self.path.endswith("/access_tokens"):
                self._respond(201, json.dumps({"token": GATE_INSTALLATION_TOKEN}).encode())
                return
            self._respond(404, b"{}")

        def do_GET(self) -> None:  # noqa: N802 - http.server's interface
            path, _, query = self.path.partition("?")
            if path == f"/repos/{GATE_REPOSITORY}/commits/{source_revision}":
                self._respond(200, json.dumps({"sha": source_revision}).encode())
                return
            authorized = (
                self.headers.get("authorization") == f"Bearer {GATE_INSTALLATION_TOKEN}"
            )
            held = path == f"{contents_prefix}{GATE_DOCUMENT_PATH}"
            if authorized and held and query == f"ref={GATE_FETCH_REVISION}":
                self._respond(200, document)
                return
            self._respond(404, b"{}")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_release_gate(
    base_url: str,
    staged: Path,
    runner_temp: Path,
    *,
    source_revision: str,
    expected_digest: str,
    without: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "RUNNER_TEMP": str(runner_temp),
        "PRODUCT_REVISION": GATE_PRODUCT_REVISION,
        "GITHUB_SHA": GATE_PRODUCT_REVISION,
        "LEGAL_BINDINGS_API_BASE_URL": base_url,
        "LEGAL_BINDINGS_REPOSITORY": GATE_REPOSITORY,
        "LEGAL_BINDINGS_APP_ID": "1234",
        "LEGAL_BINDINGS_APP_PRIVATE_KEY_B64": _synthetic_signing_key(),
        "LEGAL_BINDINGS_INSTALLATION_ID": "424242",
        "LEGAL_BINDINGS_PATH": GATE_DOCUMENT_PATH,
        "LEGAL_BINDINGS_REPOSITORY_REVISION": GATE_FETCH_REVISION,
        "LEGAL_REPOSITORY_REVISION": source_revision,
        "LEGAL_BINDINGS_DIGEST": expected_digest,
    }
    environment.pop("GITHUB_WORKSPACE", None)
    if without is not None:
        environment.pop(without)
    return subprocess.run(
        ["node.exe", str(RELEASE_GATE), f"--output={staged}"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
def test_a_declared_digest_decides_whether_a_store_candidate_exists(
    tmp_path: Path,
) -> None:
    """The workflow's own order, run end to end: the gate verifies the declared
    coordinates and only then does packaging read what it staged.

    Each refused run differs from the accepted one in a single element - an
    absent retrieval credential, or one character of the declared digest - and
    must leave packaging no bindings document to read and therefore no Store
    candidate. The accepted run must produce exactly one."""

    document = LEGAL_BINDINGS_FIXTURE.read_bytes()
    source_revision = json.loads(document.decode("utf-8"))["legal_repository_revision"]
    digest = hashlib.sha256(document).hexdigest()
    wrong = digest[:63] + ("b" if digest.endswith("a") else "a")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    staged = runner_temp / "legal-release-bindings" / "legal-release-bindings.json"

    refusals = (
        # An unconfigured retrieval credential, and a digest that does not
        # describe the document held at the declared coordinates.
        ("uncredentialed", 3, {"without": "LEGAL_BINDINGS_APP_PRIVATE_KEY_B64"}),
        ("undeclared", 6, {"expected_digest": wrong}),
    )

    with _legal_repository(document, source_revision) as base_url:
        for label, code, changed in refusals:
            arguments = {
                "source_revision": source_revision,
                "expected_digest": digest,
            } | changed
            refused = _run_release_gate(base_url, staged, runner_temp, **arguments)
            assert refused.returncode == code, refused.stdout + refused.stderr
            assert not staged.exists(), "a refused gate staged a document for packaging"

            _clear_extension_archives()
            unbuilt = tmp_path / f"{label}-build"
            stopped = _run_build(
                BUILD_SCRIPT,
                _write_pyinstaller_standin(tmp_path),
                unbuilt,
                test_injection="",
                inno_setup_compiler=_write_inno_setup_standin(tmp_path),
                legal_release_bindings=staged,
            )
            assert stopped.returncode != 0, stopped.stdout + stopped.stderr
            assert "does not exist" in stopped.stdout + stopped.stderr
            _assert_no_store_candidate(unbuilt)

        accepted = _run_release_gate(
            base_url,
            staged,
            runner_temp,
            source_revision=source_revision,
            expected_digest=digest,
        )

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert staged.read_bytes() == document

    _clear_extension_archives()
    built = tmp_path / "accepted-build"
    published = _run_build(
        BUILD_SCRIPT,
        _write_pyinstaller_standin(tmp_path),
        built,
        test_injection="",
        inno_setup_compiler=_write_inno_setup_standin(tmp_path),
        legal_release_bindings=staged,
    )

    assert published.returncode == 0, published.stdout + published.stderr
    assert len(_store_candidates(built)) == 1, _store_candidates(built)
