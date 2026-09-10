"""Build and verify a Windows sqlcipher3 wheel from pinned sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from re import search
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCES = Path(__file__).with_name("fixed-runtime-sources.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(item: dict[str, str], destination: Path) -> None:
    with urllib.request.urlopen(item["source_url"]) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = _sha256(destination)
    if actual != item["sha256"]:
        raise RuntimeError(f"checksum mismatch for {destination.name}: {actual}")


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as contents:
        for member in contents.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeError(f"unsafe tar member: {member.name}")
        contents.extractall(destination, filter="data")


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as contents:
        for member in contents.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeError(f"unsafe zip member: {member.filename}")
        contents.extractall(destination)


def _single_directory(path: Path) -> Path:
    directories = [item for item in path.iterdir() if item.is_dir()]
    if len(directories) != 1:
        raise RuntimeError(f"expected one source directory in {path}, found {directories}")
    return directories[0]


def _run(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    rendered = command if isinstance(command, str) else subprocess.list2cmdline(command)
    print("+", rendered, flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True, shell=isinstance(command, str))


def _vcvars64() -> Path:
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = program_files_x86 / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise RuntimeError("MSVC discovery tool vswhere.exe is unavailable")
    result = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        text=True,
        capture_output=True,
        check=True,
    )
    install = Path(result.stdout.strip())
    script = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not script.is_file():
        raise RuntimeError(f"x64 MSVC environment is unavailable below {install}")
    return script


def _cmd_with_msvc(vcvars: Path, body: str) -> str:
    # Pass one command string so cmd.exe handles quoted vcvars64 paths.
    return f'call "{vcvars}" && {body}'


def _python_version(python_executable: Path) -> str:
    return subprocess.run(
        [str(python_executable), "-c", "import sys; print(sys.version)"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _msvc_compiler_version(vcvars: Path) -> str:
    result = subprocess.run(
        _cmd_with_msvc(vcvars, "cl"),
        shell=True,
        text=True,
        capture_output=True,
    )
    match = search(r"Compiler Version ([0-9.]+) for", result.stdout + result.stderr)
    if not match:
        raise RuntimeError("could not identify the MSVC compiler version")
    return match.group(1)


def _openssl_port_version(installed: Path) -> str:
    spdx = installed / "share" / "openssl" / "vcpkg.spdx.json"
    document = json.loads(spdx.read_text(encoding="utf-8"))
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise RuntimeError("vcpkg OpenSSL SPDX document has no package list")
    for package in packages:
        if isinstance(package, dict) and package.get("name") == "openssl":
            version = package.get("versionInfo")
            if isinstance(version, str) and version:
                return version
    raise RuntimeError("vcpkg OpenSSL SPDX document has no port version")


def _rewrite_binding_version(binding: Path, version: str) -> None:
    setup = binding / "setup.py"
    text = setup.read_text(encoding="utf-8")
    expected = "VERSION = '0.6.2'"
    if expected not in text:
        raise RuntimeError("sqlcipher3 setup.py no longer has the reviewed version assignment")
    setup.write_text(text.replace(expected, f"VERSION = '{version}'", 1), encoding="utf-8")
    project = binding / "pyproject.toml"
    project_text = project.read_text(encoding="utf-8")
    expected_project = 'version = "0.6.2"'
    if expected_project not in project_text:
        raise RuntimeError("sqlcipher3 pyproject.toml no longer has the reviewed version")
    project.write_text(
        project_text.replace(
            expected_project,
            f'version = "{version}"\nlicense-files = ["LICENSE", "ofca-licenses/*/*"]',
            1,
        ),
        encoding="utf-8",
    )


def _retain_runtime_licenses(
    *, binding: Path, cipher: Path, installed: Path, wheelhouse: Path,
    version: str, distribution_notices: Path,
) -> dict[str, str]:
    """Keep native notices in the wheel and release record before cleanup."""

    sources = {
        "binding/LICENSE": binding / "LICENSE",
        "sqlcipher/LICENSE.md": cipher / "LICENSE.md",
        "sqlcipher/LICENSE.txt": cipher / "LICENSE.txt",
        "sqlcipher/SQLITE_LICENSE.md": cipher / "SQLITE_LICENSE.md",
        "openssl/LICENSE.txt": installed / "share" / "openssl" / "copyright",
    }
    distributed_text = distribution_notices.read_text(encoding="utf-8")
    # The installer already distributes THIRD_PARTY_NOTICES.md. Refuse a native
    # dependency update until that file includes the new upstream notices.
    for name, source in sources.items():
        notice = "\n".join(
            line.rstrip() for line in source.read_text(encoding="utf-8").splitlines()
        ).strip()
        if not notice or notice not in distributed_text:
            raise RuntimeError(f"distributed third-party notices omit {name}")

    retained: dict[str, str] = {}
    for name, source in sources.items():
        relative = Path(f"sqlcipher3-{version}.licenses") / name
        destination = wheelhouse / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        retained[relative.as_posix()] = _sha256(destination)
        # setuptools includes these files through the explicit license-files
        # declaration. The binding's own LICENSE is already included there.
        if not name.startswith("binding/"):
            wheel_notice = binding / "ofca-licenses" / name
            wheel_notice.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, wheel_notice)
    return retained


def _assert_generated_runtime(amalgamation: Path) -> str:
    """Bind the generated source to the expected SQLCipher/SQLite release facts."""

    text = amalgamation.read_text(encoding="utf-8", errors="strict")
    expected = ('#define SQLITE_VERSION        "3.53.3"', "#define CIPHER_VERSION_NUMBER 4.17.0")
    missing = [item for item in expected if item not in text]
    if missing:
        raise RuntimeError(f"generated SQLCipher amalgamation lacks {missing}")
    return _sha256(amalgamation)


def _build(
    *, root: Path, wheelhouse: Path, python_executable: Path, sources: dict[str, Any]
) -> int:
    builder_environment = root / "wheel-builder"
    _run([str(python_executable), "-m", "venv", str(builder_environment)])
    builder_python = builder_environment / "Scripts" / "python.exe"
    _run([
        str(builder_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps",
        f"setuptools=={sources['builder_tools']['setuptools']}",
        f"wheel=={sources['builder_tools']['wheel']}",
    ])
    downloads = root / "downloads"
    downloads.mkdir(exist_ok=True)
    binding_archive = downloads / "sqlcipher3-0.6.2.tar.gz"
    cipher_archive = downloads / "sqlcipher-4.17.0.zip"
    vcpkg_archive = downloads / "vcpkg.zip"
    _download(sources["binding"], binding_archive)
    _download(sources["sqlcipher"], cipher_archive)
    _download(sources["vcpkg"], vcpkg_archive)
    binding_extract, cipher_extract, vcpkg_extract = root / "binding", root / "cipher", root / "vcpkg"
    for item in (binding_extract, cipher_extract, vcpkg_extract):
        item.mkdir()
    _safe_extract_tar(binding_archive, binding_extract)
    _safe_extract_zip(cipher_archive, cipher_extract)
    _safe_extract_zip(vcpkg_archive, vcpkg_extract)
    binding, cipher, vcpkg = map(_single_directory, (binding_extract, cipher_extract, vcpkg_extract))
    _rewrite_binding_version(binding, sources["binding"]["local_version"])
    vcvars = _vcvars64()
    _run(_cmd_with_msvc(vcvars, "nmake /nologo /f Makefile.msc sqlite3.c"), cwd=cipher)
    # Validate the amalgamation before compiling the binding.
    amalgamation_sha256 = _assert_generated_runtime(cipher / "sqlite3.c")
    for name in ("sqlite3.c", "sqlite3.h"):
        generated = cipher / name
        if not generated.is_file():
            raise RuntimeError(f"SQLCipher did not generate {name}")
        shutil.copy2(generated, binding / "vendor" / name)
    _run([str(vcpkg / "bootstrap-vcpkg.bat"), "-disableMetrics"], cwd=vcpkg)
    _run([str(vcpkg / "vcpkg.exe"), "install", "openssl", "--triplet", sources["vcpkg"]["triplet"], "--disable-metrics"], cwd=vcpkg)
    installed = vcpkg / "installed" / sources["vcpkg"]["triplet"]
    openssl_include, openssl_lib = installed / "include", installed / "lib"
    if not (openssl_include / "openssl" / "crypto.h").is_file() or not (openssl_lib / "libcrypto.lib").is_file():
        raise RuntimeError("the reviewed static vcpkg OpenSSL layout is unavailable")
    compiler_version = _msvc_compiler_version(vcvars)
    openssl_port_version = _openssl_port_version(installed)
    retained_licenses = _retain_runtime_licenses(
        binding=binding,
        cipher=cipher,
        installed=installed,
        wheelhouse=wheelhouse,
        version=sources["binding"]["local_version"],
        distribution_notices=ROOT / "THIRD_PARTY_NOTICES.md",
    )
    # Add vcpkg after vcvars64 replaces INCLUDE and LIB.
    body = (
        f'set "INCLUDE={openssl_include};%INCLUDE%" && '
        f'set "LIB={openssl_lib};%LIB%" && '
        f'"{builder_python}" -m pip wheel --no-build-isolation --no-deps '
        f'--wheel-dir "{wheelhouse}" "{binding}"'
    )
    _run(_cmd_with_msvc(vcvars, body), cwd=binding)
    wheels = sorted(wheelhouse.glob("sqlcipher3-0.6.2+ofca.1-*-win_amd64.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one fixed sqlcipher3 wheel, found {wheels}")
    wheel = wheels[0]
    wheel_contents = root / "wheel-contents"
    wheel_contents.mkdir()
    _safe_extract_zip(wheel, wheel_contents)
    extensions = list((wheel_contents / "sqlcipher3").glob("_sqlite3*.pyd"))
    if len(extensions) != 1:
        raise RuntimeError(f"expected one SQLCipher extension in the wheel, found {extensions}")
    dependency_scan = subprocess.run(
        _cmd_with_msvc(vcvars, f'dumpbin /dependents "{extensions[0]}"'),
        text=True,
        capture_output=True,
        check=True,
        shell=True,
    ).stdout.lower()
    if "libcrypto" in dependency_scan or "libssl" in dependency_scan:
        raise RuntimeError("fixed wheel has an external OpenSSL DLL dependency")
    provenance = {
        "schema_version": 1,
        "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
        "sources": {**sources, "generated_amalgamation_sha256": amalgamation_sha256},
        "build": {
            "target_python": str(python_executable),
            "builder_python": str(builder_python),
            "target_python_version": _python_version(python_executable),
            "builder_python_version": _python_version(builder_python),
            "platform": os.name,
            "msvc_environment": str(vcvars),
            "msvc_compiler_version": compiler_version,
            "vcpkg_openssl": {
                "port": sources["vcpkg"]["port"],
                "version": openssl_port_version,
                "triplet": sources["vcpkg"]["triplet"],
            },
            "openssl_linkage": "static /MD; dumpbin found no libcrypto/libssl DLL dependency",
        },
        "licenses_retained": retained_licenses,
    }
    (wheelhouse / "sqlcipher3-0.6.2+ofca.1.provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(provenance, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, type=Path, dest="python_executable")
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--work-root", type=Path)
    arguments = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("the shipped dependency is a Windows wheel; run this on Windows")
    python_executable = arguments.python_executable.resolve()
    if not python_executable.is_file():
        raise RuntimeError(f"build Python does not exist: {python_executable}")
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    wheelhouse = arguments.wheelhouse.resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if arguments.work_root is not None:
        root = arguments.work_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return _build(
            root=root,
            wheelhouse=wheelhouse,
            python_executable=python_executable,
            sources=sources,
        )
    # Preserve explicit work roots for diagnosis; remove temporary defaults.
    with tempfile.TemporaryDirectory(prefix="ofca-fixed-sqlcipher-") as temporary:
        return _build(
            root=Path(temporary).resolve(),
            wheelhouse=wheelhouse,
            python_executable=python_executable,
            sources=sources,
        )


if __name__ == "__main__":
    raise SystemExit(main())
