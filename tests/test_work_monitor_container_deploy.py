from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _config(*files: str, env: dict[str, str] | None = None) -> dict:
    command = ["docker", "compose"]
    for file in files:
        command.extend(("-f", file))
    command.append("config")
    result = subprocess.run(command, check=True, capture_output=True, text=True, env=env)
    return yaml.safe_load(result.stdout)


def test_default_compose_uses_named_volume_and_nonroot_image() -> None:
    config = _config("deploy/work-monitor/docker-compose.yml")
    mount = config["services"]["work-monitor"]["volumes"][0]
    dockerfile = Path("deploy/work-monitor/Dockerfile").read_text(encoding="utf-8")

    assert mount["type"] == "volume" and mount["target"] == "/var/lib/ostag"
    assert "USER ostag" in dockerfile
    assert "requirements.lock" in dockerfile
    assert "import lark_oapi; import Crypto" in dockerfile
    assert "FROM node:22-slim AS node" in dockerfile
    assert "@larksuite/cli@1.0.69" in dockerfile
    assert "dingtalk-workspace-cli@1.0.51" in dockerfile


def test_production_override_uses_explicit_host_path(tmp_path: Path) -> None:
    env = os.environ | {"OSTAG_MONITOR_DATA_DIR": str(tmp_path)}
    config = _config(
        "deploy/work-monitor/docker-compose.yml",
        "deploy/work-monitor/docker-compose.production.yml",
        env=env,
    )
    mounts = config["services"]["work-monitor"]["volumes"]

    assert mounts == [
        {
            "type": "bind",
            "source": str(tmp_path),
            "target": "/var/lib/ostag",
        }
    ]
