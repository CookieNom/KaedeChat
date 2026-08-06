import base64

import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.email.backends import ConsoleEmailBackend, SmtpEmailBackend, create_email_backend

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_console_backend_is_default() -> None:
    assert isinstance(create_email_backend(settings()), ConsoleEmailBackend)


def test_smtp_backend_parses_tls_credentials() -> None:
    backend = create_email_backend(
        settings(
            email_backend="smtp",
            smtp_url=SecretStr("smtps://user:pass@smtp.example.com:465"),
            email_from_address="hello@example.com",
        )
    )
    assert isinstance(backend, SmtpEmailBackend)
    assert backend.host == "smtp.example.com"
    assert backend.use_tls


def test_smtp_backend_requires_url() -> None:
    with pytest.raises(ValueError, match="smtp_url"):
        create_email_backend(settings(email_backend="smtp"))


def test_disabled_backend_cannot_be_used_for_delivery() -> None:
    with pytest.raises(ValueError, match="disabled"):
        create_email_backend(settings(email_backend="disabled"))
