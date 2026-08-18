from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.channels as channels_api
import app.api.federation as federation_api
import app.api.media as media_api
from app.api.channels import (
    dm_delivery_statuses,
    publish_replica_guild_status,
    raise_proxy_rejection,
)
from app.api.federation import (
    _dm_history_media_scope,
    _federation_media_attachment,
    federation_dm_history_page,
)
from app.api.media import authorized_dm_history_media
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.core.types import EntityRef
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Guild,
    Message,
    RemoteMediaCache,
    RemoteMediaTombstone,
    User,
)
from app.federation.dm_history import (
    dm_history_page_is_complete,
    history_media_capability_status,
    history_media_path,
    merge_dm_history_messages,
    validate_dm_history_page,
    verify_history_media_capability,
)
from app.federation.dm_storage import (
    _eligible_replica_messages,
    _minimal_eviction_prefix,
    _projected_quota_deficit,
    dm_authority_history_available,
    dm_history_metadata,
    opaque_dm_history_ref_allowed,
    sweep_federated_dm_replica_cache,
)
from app.federation.network import FederationNetworkError
from app.federation.security import FederationPrincipal

from .test_settings import settings


def snowflake_at(value: datetime, sequence: int = 0) -> int:
    timestamp = int(value.timestamp() * 1000) - EPOCH_MS
    return (timestamp << (WORKER_BITS + SEQUENCE_BITS)) | sequence


def profile(user_id: int, domain: str, username: str) -> dict[str, object]:
    return {
        "id": str(user_id),
        "origin_domain": domain,
        "username": username,
        "display_name": username.title(),
        "avatar_hash": None,
        "banner_hash": None,
        "bio": None,
        "custom_status": None,
        "profile_version": 1,
    }


def message(
    *,
    message_id: int,
    origin: str,
    channel_ref: tuple[int, str],
    author_id: int,
    username: str,
    created_at: datetime,
) -> dict[str, object]:
    return {
        "id": str(message_id),
        "origin_domain": origin,
        "channel_id": str(channel_ref[0]),
        "channel_domain": channel_ref[1],
        "author_id": str(author_id),
        "author_domain": origin,
        "author": profile(author_id, origin, username),
        "content": "hello",
        "e2ee": None,
        "message_type": 0,
        "flags": 0,
        "client_nonce": None,
        "referenced_message_id": None,
        "referenced_message_domain": None,
        "mention_user_refs": [],
        "attachments": [],
        "edited_at": None,
        "deleted_at": None,
        "created_at": created_at.isoformat(),
    }


def test_history_page_ignores_authority_local_body_and_marks_completion() -> None:
    configured = settings()
    authority = "authority.example"
    local = configured.domain
    now = datetime.now(UTC) - timedelta(minutes=1)
    newest = snowflake_at(now, 2)
    older = snowflake_at(now - timedelta(seconds=1), 1)
    before = (snowflake_at(now + timedelta(seconds=1)), authority)
    conversation_ref = (snowflake_at(now - timedelta(days=1)), authority)
    local_user_id = snowflake_at(now - timedelta(days=2))
    remote_user_id = snowflake_at(now - timedelta(days=3))
    local_message = message(
        message_id=newest,
        origin=local,
        channel_ref=conversation_ref,
        author_id=local_user_id,
        username="spoofed_local",
        created_at=now,
    )
    remote_message = message(
        message_id=older,
        origin=authority,
        channel_ref=conversation_ref,
        author_id=remote_user_id,
        username="remote",
        created_at=now - timedelta(seconds=1),
    )
    body = {
        "conversation_id": str(conversation_ref[0]),
        "conversation_domain": conversation_ref[1],
        "messages": [local_message, remote_message],
        "complete": True,
        "next_before": None,
    }
    page = validate_dm_history_page(
        body,
        settings=configured,
        conversation_ref=conversation_ref,
        authority_domain=authority,
        participant_refs={(local_user_id, local), (remote_user_id, authority)},
        trusted_profiles={},
        before=before,
        limit=50,
    )

    assert len(page.messages) == 1
    assert page.messages[0]["origin_domain"] == authority
    assert page.ignored_local_refs == {(newest, local)}
    assert page.messages[0]["history_page_complete"] is True


