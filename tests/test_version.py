from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

import ostag
from ostag.copilot_relay.reporter import CopilotReporter


def test_package_version_matches_project_metadata() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert ostag.__version__ == project["project"]["version"]


def test_gateway_default_matches_package_version() -> None:
    default = inspect.signature(CopilotReporter).parameters["gateway_version"].default

    assert default == ostag.__version__
