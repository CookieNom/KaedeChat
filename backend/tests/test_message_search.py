import base64
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.search as search_api
import app.search.service as search_service
from app.core.federation import FEDERATION_CAPABILITIES
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.base import Base
from app.db.models import Channel, Guild, GuildMember
from app.search.meili import (
    FILTERABLE_ATTRIBUTES,
    TYPO_TOLERANCE,
    MeiliClient,
    attachment_search_projection,
    document_id,
    embed_search_projection,
    message_author_type,
    search_link_hostnames,
)
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
    with pytest.raises(ValidationError):
        MessageSearchRequest.model_validate(
            {"query": "hello", "scope": "dms", "filters": {"pinned": 1}}
        )
    with pytest.raises(ValidationError):
        MessageSearchRequest.model_validate({"query": "hello", "scope": "dms", "limit": "25"})
    assert (
        MessageSearchRequest.model_validate(
            {
                "query": "hello",
                "scope": "guild",
                "scope_ref": "123@alpha.localhost",
                "filters": {"author_type": "bot"},
            }
        ).filters.author_type
        == "bot"
    )


def test_search_request_supports_current_discord_filter_surface() -> None:
    request = MessageSearchRequest.model_validate(
        {
            "query": "release candidate",
            "scope": "guild",
            "scope_ref": "42@alpha.localhost",
            "filters": {
                "channel_ids": ["43@alpha.localhost"],
                "authors": ["5@beta.localhost"],
                "mentions": ["6@beta.localhost"],
                "mentions_role_ids": ["7@alpha.localhost"],
                "mention_everyone": False,
                "replied_to_user_ids": ["8@beta.localhost"],
                "replied_to_message_ids": ["90@alpha.localhost"],
                "has": ["image", "-video", "poll", "snapshot"],
                "embed_types": ["article", "gif"],
                "embed_providers": ["YouTube"],
                "link_hostnames": ["EXAMPLE.com."],
                "attachment_filenames": ["Release.TXT"],
                "attachment_extensions": [".TXT"],
                "max_id": "100@alpha.localhost",
                "min_id": "10@alpha.localhost",
                "pinned": True,
                "author_types": ["bot", "-webhook"],
            },
            "slop": 2,
            "include_nsfw": True,
        }
    )

    assert request.filters.link_hostnames == ["example.com"]
    assert request.filters.attachment_filenames == ["release.txt"]
    assert request.filters.attachment_extensions == ["txt"]
    assert request.sort == "newest"
    assert request.limit == 25
    with pytest.raises(ValidationError, match="cannot include and exclude"):
        MessageSearchRequest.model_validate(
            {
                "query": "release",
                "scope": "guild",
                "scope_ref": "42@alpha.localhost",
                "filters": {"has": ["poll", "-poll"]},
            }
        )
    with pytest.raises(ValidationError):
        MessageSearchRequest.model_validate(
            {
                "query": "release",
                "scope": "guild",
                "scope_ref": "42@alpha.localhost",
                "filters": {"link_hostnames": ["example.com:443"]},
            }
        )
    with pytest.raises(ValidationError):
        MessageSearchRequest.model_validate({"query": "release", "scope": "dms", "include_nsfw": 1})
    with pytest.raises(ValidationError):
        MessageSearchRequest.model_validate({"query": "release", "scope": "dms", "limit": 26})