def test_history_merge_preserves_local_body_and_attachments() -> None:
    reference = {"id": "10", "origin_domain": "local.example"}
    remote = {
        **reference,
        "content": "forged",
        "attachments": [{"id": "99", "filename": "forged.png"}],
    }
    local = {
        **reference,
        "content": "trusted",
        "attachments": [{"id": "20", "filename": "trusted.png"}],
    }

    merged = merge_dm_history_messages([remote], [local], limit=50)

    assert merged == [local]


@pytest.mark.asyncio
async def test_authority_dm_history_filters_controls_before_spanning_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings()
    conversation = DMConversation(
        id=100,
        origin_domain=configured.domain,
        authority_domain=configured.domain,
        pair_key="a" * 64,
        type="direct",
    )
    now = datetime.now(UTC)
    messages = [
        Message(
            id=identifier,
            origin_domain=configured.domain,
            channel_id=conversation.id,
            channel_domain=conversation.origin_domain,
            author_id=200,
            author_domain=configured.domain,
            content=f"application-{identifier}",
            created_at=now - timedelta(seconds=index),
        )
        # Durable control rows at 499 and 497 must not consume the page. The
        # application page therefore spans those gaps and still returns 3
        # rows for a requested limit of 2 (two selected plus has-more probe).
        for index, identifier in enumerate((500, 498, 496))
    ]
    author = User(
        id=200,
        origin_domain=configured.domain,
        is_local=True,
        username="author",
        display_name="Author",
        profile_version=1,
        e2ee_device_generation=0,
        profile_resolved=True,
        account_type="human",
    )
    captured: list[object] = []
    scalar_batches: list[list[object]] = [messages, [author], []]

    async def get(model: object, key: object) -> object | None:
        if model is DMConversation and key == (100, configured.domain):
            return conversation
        return None

    async def scalars(statement: object) -> list[object]:
        captured.append(statement)
        return scalar_batches.pop(0)

    session = cast(
        AsyncSession,
        SimpleNamespace(
            get=get,
            scalar=AsyncMock(return_value=200),
            scalars=scalars,
        ),
    )
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )

    result = await federation_dm_history_page(
        conversation_id=cast(Any, 100),
        before_id=None,
        before_domain=None,
        limit=2,
        requester_id=None,
        requester_domain=None,
        principal=FederationPrincipal("requester.example", "ed25519:test"),
        session=session,
        redis=cast(Any, object()),
        settings=configured,
    )

    sql = str(
        captured[0].compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    assert "e2ee_control_records" in sql
    assert "NOT (EXISTS" in sql
    assert [item["id"] for item in cast(list[dict[str, object]], result["messages"])] == [
        "500",
        "498",
    ]
    assert result["next_before"] == {
        "id": "498",
        "origin_domain": configured.domain,
    }
    assert result["complete"] is False


def test_minimal_eviction_prefix_keeps_every_recent_row_that_fits() -> None:
    candidates = [
        Message(id=identifier, origin_domain="authority.example") for identifier in (1, 2, 3)
    ]
    charges = {
        (1, "authority.example"): 4_096,
        (2, "authority.example"): 8_192,
        (3, "authority.example"): 16_384,
    }

    selected, selected_bytes = _minimal_eviction_prefix(
        candidates,
        charges,
        message_deficit=1,
        byte_deficit=1,
    )
    assert [message.id for message in selected] == [1]
    assert selected_bytes == 4_096

    selected, selected_bytes = _minimal_eviction_prefix(
        candidates,
        charges,
        message_deficit=1,
        byte_deficit=4_097,
    )
    assert [message.id for message in selected] == [1, 2]
    assert selected_bytes == 12_288


def test_projected_quota_deficit_charges_incoming_message_once() -> None:
    assert _projected_quota_deficit(9, 1, 10) == 0
    assert _projected_quota_deficit(10, 1, 10) == 1


def test_history_completion_requires_both_sources_and_all_merged_rows_exhausted() -> None:
    newest = {"id": "10", "origin_domain": "local.example"}
    older_remote = {"id": "9", "origin_domain": "authority.example"}

    assert dm_history_page_is_complete(
        remote_complete=True,
        merged_messages=[newest, older_remote],
        remote_messages=[older_remote],
        local_has_more=False,
    )
    assert not dm_history_page_is_complete(
        remote_complete=True,
        merged_messages=[newest],
        remote_messages=[older_remote],
        local_has_more=False,
    )
    assert not dm_history_page_is_complete(
        remote_complete=True,
        merged_messages=[newest, older_remote],
        remote_messages=[older_remote],
        local_has_more=True,
    )


@pytest.mark.asyncio
async def test_eviction_query_protects_local_authored_and_actual_newest_rows() -> None:
    captured: list[object] = []

    async def scalars(statement: object) -> list[Message]:
        captured.append(statement)
        return []

    session = cast(AsyncSession, SimpleNamespace(scalars=scalars))
    configured = settings()
    await _eligible_replica_messages(
        session,
        configured,
        conversation=None,
        authority_domain="authority.example",
        limit=10,
    )

    sql = str(
        captured[0].compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    assert "messages.origin_domain !=" in sql
    assert "EXISTS (SELECT" in sql
    assert "newer_dm_message.id, newer_dm_message.origin_domain) >" in sql
    assert "NOT (EXISTS" not in sql.partition("newer_dm_message")[0][-32:]
    assert "pins" in sql


@pytest.mark.asyncio
async def test_sweep_selects_only_over_target_capable_replicas() -> None:
    captured: list[object] = []

    class EmptyResult:
        def tuples(self) -> list[tuple[int, str, str]]:
            return []

    async def execute(statement: object) -> EmptyResult:
        captured.append(statement)
        return EmptyResult()

    session = cast(AsyncSession, SimpleNamespace(execute=execute))
    await sweep_federated_dm_replica_cache(session, settings())

    sql = str(
        captured[0].compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    assert "federated_dm_row_charges.row_domain !=" in sql
    assert "instances.capabilities" in sql
    assert "federated_dm_max_messages_per_authority" not in sql
    assert "anon_1.message_rows >" in sql
    assert "anon_1.total_bytes >" in sql
    assert "LIMIT" in sql


def test_history_page_rejects_oversized_or_nonadvancing_peer_rows() -> None:
    configured = settings()
    authority = "authority.example"
    now = datetime.now(UTC) - timedelta(minutes=1)
    conversation_ref = (snowflake_at(now - timedelta(days=1)), authority)
    author_id = snowflake_at(now - timedelta(days=2))
    identifier = snowflake_at(now)
    item = message(
        message_id=identifier,
        origin=authority,
        channel_ref=conversation_ref,
        author_id=author_id,
        username="remote",
        created_at=now,
    )
    item["content"] = "x" * 4_001
    body = {
        "conversation_id": str(conversation_ref[0]),
        "conversation_domain": conversation_ref[1],
        "messages": [item],
        "complete": True,
        "next_before": None,
    }
    with pytest.raises(FederationNetworkError, match="content"):
        validate_dm_history_page(
            body,
            settings=configured,
            conversation_ref=conversation_ref,
            authority_domain=authority,
            participant_refs={(author_id, authority)},
            trusted_profiles={},
            before=(identifier + 1, authority),
            limit=50,
        )

    item["content"] = "valid"
    body["messages"] = [item, dict(item)]
    with pytest.raises(FederationNetworkError, match="mismatched references"):
        validate_dm_history_page(
            body,
            settings=configured,
            conversation_ref=conversation_ref,
            authority_domain=authority,
            participant_refs={(author_id, authority)},
            trusted_profiles={},
            before=(identifier + 1, authority),
            limit=50,
        )


@pytest.mark.asyncio
async def test_rolling_metadata_requires_authority_history_capability() -> None:
    configured = settings()
    conversation = DMConversation(
        id=10,
        origin_domain="authority.example",
        authority_domain="authority.example",
        pair_key="a" * 64,
        history_truncated=True,
        history_cache_start_id=20,
        history_cache_start_domain="authority.example",
    )
    old_session = cast(
        AsyncSession,
        SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(capabilities=[]))),
    )
    new_session = cast(
        AsyncSession,
        SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(capabilities=["dm-history-page/1"]))
        ),
    )
    assert not await dm_authority_history_available(
        old_session, conversation, local_domain=configured.domain
    )
    assert await dm_authority_history_available(
        new_session, conversation, local_domain=configured.domain
    )
    old_metadata = dm_history_metadata(
        conversation, local_domain=configured.domain, remote_available=False
    )
    new_metadata = dm_history_metadata(
        conversation, local_domain=configured.domain, remote_available=True
    )
    assert old_metadata["history_remote_available"] is False
    assert new_metadata["history_remote_available"] is True


