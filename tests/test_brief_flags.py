from dataclasses import fields
from pathlib import Path

import pytest

from ostag.config import BRIEF_FLAGS, Settings, load_settings


EXPECTED = {
    "brief_enabled": "OSTAG_BRIEF_ENABLED",
    "brief_meetings_enabled": "OSTAG_BRIEF_MEETINGS_ENABLED",
    "brief_history_enabled": "OSTAG_BRIEF_HISTORY_ENABLED",
    "brief_actions_enabled": "OSTAG_BRIEF_ACTIONS_ENABLED",
    "brief_routines_enabled": "OSTAG_BRIEF_ROUTINES_ENABLED",
    "brief_sales_enabled": "OSTAG_BRIEF_SALES_ENABLED",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    # Never load local dotenv or platform/model credentials in these tests.
    monkeypatch.setattr("os.environ", {})


def test_registry_and_defaults():
    assert dict(BRIEF_FLAGS) == EXPECTED
    registered = {f.name for f in fields(Settings) if f.name.startswith("brief_")}
    assert registered == set(EXPECTED) | {"brief_status_dir", "brief_control_path"}
    settings = Settings().validate()
    assert all(getattr(settings, flag) is False for flag in EXPECTED)
    assert settings.brief_status_dir == Path("/run/ostag-brief-status")
    assert settings.brief_control_path == Path("/run/ostag-brief-control/control.json")


@pytest.mark.parametrize("flag", EXPECTED)
@pytest.mark.parametrize("value", [True, 0, 1, None, "false", [], {}])
def test_settings_reject_enabled_and_non_boolean(flag, value):
    with pytest.raises(ValueError, match="not ready|boolean"):
        Settings(**{flag: value}).validate()


@pytest.mark.parametrize("flag,env", EXPECTED.items())
@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "", "garbage", "null", "2"])
def test_env_rejects_enabled_or_malformed(tmp_path, monkeypatch, flag, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match="not ready|boolean"):
        load_settings(str(tmp_path / "absent.env"))


@pytest.mark.parametrize("flag,env", EXPECTED.items())
@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FALSE "])
def test_false_env_values(tmp_path, monkeypatch, flag, env, value):
    monkeypatch.setenv(env, value)
    assert getattr(load_settings(str(tmp_path / "absent.env")), flag) is False


def test_env_wins_over_dotenv_and_paths_are_inert(tmp_path, monkeypatch):
    dotenv = tmp_path / "synthetic.env"
    dotenv.write_text("OSTAG_BRIEF_ENABLED=true\nOSTAG_BRIEF_STATUS_DIR=/unused\n")
    monkeypatch.setenv("OSTAG_BRIEF_ENABLED", "false")
    monkeypatch.setenv("OSTAG_BRIEF_STATUS_DIR", str(tmp_path / "status"))
    monkeypatch.setenv("OSTAG_BRIEF_CONTROL_PATH", str(tmp_path / "control.json"))
    settings = load_settings(str(dotenv))
    assert settings.brief_enabled is False
    assert settings.brief_status_dir == tmp_path / "status"
    assert settings.brief_control_path == tmp_path / "control.json"
    assert not settings.brief_status_dir.exists()
    assert not settings.brief_control_path.exists()
