from __future__ import annotations

import json
from pathlib import Path

from ostag.config import load_settings
from ostag.core.channel_brain import ChannelBrainStore, ChannelPolicy, format_channel_brain


def test_channel_brain_is_platform_and_chat_scoped(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    store = ChannelBrainStore(config, root=tmp_path)

    store.remember_fact("feishu", "oc_one", "张三负责交易相关需求")
    store.remember_fact("feishu", "oc_two", "李四负责市场相关需求")

    assert "张三负责交易" in store.load("feishu", "oc_one").facts[0]
    assert "李四负责市场" in store.load("feishu", "oc_two").facts[0]
    assert store.load("feishu", "oc_one").facts != store.load("feishu", "oc_two").facts


def test_channel_brain_defaults_policy_from_env(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_env",
        auto_base_default_table_id="tblEnv",
    )
    store = ChannelBrainStore(config, root=tmp_path)

    brain = store.load("feishu", "oc_demo")

    assert brain.policy.default_base_token == "base_env"
    assert brain.policy.default_table_id == "tblEnv"


def test_channel_brain_file_policy_overrides_env(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_env",
        auto_base_default_table_id="tblEnv",
    )
    store = ChannelBrainStore(config, root=tmp_path)
    path = store.path_for("feishu", "oc_demo")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "platform": "feishu",
                "chat_id": "oc_demo",
                "facts": [],
                "preferences": [],
                "policy": {
                    "default_base_token": "base_channel",
                    "default_table_id": "tblChannel",
                },
            }
        ),
        encoding="utf-8",
    )

    brain = store.load("feishu", "oc_demo")

    assert brain.policy == ChannelPolicy(
        default_base_token="base_channel",
        default_table_id="tblChannel",
    )


def test_format_channel_brain_reports_memory_and_default_table(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_env",
        auto_base_default_table_id="tblEnv",
    )
    store = ChannelBrainStore(config, root=tmp_path)
    brain = store.remember_fact("feishu", "oc_demo", "张三负责交易相关需求")

    text = format_channel_brain(brain)

    assert "张三负责交易相关需求" in text
    assert "base=base_env" in text
    assert "table=tblEnv" in text