def test_opaque_reply_reference_is_limited_to_capable_evicted_remote_prefix() -> None:
    conversation = DMConversation(
        id=10,
        origin_domain="authority.example",
        authority_domain="authority.example",
        pair_key="a" * 64,
        history_truncated=True,
        history_truncated_before_id=100,
        history_truncated_before_domain="authority.example",
    )
    arguments = {
        "participant_domains": {"local.example", "authority.example"},
        "local_domain": "local.example",
        "remote_available": True,
    }

    assert opaque_dm_history_ref_allowed(
        conversation,
        (99, "authority.example"),
        **arguments,
    )
    assert not opaque_dm_history_ref_allowed(
        conversation,
        (99, "local.example"),
        **arguments,
    )
    assert not opaque_dm_history_ref_allowed(
        conversation,
        (101, "authority.example"),
        **arguments,
    )
    assert not opaque_dm_history_ref_allowed(
        conversation,
        (99, "unrelated.example"),
        **arguments,
    )
    assert not opaque_dm_history_ref_allowed(
        conversation,
        (99, "authority.example"),
        **{**arguments, "remote_available": False},
    )


def test_rolling_cache_migration_preserves_reference_integrity_outside_opaque_prefix() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/b72c9e4a1f63_federated_dm_rolling_cache.py"
    ).read_text()

    assert "CREATE TRIGGER trg_messages_reply_reference" in migration
    assert "CREATE TRIGGER trg_read_states_last_message_reference" in migration
    assert "CREATE CONSTRAINT TRIGGER trg_messages_delete_reference" in migration
    assert "CREATE CONSTRAINT TRIGGER trg_dm_conversations_history_boundary" in migration
    assert "authority.capabilities @> '[\"dm-history-page/1\"]'::jsonb" in migration
    assert "AND p_message_domain <>" in migration
    assert "clear only dangling references before restoring the legacy FKs" in migration


