import pytest

from scripts.email_tokens import token_from_email


def test_token_from_styled_plain_text_email() -> None:
    text = (
        "Kaede Chat\n\nFinish creating your account.\n\n"
        "Verify email: https://chat.example/verify#token=kc1_ot_secret\n\n"
        "This link expires in 48 hours."
    )

    assert token_from_email(text) == "kc1_ot_secret"


def test_token_from_email_rejects_unrelated_links() -> None:
    with pytest.raises(RuntimeError, match="action token"):
        token_from_email("Help: https://chat.example/support")
