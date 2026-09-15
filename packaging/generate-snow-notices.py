"""Retain upstream notices for every locked native Snow dependency."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native/companion-snow"


def main() -> None:
    result = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--locked",
         "--filter-platform", "x86_64-pc-windows-msvc"],
        cwd=CRATE, check=True, capture_output=True, text=True,
    )
    metadata = json.loads(result.stdout)
    used = {node["id"] for node in metadata["resolve"]["nodes"]}
    notices = []
    for package in sorted(metadata["packages"], key=lambda value: value["name"]):
        if package["id"] not in used or not package["source"]:
            continue
        directory = Path(package["manifest_path"]).parent
        files = sorted(path for path in directory.iterdir() if path.is_file()
                       and re.match(r"^(?:licen[sc]e|copying|notice)(?:[._-]|$)", path.name, re.I))
        if not files:
            raise RuntimeError(f"license absent for {package['name']}")
        notices.append(f"{package['name']} {package['version']} ({package['license']})")
        for path in files:
            notices.append(path.name + "\n" + path.read_text(encoding="utf-8").strip())
    (CRATE / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n\n".join(notices) + "\n", encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    main()