def test_history_media_capability_is_short_lived_and_bound_to_every_reference() -> None:
    configured = settings()
    now = datetime.now(UTC)
    path = history_media_path(
        configured,
        conversation_ref=(1, "authority.example"),
        message_ref=(2, "authority.example"),
        attachment_ref=(3, "authority.example"),
        now=now,
    )
    query = path.split("?", 1)[1]
    values = dict(item.split("=", 1) for item in query.split("&"))
    assert verify_history_media_capability(
        configured,
        conversation_ref=(1, "authority.example"),
        message_ref=(2, "authority.example"),
        attachment_ref=(3, "authority.example"),
        variant="original",
        expires=int(values["expires"]),
        token=values["token"],
        now=now,
    )
    assert not verify_history_media_capability(
        configured,
        conversation_ref=(1, "authority.example"),
        message_ref=(2, "authority.example"),
        attachment_ref=(4, "authority.example"),
        variant="original",
        expires=int(values["expires"]),
        token=values["token"],
        now=now,
    )
    expired_at = now + timedelta(minutes=16)
    tampered_token = f"{values['token'][:-1]}{'A' if values['token'][-1] != 'A' else 'B'}"
    assert (
        history_media_capability_status(
            configured,
            conversation_ref=(1, "authority.example"),
            message_ref=(2, "authority.example"),
            attachment_ref=(3, "authority.example"),
            variant="original",
            expires=int(values["expires"]),
            token=values["token"],
            now=expired_at,
        )
        == "renewable"
    )
    assert (
        history_media_capability_status(
            configured,
            conversation_ref=(1, "authority.example"),
            message_ref=(2, "authority.example"),
            attachment_ref=(3, "authority.example"),
            variant="original",
            expires=int(values["expires"]),
            token=tampered_token,
            now=expired_at,
        )
        == "invalid"
    )


