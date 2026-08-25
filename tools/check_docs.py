#!/usr/bin/env python3
"""Check repository Markdown links and a small set of mechanical style rules."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "node_modules", "dist", "build", "coverage"}

INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+\S")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)
    )


def link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<"):
        closing = value.find(">")
        return value[1:closing] if closing != -1 else value
    return value.split(maxsplit=1)[0]


def is_external(target: str) -> bool:
    parsed = urlsplit(target)
    return bool(parsed.scheme or parsed.netloc) or target.startswith("/")


def local_target(source: Path, target: str) -> Path | None:
    if not target or target.startswith("#") or is_external(target):
        return None
    path_text = unquote(urlsplit(target).path)
    if not path_text:
        return None
    return (source.parent / path_text).resolve()


def check_file(path: Path) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    relative = path.relative_to(ROOT).as_posix()

    if text and not text.endswith("\n"):
        findings.append(f"{relative}: final newline missing")

    fence_marker: str | None = None
    previous_heading_level = 0
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if line.endswith((" ", "\t")):
            findings.append(f"{relative}:{number}: trailing whitespace")

        fence_match = FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            remainder = fence_match.group(2)
            if fence_marker is None:
                fence_marker = marker
                continue
            if (
                marker[0] == fence_marker[0]
                and len(marker) >= len(fence_marker)
                and not remainder.strip()
            ):
                fence_marker = None
                continue

        if fence_marker is not None:
            continue

        if "\t" in line:
            findings.append(f"{relative}:{number}: tab outside fenced code block")

        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if level > previous_heading_level + 1:
                findings.append(
                    f"{relative}:{number}: heading jumps from level "
                    f"{previous_heading_level} to {level}"
                )
            previous_heading_level = level

        candidates = [match.group(1) for match in INLINE_LINK.finditer(line)]
        reference = REFERENCE_LINK.match(line)
        if reference:
            candidates.append(reference.group(1))

        for raw_target in candidates:
            target = link_target(raw_target)
            resolved = local_target(path, target)
            if resolved is None:
                continue
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                findings.append(
                    f"{relative}:{number}: relative link escapes repository: {target}"
                )
                continue
            if not resolved.exists():
                findings.append(f"{relative}:{number}: broken relative link: {target}")

    if fence_marker is not None:
        findings.append(f"{relative}: unclosed fenced code block")

    return findings


def main() -> int:
    files = markdown_files()
    findings = [finding for path in files for finding in check_file(path)]
    if findings:
        print("Documentation checks failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"Documentation checks passed ({len(files)} Markdown files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
