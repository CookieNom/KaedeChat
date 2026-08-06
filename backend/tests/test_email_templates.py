from app.email.templates import (
    email_change_confirmation,
    password_reset_email,
    verification_email,
)


def test_verification_email_has_branded_html_and_plain_text_fallback() -> None:
    message = verification_email(
        to="maple@example.com",
        app_url="https://chat.example.com/",
        token="kc1_ot_secret",
        expires_in_hours=48,
    )

    expected_url = "https://chat.example.com/verify#token=kc1_ot_secret"
    assert message.subject == "Verify your Kaede Chat account"
    assert expected_url in message.text
    assert "48 hours" in message.text
    assert message.html is not None
    assert "Kaede Chat" in message.html
    assert "Verify email" in message.html
    assert "#b83b26" in message.html
    assert message.html.count(expected_url) == 3
    assert "<img" not in message.html


def test_transactional_email_escapes_the_action_url() -> None:
    message = verification_email(
        to="maple@example.com",
        app_url='https://chat.example.com/?theme=<unsafe>&next="quoted"',
        token="kc1_ot_secret",
        expires_in_hours=48,
    )

    assert message.html is not None
    assert "<unsafe>" not in message.html
    assert "&lt;unsafe&gt;" in message.html
    assert "&quot;quoted&quot;" in message.html


def test_password_reset_and_email_change_use_their_expected_routes() -> None:
    reset = password_reset_email(
        to="maple@example.com",
        app_url="https://chat.example.com",
        token="reset-token",
        expires_in_minutes=30,
    )
    change = email_change_confirmation(
        to="new@example.com",
        app_url="https://chat.example.com",
        token="change-token",
    )

    assert "/reset-password#token=reset-token" in reset.text
    assert "30 minutes" in reset.text
    assert "/verify-email-change#token=change-token" in change.text
    assert reset.html is not None
    assert change.html is not None