@pytest.mark.asyncio
async def test_expired_history_media_is_reauthorized_and_returns_fresh_scoped_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings()
    old_path = history_media_path(
        configured,
        conversation_ref=(1, "authority.example"),
        message_ref=(2, "authority.example"),
        attachment_ref=(3, "authority.example"),
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    values = dict(item.split("=", 1) for item in old_path.split("?", 1)[1].split("&"))
    conversation = DMConversation(
        id=1,
        origin_domain="authority.example",
        authority_domain="authority.example",
        pair_key="a" * 64,
        history_truncated=True,
    )
    participant = DMParticipant(
        conversation_id=1,
        conversation_domain="authority.example",
        user_id=9,
        user_domain=configured.domain,
    )
    cached = RemoteMediaCache(
        origin_domain="authority.example",
        attachment_id=3,
        variant="original",
        object_key="remote/authority.example/3/original",
        size=128,
        content_type="image/png",
        scan_status="clean",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is DMConversation and key == (1, "authority.example"):
            return conversation
        if model is DMParticipant:
            return participant
        if model is RemoteMediaTombstone:
            return None
        if model is RemoteMediaCache:
            return cached
        return None

    session = cast(
        AsyncSession,
        SimpleNamespace(
            get=get,
            scalar=AsyncMock(return_value=None),
            commit=AsyncMock(),
        ),
    )
    authorization = AsyncMock(
        return_value=httpx.Response(
            204,
            request=httpx.Request("GET", "https://authority.example/authorize"),
        )
    )
    monkeypatch.setattr(media_api, "signed_request", authorization)
    monkeypatch.setattr(
        media_api.S3Storage,
        "presign",
        lambda *_args, **_kwargs: "https://objects.example/signed",
    )
    auth = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=9, origin_domain=configured.domain),
        ),
    )

    response = await authorized_dm_history_media(
        EntityRef("1@authority.example"),
        EntityRef("2@authority.example"),
        EntityRef("3@authority.example"),
        Response(),
        expires=int(values["expires"]),
        token=values["token"],
        variant="original",
        auth=auth,
        session=session,
        redis=cast(Any, object()),
        settings=configured,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://objects.example/signed"
    fresh_path = response.headers["content-location"]
    assert fresh_path != old_path
    fresh_values = dict(item.split("=", 1) for item in fresh_path.split("?", 1)[1].split("&"))
    assert verify_history_media_capability(
        configured,
        conversation_ref=(1, "authority.example"),
        message_ref=(2, "authority.example"),
        attachment_ref=(3, "authority.example"),
        variant="original",
        expires=int(fresh_values["expires"]),
        token=fresh_values["token"],
    )
    assert authorization.await_args.kwargs["query"] == {
        "conversation_id": "1",
        "conversation_domain": "authority.example",
        "message_id": "2",
        "message_domain": "authority.example",
        "requester_id": "9",
        "requester_domain": configured.domain,
    }


@pytest.mark.asyncio
async def test_history_media_origin_rejects_cross_conversation_or_message_scope() -> None:
    configured = settings()
    attachment = Attachment(
        id=7,
        origin_domain=configured.domain,
        uploader_id=1,
        uploader_domain=configured.domain,
        filename="image.png",
        content_type="image/png",
        size=128,
        purpose="attachment",
        scan_status="clean",
        message_id=20,
        message_domain=configured.domain,
    )
    message_row = Message(
        id=20,
        origin_domain=configured.domain,
        channel_id=30,
        channel_domain="authority.example",
        author_id=1,
        author_domain=configured.domain,
        content="image",
    )
    channel = Channel(
        id=30,
        origin_domain="authority.example",
        type=1,
        name=None,
        guild_id=None,
        guild_domain=None,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Attachment and key == (7, configured.domain):
            return attachment
        if model is Message and key == (20, configured.domain):
            return message_row
        if model is Channel and key == (30, "authority.example"):
            return channel
        return None

    session = cast(
        AsyncSession,
        SimpleNamespace(get=get, scalar=AsyncMock(return_value=1)),
    )
    principal = FederationPrincipal(origin="requester.example", key_id="ed25519:test")
    result = await _federation_media_attachment(
        session,
        cast(Any, object()),
        configured,
        principal,
        7,
        "original",
        expected_conversation=(30, "authority.example"),
        expected_message=(20, configured.domain),
    )
    assert result is attachment

    for conversation_ref, message_ref in (
        ((31, "authority.example"), (20, configured.domain)),
        ((30, "authority.example"), (21, configured.domain)),
    ):
        with pytest.raises(HTTPException) as raised:
            await _federation_media_attachment(
                session,
                cast(Any, object()),
                configured,
                principal,
                7,
                "original",
                expected_conversation=conversation_ref,
                expected_message=message_ref,
            )
        assert raised.value.status_code == 404


def test_history_media_scope_requires_all_composite_reference_parts() -> None:
    assert (
        _dm_history_media_scope(
            conversation_id=None,
            conversation_domain=None,
            message_id=None,
            message_domain=None,
        )
        is None
    )
    with pytest.raises(HTTPException):
        _dm_history_media_scope(
            conversation_id=1,
            conversation_domain="authority.example",
            message_id=None,
            message_domain=None,
        )


def test_remote_guild_message_and_pin_proxies_preserve_typed_507_errors() -> None:
    response = httpx.Response(
        507,
        json={
            "detail": {
                "code": "KAED_FED_REPLICA_QUOTA_EXCEEDED",
                "scope": "guild",
                "resource": "bytes",
                "used": 11,
                "limit": 10,
            }
        },
        request=httpx.Request("POST", "https://authority.example/proxy"),
    )
    for handled in ({403, 404, 429, 507}, {400, 403, 404, 409, 429, 507}):
        with pytest.raises(HTTPException) as raised:
            raise_proxy_rejection(response, handled)
        assert raised.value.status_code == 507
        assert raised.value.detail["code"] == "KAED_FED_REPLICA_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_live_replica_quota_status_publishes_only_safe_guild_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=1,
        origin_domain="authority.example",
        name="Remote",
        owner_id=2,
        owner_domain="authority.example",
        sync_status="quota_paused",
        sync_error_code="KAED_FED_REPLICA_QUOTA_EXCEEDED",
        sync_error="internal database details",
    )
    publish = AsyncMock()
    monkeypatch.setattr(channels_api, "publish_dispatch", publish)

    await publish_replica_guild_status(cast(Any, object()), guild)

    payload = publish.await_args.args[3]
    assert payload["sync_status"] == "quota_paused"
    assert payload["sync_error_code"] == "KAED_FED_REPLICA_QUOTA_EXCEEDED"
    assert "sync_error" not in payload


@pytest.mark.asyncio
async def test_retrying_dm_delivery_survives_message_page_reload() -> None:
    configured = settings()
    channel = Channel(id=30, origin_domain=configured.domain, type=1)
    message_row = Message(
        id=20,
        origin_domain=configured.domain,
        channel_id=30,
        channel_domain=configured.domain,
        author_id=1,
        author_domain=configured.domain,
        content="pending",
    )
    envelope = {
        "content": {
            "message": {
                "id": "20",
                "origin_domain": configured.domain,
                "channel_id": "30",
            }
        }
    }
    result = SimpleNamespace(
        all=lambda: [
            (
                envelope,
                "retry",
                "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
            )
        ]
    )
    session = cast(AsyncSession, SimpleNamespace(execute=AsyncMock(return_value=result)))

    statuses = await dm_delivery_statuses(
        session,
        configured,
        channel,
        [message_row],
    )

    assert statuses[(20, configured.domain)] == (
        "retrying",
        "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
    )
