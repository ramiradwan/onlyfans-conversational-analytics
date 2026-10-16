"""Isolated release source for Windows packaging falsifiers."""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@contextmanager
def configured_release_source(module, destination: Path):
    """Keep synthetic routing and all build output out of the real checkout."""
    original = module.ROOT
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", "--quiet", str(original), str(destination)],
        capture_output=True, check=True,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=original, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    for relative in filter(None, tracked):
        source = original / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    # These generated inputs are deliberately absent from the Git inventory.
    for relative in ("extension/node_modules", "app/static/dist"):
        source = original / relative
        if source.is_dir():
            shutil.copytree(source, destination / relative, dirs_exist_ok=True)
    native = destination / "desktop/src-tauri/target/x86_64-pc-windows-msvc/release/Bridge.exe"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"native-desktop-build-fixture")
    config = destination / "app/core/customer-release.json"
    config.write_text(json.dumps({
        "schema": "ofca-customer-release/v2",
        "setup_transfer_receiver_url": "https://setup.example.com/public/onboarding/setup",
        "hosted_api_origin": "https://setup.example.com",
        "authentication_providers": ["default"],
    }), encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        for name, value in vars(module).copy().items():
            if isinstance(value, Path) and value.is_relative_to(original):
                patch.setattr(module, name, destination / value.relative_to(original))
        yield destination
