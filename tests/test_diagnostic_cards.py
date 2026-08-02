from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.connector_diagnostics import connector_test
from ostag.core.capabilities import get_capability
from ostag.core.diagnostic_cards import (
    DiagnosticAction,
    build_diagnostic_view,
    render_diagnostic_dingtalk_card,
    render_diagnostic_feishu_card,
    render_diagnostic_text,
)
from ostag.main import main_async


def _view(tmp_path: Path, *, ready: bool = True):
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        gh_bin="gh",
        github_default_repo="owner/repo" if ready else "",
    )
    report = connector_test(config, "github", which=lambda _: "/usr/bin/gh")
    return build_diagnostic_view(
        report,
        completion_rows=(
            {
                "workThreadKey": "owner/repo#12",
                "completion": "satisfied",
                "evidenceBacked": True,
                "testsPassed": True,
                "deployVerified": False,
                "prodAccepted": False,
            },
        ),
    )


def test_feishu_card_uses_status_color_bounded_content_and_text_fallback(tmp_path: Path) -> None:
    view = _view(tmp_path)
    card = render_diagnostic_feishu_card(view)
    fallback = render_diagnostic_text(view)
    encoded = json.dumps(card, ensure_ascii=False)

    assert card["header"]["template"] == "green"
    assert "owner/repo\\\\#12" in encoded
    assert "证据" in encoded
    assert "github" in fallback
    assert len(encoded.encode("utf-8")) < 24_000


def test_blocked_dingtalk_card_escapes_external_text_and_has_no_unauthorized_controls(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path, ready=False)
    card = render_diagnostic_dingtalk_card(
        view,
        actions=(
            DiagnosticAction(
                label="Retry <now>", callback_id="cap_abcdefghijklmnop", authorized=False
            ),
        ),
        action_base_url="https://control.example.test/actions",
    )
    encoded = json.dumps(card, ensure_ascii=False)

    assert card["actionCard"]["title"].startswith("[BLOCKED]")
    assert "cap_abcdefghijklmnop" not in encoded
    assert "<now>" not in encoded


def test_authorized_controls_only_expose_opaque_capability_ids(tmp_path: Path) -> None:
    action = DiagnosticAction(
        label="Refresh status", callback_id="cap_abcdefghijklmnop", authorized=True
    )
    view = _view(tmp_path)

    feishu = render_diagnostic_feishu_card(view, actions=(action,))
    dingtalk = render_diagnostic_dingtalk_card(
        view,
        actions=(action,),
        action_base_url="https://control.example.test/actions",
    )
    feishu_encoded = json.dumps(feishu)
    dingtalk_encoded = json.dumps(dingtalk)

    assert '"capability_id": "cap_abcdefghijklmnop"' in feishu_encoded
    assert "https://control.example.test/actions/cap_abcdefghijklmnop" in dingtalk_encoded
    assert "owner/repo" not in dingtalk["actionCard"]["btns"][0]["actionURL"]


@pytest.mark.parametrize(
    "callback_id",
    ["github:merge:123", "cap_short", "https://example.test/action", "cap_bad/value_123456"],
)
def test_action_callback_must_be_opaque(callback_id: str) -> None:
    with pytest.raises(ValueError, match="opaque"):
        DiagnosticAction(label="Action", callback_id=callback_id, authorized=True)


async def test_connector_cli_can_render_feature_gated_native_card(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OSTAG_NATIVE_DIAGNOSTIC_CARDS_ENABLED", "true")
    monkeypatch.setenv("OSTAG_GITHUB_DEFAULT_REPO", "owner/repo")

    exit_code = await main_async(
        ["connector", "test", "github", "--card-platform", "feishu", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["card_platform"] == "feishu"
    assert payload["card"]["header"]["template"] == "green"
    assert "fallback" in payload


def test_native_card_capabilities_match_implemented_adapters() -> None:
    assert get_capability("feishu", "chat").capabilities["can_render_cards"] is True
    assert get_capability("lark", "chat").capabilities["can_render_cards"] is True
    assert get_capability("dingtalk", "chat").capabilities["can_render_cards"] is True
    assert get_capability("slack", "chat").capabilities["can_render_cards"] is False