def test_search_filter_projection_is_exact_and_nsfw_fails_closed() -> None:
    request = MessageSearchRequest.model_validate(
        {
            "query": "release",
            "scope": "guild",
            "scope_ref": "42@alpha.localhost",
            "filters": {
                "channel_ids": ["43@alpha.localhost"],
                "authors": ["5@beta.localhost"],
                "mentions": ["6@beta.localhost"],
                "mentions_role_ids": ["7@alpha.localhost"],
                "mention_everyone": True,
                "replied_to_user_ids": ["8@beta.localhost"],
                "replied_to_message_ids": ["90@alpha.localhost"],
                "has": ["image", "-video"],
                "embed_types": ["article"],
                "embed_providers": ["YouTube"],
                "link_hostnames": ["example.com"],
                "attachment_filenames": ["release.txt"],
                "attachment_extensions": ["txt"],
                "max_id": "100@alpha.localhost",
                "min_id": "10@alpha.localhost",
                "pinned": False,
                "author_types": ["bot", "-webhook"],
            },
            "include_nsfw": True,
        }
    )
    bot = SimpleNamespace(account_type="bot", age_assurance_state="unknown")
    filters = search_service.meili_filters(request, bot, settings())  # type: ignore[arg-type]

    for expected in (
        'guild_ref = "42@alpha.localhost"',
        'channel_ref = "43@alpha.localhost"',
        'author_ref = "5@beta.localhost"',
        'mention_refs = "6@beta.localhost"',
        'mention_role_refs = "7@alpha.localhost"',
        "mention_everyone = true",
        'replied_to_user_ref = "8@beta.localhost"',
        'replied_to_message_ref = "90@alpha.localhost"',
        'content_types != "video"',
        'embed_types = "article"',
        'embed_providers = "YouTube"',
        'link_hostnames = "example.com"',
        'attachment_filenames = "release.txt"',
        'attachment_extensions = "txt"',
        "pinned = false",
        'author_type != "webhook"',
        "message_id < 100",
        "message_id > 10",
    ):
        assert any(expected in item for item in filters)
    assert "nsfw = false" not in filters

    human = SimpleNamespace(account_type="human", age_assurance_state="unknown")
    assert "nsfw = false" in search_service.meili_filters(  # type: ignore[arg-type]
        request, human, settings()
    )


def test_search_document_helpers_cover_media_embed_and_links() -> None:
    image = SimpleNamespace(
        filename="Release.PNG",
        detected_content_type="image/png",
        content_type="application/octet-stream",
    )
    sound = SimpleNamespace(
        filename="voice.OGG",
        detected_content_type=None,
        content_type="audio/ogg",
    )
    content_types, filenames, extensions = attachment_search_projection(  # type: ignore[arg-type]
        [image, sound]
    )
    assert content_types == {"file", "image", "sound", "audio"}
    assert filenames == ["release.png", "voice.ogg"]
    assert extensions == ["ogg", "png"]
    embed_types, providers, hostnames = embed_search_projection(
        [
            {
                "type": "gifv",
                "provider": {"name": "Tenor"},
                "url": "https://media.example/a.gif",
                "thumbnail": {"url": "https://thumb.example/a.png"},
            }
        ]
    )
    assert embed_types == ["gif", "image"]
    assert providers == ["Tenor"]
    assert hostnames == ["media.example", "thumb.example"]
    assert search_link_hostnames("see https://EXAMPLE.com/a?q=1.") == ["example.com"]
    assert "message_id" in FILTERABLE_ATTRIBUTES
    assert "dm_authority" in FILTERABLE_ATTRIBUTES


@pytest.mark.asyncio
async def test_local_search_cursor_advances_only_past_consumed_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = cast(Any, SimpleNamespace(id=7, origin_domain="alpha.localhost"))
    request = MessageSearchRequest.model_validate(
        {
            "query": "release",
            "scope": "guild",
            "scope_ref": "42@alpha.localhost",
        }
    )
    candidates = [
        search_service.SearchCandidate(f"{identifier}@alpha.localhost", 0.5)
        for identifier in (10, 11, 12)
    ]
    monkeypatch.setattr(search_service, "authorize_scope", AsyncMock(return_value=(None, [])))
    monkeypatch.setattr(
        search_service,
        "candidate_refs",
        AsyncMock(return_value=(candidates, 0, 4)),
    )
    monkeypatch.setattr(
        search_service,
        "hydrate_results",
        AsyncMock(return_value=([{"message": {"id": "11"}}], 2)),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=False),
    )

    page = await search_service.local_search(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        settings(search_enabled=True, search_master_key="s" * 32),
        actor,
        request,
    )

    assert page["next_cursor"] == search_service.encode_cursor(2)


class SearchCursorRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_kwargs: object) -> bool:
        self.values[key] = value
        return True


def internal_search_result(
    identifier: int,
    created_at: str,
    score: float,
    cursor_after: str,
    *,
    authority: str,
) -> dict[str, object]:
    return {
        "message": {
            "id": str(identifier),
            "origin_domain": authority,
            "created_at": created_at,
        },
        "channel": {"id": "20", "origin_domain": authority},
        "guild": None,
        "snippet": f"message {identifier}",
        "_search_score": score,
        "_search_cursor_after": cursor_after,
    }


@pytest.mark.asyncio
async def test_account_dm_search_fanout_merges_without_skipping_source_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = cast(
        Any,
        SimpleNamespace(id=7, origin_domain="alpha.localhost", account_type="human"),
    )
    request = MessageSearchRequest.model_validate(
        {"query": "release", "scope": "dms", "limit": 3, "sort": "newest"}
    )
    authorities = ["alpha.localhost", "a.example", "b.example"]
    monkeypatch.setattr(
        search_service,
        "active_dm_search_authorities",
        AsyncMock(return_value=authorities),
    )
    local_results = [
        internal_search_result(
            10,
            "2026-08-28T10:00:00+00:00",
            0.9,
            search_service.encode_cursor(1),
            authority="alpha.localhost",
        ),
        internal_search_result(
            7,
            "2026-08-28T07:00:00+00:00",
            0.6,
            search_service.encode_cursor(2),
            authority="alpha.localhost",
        ),
    ]
    monkeypatch.setattr(
        search_service,
        "local_search",
        AsyncMock(
            return_value={
                "results": local_results,
                "next_cursor": search_service.encode_cursor(2),
                "encrypted_channel_refs": ["90@alpha.localhost"],
                "indexing": False,
            }
        ),
    )
    monkeypatch.setattr(
        search_service,
        "search_authority_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(search_service, "async_sessionmaker", lambda *_args, **_kwargs: object())

    def wire(authority: str, ids: tuple[int, ...]) -> FederatedMessageSearchResponse:
        return FederatedMessageSearchResponse.model_validate(
            {
                "results": [
                    {
                        "message_ref": f"{identifier}@{authority}",
                        "channel_ref": f"20@{authority}",
                        "guild_ref": None,
                        "author_ref": f"8@{authority}",
                        "snippet": f"message {identifier}",
                        "created_at": f"2026-08-28T0{identifier}:00:00Z",
                        "ranking_score": 0.5,
                        "cursor_after": search_service.encode_cursor(position),
                    }
                    for position, identifier in enumerate(ids, start=1)
                ],
                "next_cursor": None,
                "encrypted_channel_refs": [],
                "indexing": False,
            }
        )

    remote_pages = {
        "a.example": wire("a.example", (9, 8)),
        "b.example": wire("b.example", (6,)),
    }

    async def remote_wire(
        _factory: object,
        _settings: Settings,
        _actor: object,
        authority: str,
        _request: MessageSearchRequest,
    ) -> FederatedMessageSearchResponse:
        return remote_pages[authority]

    monkeypatch.setattr(search_service, "remote_search_wire_response", remote_wire)

    async def materialize(
        _session: object,
        _redis: object,
        _settings: Settings,
        _actor: object,
        _request: MessageSearchRequest,
        response: FederatedMessageSearchResponse,
        *,
        expected_authority: str | None = None,
    ) -> list[dict[str, object]]:
        assert expected_authority is not None
        return [
            internal_search_result(
                int(item.message_ref.id),
                item.created_at.isoformat(),
                item.ranking_score,
                item.cursor_after,
                authority=expected_authority,
            )
            for item in response.results
        ]

    monkeypatch.setattr(search_service, "materialize_federated_results", materialize)
    redis = SearchCursorRedis()
    session = cast(Any, SimpleNamespace(bind=object()))
    configured = settings(search_enabled=True, search_master_key="s" * 32)

    page = await search_service.search_account_dms(
        session,
        cast(Any, redis),
        configured,
        actor,
        request,
    )

    assert [item["message"]["id"] for item in page["results"]] == ["10", "9", "8"]  # type: ignore[index]
    assert all(
        "_search_score" not in item and "_search_cursor_after" not in item
        for item in page["results"]  # type: ignore[union-attr]
    )
    assert page["encrypted_channel_refs"] == ["90@alpha.localhost"]
    assert isinstance(page["next_cursor"], str)
    stored = json.loads(next(iter(redis.values.values())))
    assert stored["authorities"]["alpha.localhost"] == {
        "cursor": search_service.encode_cursor(1),
        "exhausted": False,
        "terminal_status": None,
    }
    assert stored["authorities"]["a.example"]["exhausted"] is True
    assert stored["authorities"]["b.example"]["cursor"] is None

    tampered = request.model_copy(update={"query": "different", "cursor": page["next_cursor"]})
    with pytest.raises(HTTPException) as exc:
        await search_service.load_dm_search_cursor(
            cast(Any, redis),
            tampered,
            actor,
            None,
        )
    assert exc.value.detail["code"] == "INVALID_SEARCH_CURSOR"


@pytest.mark.asyncio
async def test_search_backend_rejects_custom_slop_and_malformed_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(search_enabled=True, search_master_key="s" * 32)
    actor = SimpleNamespace(account_type="bot", age_assurance_state="unknown")
    request = MessageSearchRequest.model_validate(
        {
            "query": "release",
            "scope": "guild",
            "scope_ref": "42@alpha.localhost",
            "slop": 3,
        }
    )
    with pytest.raises(HTTPException) as exc:
        await search_service.candidate_refs(configured, request, actor)  # type: ignore[arg-type]
    assert exc.value.detail["code"] == "SEARCH_SLOP_UNSUPPORTED"

    request.slop = 2
    monkeypatch.setattr(
        MeiliClient,
        "search",
        AsyncMock(return_value={"hits": [{"message_ref": True}], "estimatedTotalHits": 1}),
    )
    with pytest.raises(search_service.SearchUnavailable):
        await search_service.candidate_refs(configured, request, actor)  # type: ignore[arg-type]


def test_search_author_type_distinguishes_bots_and_webhooks() -> None:
    human = SimpleNamespace(account_type="human")
    bot = SimpleNamespace(account_type="bot")
    ordinary = SimpleNamespace(webhook_id=None)
    webhook = SimpleNamespace(webhook_id=99)

    assert message_author_type(ordinary, human) == "user"  # type: ignore[arg-type]
    assert message_author_type(ordinary, bot) == "bot"  # type: ignore[arg-type]
    assert message_author_type(webhook, bot) == "webhook"  # type: ignore[arg-type]


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
                    "ranking_score": 0.75,
                    "cursor_after": search_service.encode_cursor(1),
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
                        "ranking_score": 0.75,
                        "cursor_after": search_service.encode_cursor(1),
                        "attachment_url": "https://evil.example/file",
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_reset_missing_search_index_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted = False

    class Response:
        status_code = 404

        def raise_for_status(self) -> None:
            raise AssertionError("a missing index must not be treated as an error")

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _path: str) -> Response:
            return Response()

        async def delete(self, _path: str) -> Response:
            nonlocal deleted
            deleted = True
            return Response()

    monkeypatch.setattr("app.search.meili.httpx.AsyncClient", Client)
    await MeiliClient(settings(search_enabled=True, search_master_key="s" * 32)).reset_index()
    assert deleted is False


