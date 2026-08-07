from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.moderation import future_expiry
from app.chat.schemas import BanCreate, MemberUpdate


def test_sanction_expiry_requires_a_future_timezone_aware_value() -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(hours=1)

    assert future_expiry(expiry, code="BAN_EXPIRY") == expiry
    with pytest.raises(HTTPException) as naive:
        future_expiry(expiry.replace(tzinfo=None), code="BAN_EXPIRY")
    assert naive.value.detail == {"code": "BAN_EXPIRY_REQUIRES_TIMEZONE"}
    with pytest.raises(HTTPException) as past:
        future_expiry(now - timedelta(seconds=1), code="BAN_EXPIRY")
    assert past.value.detail == {"code": "BAN_EXPIRY_MUST_BE_FUTURE"}


def test_sanction_schemas_represent_permanent_modes_explicitly() -> None:
    assert BanCreate().expires_at is None
    permanent_timeout = MemberUpdate(timeout_until=None, timeout_indefinite=True)
    assert permanent_timeout.timeout_indefinite is True
    assert permanent_timeout.timeout_until is None
    with pytest.raises(ValidationError):
        MemberUpdate(timeout_indefinite=None)
