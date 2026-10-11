"""Windows display identity; executable and installation identities stay stable."""
from pathlib import Path
import re

DISPLAY_NAME = "Conversation Analytics"
EXECUTABLE_NAME = "Brain.exe"


def version_values(config: str) -> tuple[tuple[int, int, int, int], dict[str, str]]:
    match = re.search(r'^\s*version:\s*str\s*=\s*"([0-9]+(?:\.[0-9]+){1,3})"', config, re.MULTILINE)
    if match is None:
        raise ValueError("The authoritative Brain version is absent or invalid")
    version = match.group(1)
    parts = [int(value) for value in version.split('.')]
    if any(value > 65535 for value in parts):
        raise ValueError("Windows version components must fit 16 bits")
    fixed = tuple(parts + [0] * (4 - len(parts)))
    return fixed, {
        "FileDescription": DISPLAY_NAME,
        "ProductName": DISPLAY_NAME,
        "FileVersion": version,
        "ProductVersion": version,
        "InternalName": "Brain",
        "OriginalFilename": EXECUTABLE_NAME,
    }


def version_resource(config_path: Path):
    # Import only during the Windows freeze, not from runtime app composition.
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct,
    )
    fixed, strings = version_values(config_path.read_text(encoding="utf-8"))
    return VSVersionInfo(ffi=FixedFileInfo(filevers=fixed, prodvers=fixed, mask=0x3f,
        flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), kids=[
        StringFileInfo([StringTable('040904B0', [StringStruct(key, value) for key, value in strings.items()])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ])