@pytest.mark.asyncio
async def test_guild_e2ee_search_disclosure_only_lists_visible_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=42, origin_domain="alpha.localhost", unavailable=False)
    member = SimpleNamespace()
    hidden = SimpleNamespace(id=50, origin_domain="alpha.localhost")
    visible = SimpleNamespace(id=51, origin_domain="alpha.localhost")
    actor = SimpleNamespace(id=7, origin_domain="alpha.localhost")

    async def get(model: object, _key: object) -> object | None:
        if model is Guild:
            return guild
        if model is GuildMember:
            return member
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalars=AsyncMock(return_value=[hidden, visible]),
    )
    permissions = AsyncMock(side_effect=[Permission(0), Permission.VIEW_CHANNEL])
    monkeypatch.setattr(search_service, "get_permissions", permissions)

    authority, disabled = await search_service.authorize_scope(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        settings(),
        actor,  # type: ignore[arg-type]
        MessageSearchRequest.model_validate(
            {
                "query": "incident",
                "scope": "guild",
                "scope_ref": "42@alpha.localhost",
            }
        ),
    )

    assert authority == "alpha.localhost"
    assert disabled == ["51@alpha.localhost"]
    assert permissions.await_count == 2


@pytest.mark.asyncio
async def test_bot_guild_search_requires_content_intent_and_redacts_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=42, origin_domain="guild.example")
    installation = SimpleNamespace(
        id=88,
        granted_scopes=["messages.history", "messages.content", "attachments.read"],
        granted_intents=["message_content"],
        guild_domain="guild.example",
        channel_restrictions=[],
        grant_revision=3,
        e2ee_mode="optional",
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="apps.example"),
        application=SimpleNamespace(id=8, origin_domain="apps.example"),
        scopes=frozenset({"messages.history", "messages.content"}),
        intents=frozenset({"message_content"}),
        require_scope=Mock(),
    )
    authorize = AsyncMock(return_value=(guild, installation))
    search = AsyncMock(
        return_value={
            "results": [
                {
                    "message": {
                        "id": "9",
                        "origin_domain": "guild.example",
                        "channel_id": "4",
                        "channel_domain": "guild.example",
                        "content": "deploy complete",
                        "e2ee": None,
                        "attachments": [{"id": "5", "filename": "report.txt"}],
                    },
                    "channel": {
                        "id": "4",
                        "origin_domain": "guild.example",
                        "guild_id": "42",
                        "guild_domain": "guild.example",
                    },
                    "guild": {"id": "42", "origin_domain": "guild.example"},
                    "snippet": "deploy complete",
                }
            ],
            "next_cursor": None,
            "encrypted_channel_refs": [],
            "coverage": {"local": "complete", "authority": "not_needed"},
            "indexing": False,
        }
    )
    monkeypatch.setattr(search_api, "installation_for_guild", authorize)
    monkeypatch.setattr(search_api, "search_with_authority", search)
    body = MessageSearchRequest.model_validate(
        {
            "query": "deploy",
            "scope": "guild",
            "scope_ref": "42@guild.example",
            "filters": {"author_type": "bot"},
        }
    )

    result = await search_api.bot_search_guild_messages(
        EntityRef("42@guild.example"),
        body,
        principal,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        settings(
            domain="guild.example",
            search_enabled=True,
            search_master_key="s" * 32,
        ),
    )

    authorize.assert_awaited_once()
    assert authorize.await_args.args[-1] == "messages.history"
    principal.require_scope.assert_called_once_with("messages.content")
    search.assert_awaited_once()
    message = result["results"][0]["message"]  # type: ignore[index]
    assert message["content"] == "deploy complete"
    assert message["attachments"] == []
    assert message["attachments_unavailable"] is True
    assert message["bot_installation_id"] == "88"
    assert result["results"][0]["channel"]["bot_installation_id"] == "88"  # type: ignore[index]
    assert result["results"][0]["guild"]["capability_revision"] == "3"  # type: ignore[index]

    principal.intents = frozenset()
    with pytest.raises(HTTPException) as exc:
        await search_api.bot_search_guild_messages(
            EntityRef("42@guild.example"),
            body,
            principal,  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            settings(
                domain="guild.example",
                search_enabled=True,
                search_master_key="s" * 32,
            ),
        )
    assert exc.value.detail == {"code": "BOT_INTENT_REQUIRED", "intent": "message_content"}
