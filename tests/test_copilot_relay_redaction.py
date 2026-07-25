import pytest

from ostag.copilot_relay.redaction import SanitizedText, sanitize_assistant_text


def test_safe_assistant_text_passes_and_is_capped_by_code_points() -> None:
    safe = sanitize_assistant_text("已完成测试，可以继续部署。")
    long = sanitize_assistant_text("界" * 4_001)

    assert safe == SanitizedText(
        text="已完成测试，可以继续部署。",
        redaction_count=0,
        blocked_categories=(),
        truncated=False,
    )
    assert len(long.text) == 4_000
    assert long.truncated is True


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "raw_token"),
        ("bot token 1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", "raw_token"),
        ("api_key=sk-proj-abcdefghijklmnopqrstuvwxyz123456", "raw_token"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret", "private_key"),
        ("Cookie: session=very-sensitive-cookie-value", "cookie"),
        ("/Users/alice/project/.env contains the value", "local_path"),
        ("/home/alice/.ssh/id_ed25519", "local_path"),
        ("```diff\n@@ -1 +1 @@\n-secret\n+secret\n```", "diff"),
        ("$ export TOKEN=secret\n$ pnpm deploy", "shell_transcript"),
    ],
)
def test_sensitive_or_raw_developer_material_is_blocked(text: str, category: str) -> None:
    result = sanitize_assistant_text(text)

    assert result.text == ""
    assert category in result.blocked_categories
    assert result.redaction_count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "Ａｕｔｈｏｒｉｚａｔｉｏｎ： Ｂｅａｒｅｒ abcdefghijklmnopqrstuvwxyz123456",
        "api_key=s\u200bk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "/Users/alice/project/.e\u200bnv",
    ],
)
def test_unicode_obfuscation_does_not_bypass_secret_scanning(text: str) -> None:
    result = sanitize_assistant_text(text)

    assert result.text == ""
    assert result.blocked_categories


@pytest.mark.parametrize("value", [None, 7, b"secret"])
def test_sanitizer_rejects_non_string_input(value: object) -> None:
    with pytest.raises(ValueError, match="string"):
        sanitize_assistant_text(value)  # type: ignore[arg-type]


def test_empty_text_is_allowed_for_an_empty_assistant_snapshot() -> None:
    assert sanitize_assistant_text("").text == ""
