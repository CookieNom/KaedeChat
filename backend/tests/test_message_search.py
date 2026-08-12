import base64

import pytest
from pydantic import ValidationError

from app.core.federation import FEDERATION_CAPABILITIES
from app.core.settings import Settings
from app.db.base import Base
from app.db.models import Channel
from app.search.meili import TYPO_TOLERANCE, document_id
from app.search.schemas import FederatedMessageSearchResponse, MessageSearchRequest

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_search_configuration_requires_private_key_when_enabled() -> None:
    with pytest.raises(ValidationError, match="search_master_key"):
        settings(search_enabled=True)
    configured = settings(search_enabled=True, search_master_key="s" * 32)
    assert configured.search_url == "http://meilisearch:7700"
    assert "s" * 32 not in repr(configured)
    with pytest.raises(ValidationError, match="without a path"):
        settings(
            search_enabled=True,
            search_master_key="s" * 32,
            search_url="http://meilisearch:7700/not-an-origin",
        )


def test_search_request_is_structured_and_bounded() -> None:
    request = MessageSearchRequest.model_validate(
        {
            "query": "  typo   tolerant  ",
            "scope": "channel",
            "scope_ref": "123@alpha.localhost",
            "filters": {"has": ["image"], "pinned": True},
        }
    )
    assert request.query == "typo tolerant"
    assert request.filters.has == ["image"]
    with pytest.raises(ValidationError, match="query or filter"):
        MessageSearchRequest(scope="dms")
    with pytest.raises(ValidationError, match="scope_ref"):
        MessageSearchRequest(query="hello", scope="guild")


def test_search_schema_and_federation_are_e2ee_aware() -> None:
    channels = Base.metadata.tables[Channel.__tablename__]
    assert channels.c.encryption_mode.server_default is not None
    assert "message-search/1" in FEDERATION_CAPABILITIES
    assert document_id(123, "alpha.localhost") == document_id(123, "alpha.localhost")
    assert document_id(123, "alpha.localhost") != document_id(123, "beta.localhost")


def test_search_outbox_has_no_message_content_column() -> None:
    columns = set(Base.metadata.tables["search_index_outbox"].columns.keys())
    assert columns == {
        "message_id",
        "message_domain",
        "attempts",
        "next_attempt_at",
        "locked_at",
        "last_error_code",
        "updated_at",
    }
    state_columns = set(Base.metadata.tables["search_index_state"].columns.keys())
    assert state_columns == {
        "id",
        "enabled",
        "reset_required",
        "backfill_after_id",
        "backfill_after_domain",
        "backfill_completed",
        "updated_at",
    }
    assert TYPO_TOLERANCE["minWordSizeForTypos"] == {"oneTypo": 4, "twoTypos": 8}


def test_federated_search_response_is_minimal_and_bounded() -> None:
    response = FederatedMessageSearchResponse.model_validate(
        {
            "results": [
                {
                    "message_ref": "10@alpha.localhost",
                    "channel_ref": "20@alpha.localhost",
                    "guild_ref": "30@alpha.localhost",
                    "author_ref": "40@beta.localhost",
                    "snippet": "hello",
                    "created_at": "2026-08-12T12:00:00Z",
                }
            ],
            "next_cursor": None,
            "encrypted_channel_refs": [],
        }
    )
    assert response.results[0].message_ref == "10@alpha.localhost"
    with pytest.raises(ValidationError):
        FederatedMessageSearchResponse.model_validate(
            {
                "results": [
                    {
                        "message_ref": "10@alpha.localhost",
                        "channel_ref": "20@alpha.localhost",
                        "author_ref": "40@beta.localhost",
                        "snippet": "x" * 281,
                        "created_at": "2026-08-12T12:00:00Z",
                        "attachment_url": "https://evil.example/file",
                    }
                ]
            }
        )
