from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.api.management import require_current_version


def test_settings_mutations_require_and_compare_resource_versions() -> None:
    updated_at = datetime(2026, 8, 6, 2, 30, tzinfo=UTC)
    with pytest.raises(HTTPException) as missing:
        require_current_version(updated_at, None)
    assert missing.value.status_code == 428
    assert missing.value.detail["code"] == "SETTINGS_VERSION_REQUIRED"

    with pytest.raises(HTTPException) as stale:
        require_current_version(updated_at, '"2026-08-06T02:29:59+00:00"')
    assert stale.value.status_code == 412
    assert stale.value.detail["code"] == "SETTINGS_VERSION_CONFLICT"

    require_current_version(updated_at, f'"{updated_at.isoformat()}"')
