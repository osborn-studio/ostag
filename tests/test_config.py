from ostag.config import Settings, load_settings


def _enabled_settings(**overrides) -> Settings:
    values = {
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "secret",
        "dingtalk_client_id": "ding_test",
        "dingtalk_client_secret": "secret",
        "dingtalk_robot_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test",
        "daily_task_boards_enabled": True,
        "daily_task_board_feishu_chat_id": "oc_feishu",
        "daily_task_board_feishu_base_token": "base_feishu",
        "daily_task_board_feishu_table_id": "tbl_feishu",
        "daily_task_board_feishu_table_url": "https://example.test/feishu",
        "daily_task_board_dingtalk_chat_id": "cid_dingtalk",
        "daily_task_board_dingtalk_base_token": "base_dingtalk",
        "daily_task_board_dingtalk_table_id": "tbl_dingtalk",
        "daily_task_board_dingtalk_table_url": "https://example.test/dingtalk",
    }
    values.update(overrides)
    return Settings(**values)


def test_daily_task_boards_are_disabled_by_default() -> None:
    assert Settings().daily_task_boards_enabled is False


def test_enabled_daily_task_boards_require_complete_profiles() -> None:
    for field in (
        "daily_task_board_feishu_chat_id",
        "daily_task_board_feishu_base_token",
        "daily_task_board_feishu_table_id",
        "daily_task_board_feishu_table_url",
        "daily_task_board_dingtalk_chat_id",
        "daily_task_board_dingtalk_base_token",
        "daily_task_board_dingtalk_table_id",
        "daily_task_board_dingtalk_table_url",
    ):
        try:
            _enabled_settings(**{field: ""}).validate()
        except ValueError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"missing {field} was accepted")


def test_daily_task_board_hours_and_poll_interval_are_validated() -> None:
    _enabled_settings().validate()
    for values in (
        {"daily_task_board_morning_hour": -1},
        {"daily_task_board_evening_hour": 24},
        {"daily_task_board_morning_hour": 9, "daily_task_board_evening_hour": 9},
        {"daily_task_board_poll_seconds": 4},
        {"daily_task_board_record_limit": 0},
        {"daily_task_board_record_limit": 501},
    ):
        try:
            _enabled_settings(**values).validate()
        except ValueError as exc:
            assert "daily task board" in str(exc)
        else:
            raise AssertionError(f"invalid values were accepted: {values}")


def test_daily_task_board_environment_profiles(monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARDS_ENABLED", "true")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "ding_test")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "DINGTALK_ROBOT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_FEISHU_CHAT_ID", "oc_feishu")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_FEISHU_BASE_TOKEN", "base_feishu")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_FEISHU_TABLE_ID", "tbl_feishu")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_FEISHU_TABLE_URL", "https://example.test/f")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_FEISHU_MANAGER_IDS", "ou_owner,ou_manager")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_DINGTALK_CHAT_ID", "cid_dingtalk")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_DINGTALK_BASE_TOKEN", "base_dingtalk")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_DINGTALK_TABLE_ID", "tbl_dingtalk")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_DINGTALK_TABLE_URL", "https://example.test/d")
    monkeypatch.setenv("OSTAG_DAILY_TASK_BOARD_DINGTALK_MANAGER_IDS", "ding_owner")

    settings = load_settings(None)

    assert settings.daily_task_boards_enabled is True
    assert settings.daily_task_board_feishu_manager_ids == ("ou_owner", "ou_manager")
    assert settings.daily_task_board_dingtalk_manager_ids == ("ding_owner",)
