"""Keep offline timezone data separate from optional language-model dependencies."""

import ast
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def test_timezone_data_loads_without_a_system_zone_database():
    for name in (
        "UTC",
        "Europe/Helsinki",
        "America/New_York",
        "Asia/Kolkata",
        "Pacific/Apia",
    ):
        with (
            resources.files("tzdata.zoneinfo")
            .joinpath(*name.split("/"))
            .open("rb") as stream
        ):
            assert ZoneInfo.from_file(stream, key=name).key == name


def test_frozen_application_retains_timezone_data_and_license_metadata():
    tree = ast.parse((ROOT / "packaging/pyinstaller/brain.spec").read_text())
    calls = {
        (node.func.id, node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert ("collect_data_files", "tzdata") in calls
    assert ("copy_metadata", "tzdata") in calls


def test_base_runtime_adds_no_optional_ml_dependencies():
    requirements = (ROOT / "requirements.txt").read_text().splitlines()
    names = {line.split("==")[0].lower() for line in requirements if "==" in line}
    assert "tzdata==2026.4" in requirements
    assert not names & {
        "spacy",
        "torch",
        "transformers",
        "sentence-transformers",
        "onnxruntime",
        "llama-cpp-python",
    }
