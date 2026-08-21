"""Behavioural falsifiers for the Windows packaging policy build gate."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.runtime_paths import runtime_data_directory


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"
EXTENSION_DIST = ROOT / "extension" / "dist"
DIGEST_FILE_NAME = "sha256sums.txt"


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
    release_mode: bool = False,
    inno_setup_compiler: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"BRAIN_PROJECT_ROOT": str(ROOT)}
    environment.pop("WINDOWS_SIGNING_CONFIGURATION", None)
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
    if release_mode:
        command.append("-ReleaseMode")
    if inno_setup_compiler is not None:
        command.extend(("-InnoSetupCompiler", str(inno_setup_compiler)))
    if test_injection:
        command.extend(("-TestInjection", test_injection))
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_release_refuses_unsigned(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 1, (
        "release mode must refuse a build without signing configuration"
    )
    assert "WINDOWS_SIGNING_CONFIGURATION is not set" in result.stdout + result.stderr


def _assert_unsigned_engineering_build_succeeds(
    result: subprocess.CompletedProcess[str], output: Path
) -> None:
    assert result.returncode == 0, (
        "engineering mode must permit a build without signing configuration"
    )
    assert (output / "installer").is_dir()


def test_release_mode_refuses_unsigned_build_and_is_load_bearing(
    tmp_path: Path,
) -> None:
    """Changing the missing-signing failure to a warning permits the same release build."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)
    compiler = _write_inno_setup_standin(tmp_path)

    rejected_output = tmp_path / "release-rejected"
    rejected = _run_build(
        BUILD_SCRIPT,
        pyinstaller,
        rejected_output,
        test_injection="",
        release_mode=True,
        inno_setup_compiler=compiler,
    )

    _assert_release_refuses_unsigned(rejected)
    assert not (rejected_output / "installer").exists()

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    failure = (
        'throw "Release mode requires signing configuration: '
        'WINDOWS_SIGNING_CONFIGURATION is not set."'
    )
    warning = (
        'Write-Warning "Release mode requires signing configuration: '
        'WINDOWS_SIGNING_CONFIGURATION is not set."\n        return'
    )
    mutated = source.replace(failure, warning, 1)
    assert mutated != source, "release falsifier must replace the gate failure"
    warning_only_script = tmp_path / "build-windows-signing-warning.ps1"
    warning_only_script.write_text(mutated, encoding="utf-8")

    permitted_output = tmp_path / "release-warning-only"
    permitted = _run_build(
        warning_only_script,
        pyinstaller,
        permitted_output,
        test_injection="",
        release_mode=True,
        inno_setup_compiler=compiler,
    )

    with pytest.raises(AssertionError, match="release mode must refuse"):
        _assert_release_refuses_unsigned(permitted)
    assert permitted.returncode == 0, permitted.stdout + permitted.stderr
    assert (permitted_output / "installer").is_dir()


def test_engineering_build_remains_unsigned_and_is_load_bearing(
    tmp_path: Path,
) -> None:
    """Making the release-only condition unconditional rejects engineering builds."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)
    compiler = _write_inno_setup_standin(tmp_path)

    engineering_output = tmp_path / "engineering"
    engineering = _run_build(
        BUILD_SCRIPT,
        pyinstaller,
        engineering_output,
        test_injection="",
        inno_setup_compiler=compiler,
    )

    _assert_unsigned_engineering_build_succeeds(engineering, engineering_output)

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    release_only = "if (-not $ReleaseMode) {\n        return\n    }"
    unconditional = "if ($false) {\n        return\n    }"
    mutated = source.replace(release_only, unconditional, 1)
    assert mutated != source, (
        "engineering falsifier must make the release gate unconditional"
    )
    unconditional_script = tmp_path / "build-windows-unconditional-signing.ps1"
    unconditional_script.write_text(mutated, encoding="utf-8")

    rejected_output = tmp_path / "engineering-rejected"
    rejected = _run_build(
        unconditional_script,
        pyinstaller,
        rejected_output,
        test_injection="",
        inno_setup_compiler=compiler,
    )

    with pytest.raises(AssertionError, match="engineering mode must permit"):
        _assert_unsigned_engineering_build_succeeds(rejected, rejected_output)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert "WINDOWS_SIGNING_CONFIGURATION is not set" in rejected.stdout + rejected.stderr
    assert not (tmp_path / "engineering-rejected" / "installer").exists()


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
    command = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES"]
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
        [str(uninstaller), "/SILENT", "/SUPPRESSMSGBOXES"],
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
    compiler = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    assert compiler.is_file(), "Inno Setup compiler is required for installer behavior tests"
    return compiler


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
    compiler = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is unavailable for the retention falsifier")
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
    publication = (
        "New-AgentBundle -AgentRoot $stagedAgentRoot "
        "-BundlePath (Join-Path $installerOutput $agentBundleName)\n"
        "Write-Sha256Sums -Directory $installerOutput "
        "-RelativePaths @($installerName, $agentBundleName)"
    )
    assert publication in source, (
        "the bundle falsifier must remove the real Agent publication step"
    )
    without_bundle = tmp_path / "build-windows-without-agent-bundle.ps1"
    without_bundle.write_text(
        source.replace(
            publication,
            "Write-Sha256Sums -Directory $installerOutput "
            "-RelativePaths @($installerName)",
            1,
        ),
        encoding="utf-8",
    )

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
