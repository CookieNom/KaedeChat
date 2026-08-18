import base64

import pytest
from pydantic import ValidationError

from app.auth.schemas import MfaSetupRequest, RegisterRequest, SettingsPatch

DERIVED_PASSWORD = "A" * 43
AUTH_SALT = base64.urlsafe_b64encode(bytes(16)).decode().rstrip("=")
VAULT_SALT = base64.urlsafe_b64encode(bytes([1]) * 16).decode().rstrip("=")
PASSWORD_KDF = {
    "version": 2,
    "algorithm": "PBKDF2-SHA256",
    "iterations": 600_000,
    "auth_salt": AUTH_SALT,
    "vault_salt": VAULT_SALT,
}


def test_registration_normalizes_username_and_email() -> None:
    payload = RegisterRequest(
        username="Maple.Leaf",
        email="USER@example.com",
        password=DERIVED_PASSWORD,
        password_kdf=PASSWORD_KDF,
    )
    assert payload.username == "maple.leaf"
    assert str(payload.email) == "USER@example.com"


def test_registration_schema_allows_omitting_email() -> None:
    payload = RegisterRequest(
        username="maple",
        password=DERIVED_PASSWORD,
        password_kdf=PASSWORD_KDF,
    )
    assert payload.email is None


@pytest.mark.parametrize("username", ["A", "space name", "hyphen-name", "éclair"])
def test_registration_rejects_invalid_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(
            username=username,
            email="user@example.com",
            password=DERIVED_PASSWORD,
            password_kdf=PASSWORD_KDF,
        )


def test_settings_patch_distinguishes_absent_fields() -> None:
    patch = SettingsPatch(theme="dark")
    assert patch.model_dump(exclude_unset=True) == {"theme": "dark"}


def test_settings_patch_validates_presence_preference() -> None:
    assert SettingsPatch(presence_preference="dnd").presence_preference == "dnd"
    with pytest.raises(ValidationError):
        SettingsPatch(presence_preference="busy")  # type: ignore[arg-type]


def test_mfa_setup_requires_password_and_bounds_the_current_factor() -> None:
    payload = MfaSetupRequest(password="current password", current_code="123456")
    assert payload.current_code == "123456"
    with pytest.raises(ValidationError):
        MfaSetupRequest()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        MfaSetupRequest(password="current password", current_code="1")
