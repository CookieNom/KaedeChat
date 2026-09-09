from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Response
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
from app.federation.schemas import RemoteUserProfile
from app.federation.security import FederationPrincipal

from .test_settings import settings


def snowflake_at(value: datetime, sequence: int = 0) -> int:
    timestamp = int(value.timestamp() * 1000) - EPOCH_MS
    return (timestamp << (WORKER_BITS + SEQUENCE_BITS)) | sequence


def profile(
    user_id: int,
    domain: str,
    username: str,
    *,
    account_type: str = "human",
) -> dict[str, object]:
    return {
        "id": str(user_id),
        "origin_domain": domain,
        "account_type": account_type,
        "username": username,
        "display_name": username.title(),
        "avatar_hash": None,
        "banner_hash": None,
        "bio": None,
        "custom_status": None,
        "profile_version": 1,
        "e2ee_device_generation": 4,
    }


def message(
    *,
    message_id: int,
    origin: str,
    channel_ref: tuple[int, str],
    author_id: int,
    username: str,
    created_at: datetime,
    account_type: str = "human",
) -> dict[str, object]:
    return {
        "id": str(message_id),
        "origin_domain": origin,
        "channel_id": str(channel_ref[0]),
        "channel_domain": channel_ref[1],
        "author_id": str(author_id),
        "author_domain": origin,
        "author": profile(author_id, origin, username, account_type=account_type),
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


@pytest.mark.parametrize("account_type", ["human", "bot"])
def test_history_page_ignores_authority_local_body_and_marks_completion(
    account_type: str,
) -> None:
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
        account_type=account_type,
    )
    remote_message["reaction_counts"] = {
        "❤️": 1,
        "❤": 2,
        "<:lantern:75512661369970689@HOME.EXAMPLE.>": 3,
    }
    remote_message["reacted_emoji"] = [
        "❤️",
        "<:lantern:75512661369970689@HOME.EXAMPLE.>",
    ]
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
    rendered_author = cast(dict[str, object], page.messages[0]["author"])
    assert rendered_author["profile_version"] == "1"
    assert rendered_author["e2ee_device_generation"] == "4"
    assert rendered_author["account_type"] == account_type
    assert rendered_author["bot"] is (account_type == "bot")
    assert page.messages[0]["reaction_counts"] == {
        "❤": 3,
        "<:lantern:75512661369970689@home.example>": 3,
    }
    assert page.messages[0]["reacted_emoji"] == [
        "❤",
        "<:lantern:75512661369970689@home.example>",
    ]


@pytest.mark.parametrize(
    ("reaction_counts", "reacted_emoji"),
    [
        ({"lantern": 1}, []),
        ({"🏮🔥": 1}, []),
        ({"🏮": 1}, ["lantern"]),
    ],
)
def test_dm_history_rejects_invalid_reaction_summaries(
    reaction_counts: dict[str, int],
    reacted_emoji: list[str],
) -> None:
    configured = settings()
    authority = "authority.example"
    now = datetime.now(UTC) - timedelta(minutes=1)
    conversation_ref = (snowflake_at(now - timedelta(days=1)), authority)
    author_id = snowflake_at(now - timedelta(days=2))
    message_id = snowflake_at(now)
    raw = message(
        message_id=message_id,
        origin=authority,
        channel_ref=conversation_ref,
        author_id=author_id,
        username="remote",
        created_at=now,
    )
    raw["reaction_counts"] = reaction_counts
    raw["reacted_emoji"] = reacted_emoji

    with pytest.raises(FederationNetworkError, match="reaction summary is invalid"):
        validate_dm_history_page(
            {
                "conversation_id": str(conversation_ref[0]),
                "conversation_domain": conversation_ref[1],
                "messages": [raw],
                "complete": True,
                "next_before": None,
            },
            settings=configured,
            conversation_ref=conversation_ref,
            authority_domain=authority,
            participant_refs={(author_id, authority)},
            trusted_profiles={},
            before=(snowflake_at(now + timedelta(seconds=1)), authority),
            limit=50,
        )


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
    postgres_schema,
) -> None:
    from sqlalchemy import text

    from app.db.models import E2EEControlRecord

    for table in ("messages", "attachments", "polls", "message_views", "dm_conversations"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
        )
    for table in ("users", "dm_participants", "e2ee_control_records"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
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
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        session.add_all([conversation, author, *messages])
        session.add(
            DMParticipant(
                conversation_id=100,
                conversation_domain=configured.domain,
                user_id=201,
                user_domain="requester.example",
            )
        )
        for identifier in (499, 497):
            session.add(
                Message(
                    id=identifier,
                    origin_domain=configured.domain,
                    channel_id=100,
                    channel_domain=configured.domain,
                    author_id=200,
                    author_domain=configured.domain,
                    content="control",
                    created_at=now,
                )
            )
            session.add(E2EEControlRecord(id=identifier, origin_domain=configured.domain))
        # Neither another conversation nor the requesting home can fill the page.
        session.add(
            Message(
                id=501,
                origin_domain=configured.domain,
                channel_id=101,
                channel_domain=configured.domain,
                author_id=200,
                author_domain=configured.domain,
                content="another room",
                created_at=now,
            )
        )
        session.add(
            Message(
                id=502,
                origin_domain="requester.example",
                channel_id=100,
                channel_domain=configured.domain,
                author_id=201,
                author_domain="requester.example",
                content="requester's source",
                created_at=now,
            )
        )
        await session.flush()
        monkeypatch.setattr(
            federation_api,
            "enforce_federation_route_rate_limit",
            AsyncMock(),
        )
        reaction_summaries = AsyncMock(
            return_value={
                (500, configured.domain): ({"❤": 2}, ["❤"]),
            }
        )
        monkeypatch.setattr(
            federation_api,
            "reaction_payloads_for_messages",
            reaction_summaries,
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

        assert [item["id"] for item in cast(list[dict[str, object]], result["messages"])] == [
            "500",
            "498",
        ]
        assert result["next_before"] == {
            "id": "498",
            "origin_domain": configured.domain,
        }
        assert result["complete"] is False
        rendered_author = cast(list[dict[str, Any]], result["messages"])[0]["author"]
        strict_author = RemoteUserProfile.model_validate(rendered_author)
        assert strict_author.profile_version == 1
        assert strict_author.e2ee_device_generation == 0
        assert cast(list[dict[str, Any]], result["messages"])[0]["reaction_counts"] == {"❤": 2}
        assert cast(list[dict[str, Any]], result["messages"])[0]["reacted_emoji"] == ["❤"]
        reaction_summaries.assert_awaited_once()

        next_page = await federation_dm_history_page(
            conversation_id=100,
            before_id=int(result["next_before"]["id"]),
            before_domain=result["next_before"]["origin_domain"],
            limit=2,
            requester_id=None,
            requester_domain=None,
            principal=FederationPrincipal("requester.example", "ed25519:test"),
            session=session,
            redis=cast(Any, object()),
            settings=configured,
        )
        assert [item["id"] for item in next_page["messages"]] == ["496"]
        assert next_page["complete"] is True
        assert next_page["next_before"] is None


@pytest.mark.asyncio
async def test_eager_channel_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    conversation = SimpleNamespace(
        id=100,
        origin_domain="home.example",
        pair_key="a" * 64,
        authority_domain="home.example",
    )
    channel = SimpleNamespace(
        id=100,
        origin_domain="home.example",
        updated_at=now,
    )
    local = SimpleNamespace(id=1, origin_domain="home.example", username="local")
    remote = SimpleNamespace(id=2, origin_domain="remote.example", username="remote")

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.refreshed: list[object] = []

        async def flush(self) -> None:
            assert not self.committed

        async def refresh(self, value: object) -> None:
            assert not self.committed
            self.refreshed.append(value)

        async def scalars(self, _statement: object) -> list[object]:
            assert not self.committed
            return [local, remote]

    session = FakeSession()

    def render_channel(value: object) -> dict[str, object]:
        assert not session.committed
        return {"version": cast(Any, value).updated_at.isoformat()}

    def render_dm_channel(
        value: object,
        recipients: list[object],
        **_kwargs: object,
    ) -> dict[str, object]:
        assert not session.committed
        return {
            "version": cast(Any, value).updated_at.isoformat(),
            "recipients": [cast(Any, item).username for item in recipients],
        }

    def render_profile(value: object) -> dict[str, object]:
        assert not session.committed
        return {"id": str(cast(Any, value).id)}

    monkeypatch.setattr(federation_api, "channel_payload", render_channel)
    monkeypatch.setattr(federation_api, "dm_channel_payload", render_dm_channel)
    monkeypatch.setattr(federation_api, "profile_from_user", render_profile)

    projection = await federation_api.materialize_dm_open_projection(
        cast(Any, session),
        cast(Any, conversation),
        cast(Any, channel),
        local_recipient_ref=(local.id, local.origin_domain),
        created=True,
    )
    session.committed = True

    assert session.refreshed == [conversation, channel]
    assert projection.channel == {"version": now.isoformat()}
    assert projection.created_channel == {
        "version": now.isoformat(),
        "recipients": ["remote"],
    }
    assert projection.participants == ({"id": "1"}, {"id": "2"})


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
        remote_complete=False,
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
async def test_eviction_query_protects_local_authored_and_actual_newest_rows(
    postgres_schema,
) -> None:
    from sqlalchemy import text

    from app.db.models import MessageProjection, Pin

    for table in (
        "messages",
        "channels",
        "dm_conversations",
        "pins",
        "message_projections",
        "attachments",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    configured = settings()
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        conversation = DMConversation(
            id=100,
            origin_domain="authority.example",
            authority_domain="authority.example",
            type="direct",
            pair_key="a" * 64,
        )
        channel = Channel(
            id=100,
            origin_domain="authority.example",
            type=1,
            last_message_id=7,
            last_message_domain="authority.example",
        )
        session.add_all([conversation, channel])
        for identifier in range(1, 10):
            domain = configured.domain if identifier == 2 else "authority.example"
            session.add(
                Message(
                    id=identifier,
                    origin_domain=domain,
                    channel_id=100,
                    channel_domain="authority.example",
                    author_id=1,
                    author_domain=domain,
                )
            )
        session.add(
            Pin(
                message_id=3,
                message_domain="authority.example",
                channel_id=100,
                channel_domain="authority.example",
                pinned_by_id=1,
                pinned_by_domain=configured.domain,
            )
        )
        session.add(
            MessageProjection(message_id=4, message_domain="authority.example", processed_at=None)
        )
        session.add(
            Attachment(
                id=50,
                origin_domain=configured.domain,
                message_id=5,
                message_domain="authority.example",
            )
        )
        await session.flush()
        rows = await _eligible_replica_messages(
            session,
            configured,
            conversation=None,
            authority_domain="authority.example",
            limit=10,
            protected_refs={(6, "authority.example")},
        )
        assert [(row.id, row.origin_domain) for row in rows] == [
            (1, "authority.example"),
            (8, "authority.example"),
        ]
        channel.last_message_id = channel.last_message_domain = None
        await session.flush()
        rows = await _eligible_replica_messages(
            session,
            configured,
            conversation=conversation,
            authority_domain="authority.example",
            limit=2,
        )
        assert [row.id for row in rows] == [1, 6]


@pytest.mark.asyncio
async def test_sweep_selects_only_over_target_capable_replicas(
    monkeypatch: pytest.MonkeyPatch, postgres_schema
) -> None:
    from sqlalchemy import text

    import app.federation.dm_storage as storage
    from app.db.models import FederatedDMRowCharge, FederatedDMStorageUsage, Instance
    from app.federation.dm_storage import DM_HISTORY_PAGE_CAPABILITY

    for table in ("instances", "dm_conversations", "federated_dm_row_charges"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "CREATE TABLE federated_dm_storage_usage (LIKE "
            "public.federated_dm_storage_usage INCLUDING ALL)"
        )
    )
    configured = settings(federation_dm_replica_cache_messages_per_conversation=100)
    prune = AsyncMock(return_value=SimpleNamespace(evicted_messages=1))
    monkeypatch.setattr(storage, "prune_federated_dm_replica", prune)
    monkeypatch.setattr(storage, "lock_federated_dm_authority", AsyncMock())
    async with AsyncSession(bind=postgres_schema) as session:
        for identifier in range(1, 7):
            domain = configured.domain if identifier == 2 else f"authority-{identifier}.example"
            session.add(
                Instance(
                    domain=domain,
                    is_self=identifier == 2,
                    capabilities=[] if identifier == 1 else [DM_HISTORY_PAGE_CAPABILITY],
                )
            )
            session.add(
                DMConversation(
                    id=identifier,
                    origin_domain=domain,
                    authority_domain=domain,
                    pair_key=f"{identifier:064x}",
                    type="direct",
                )
            )
            count = 100 if identifier == 3 else 101
            session.add(
                FederatedDMStorageUsage(
                    conversation_id=identifier,
                    conversation_domain=domain,
                    authority_domain=domain,
                    remote_origin_domain=domain,
                    message_rows=count,
                    message_bytes=count * 100,
                )
            )
            for offset in range(count):
                session.add(
                    FederatedDMRowCharge(
                        table_name="messages",
                        row_id=identifier * 1000 + offset,
                        row_domain=configured.domain if identifier == 4 else domain,
                        conversation_id=identifier,
                        conversation_domain=domain,
                        category="message",
                        charge_bytes=100,
                    )
                )
        await session.flush()
        changed = await sweep_federated_dm_replica_cache(session, configured, conversation_limit=1)
        assert changed == {(5, "authority-5.example")}
        prune.assert_awaited_once()
        assert prune.await_args.args[2].id == 5


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


async def test_rolling_cache_migration_preserves_reference_integrity_outside_opaque_prefix(
    postgres_schema, migrate_to
) -> None:
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db.base import Base

    await migrate_to("b72c9e4a1f63")
    statements = (
        "INSERT INTO instances (domain,is_self,current_key_id,encrypted_private_key,private_k"
        "ey_nonce,capabilities) VALUES ('local.example',true,'main',decode(repeat('ab',32),'h"
        "ex'),decode(repeat('cd',12),'hex'),'[]'),('remote.example',false,NULL,NULL,NULL,'["
        '"dm-history-page/1"]\')',
        "INSERT INTO users (id,origin_domain,username,is_local,password_hash) SELECT id,'loca"
        "l.example','local'||id,true,'hash' FROM (VALUES(1),(3)) AS ids(id)",
        "INSERT INTO users (id,origin_domain,username,is_local,federation_introduced_by_domai"
        "n) VALUES (2,'remote.example','remoteuser',false,'remote.example')",
        "INSERT INTO channels (id,origin_domain,type,created_floor_id) VALUES (10,'remote.exa"
        "mple',1,1),(11,'remote.example',1,1)",
        "INSERT INTO dm_conversations (id,origin_domain,pair_key,authority_domain,history_tru"
        "ncated,history_truncated_before_id,history_truncated_before_domain) VALUES (10,'remo"
        "te.example',repeat('a',64),'remote.example',true,100,'remote.example'),(11,'remote.e"
        "xample',repeat('b',64),'remote.example',false,NULL,NULL)",
        "INSERT INTO dm_participants (conversation_id,conversation_domain,user_id,user_domain"
        ") VALUES (10,'remote.example',1,'local.example'),(10,'remote.example',2,'remote.exam"
        "ple'),(11,'remote.example',1,'local.example'),(11,'remote.example',2,'remote.example"
        "')",
        "INSERT INTO messages (id,origin_domain,channel_id,channel_domain,author_id,author_do"
        "main,content) VALUES (50,'remote.example',10,'remote.example',2,'remote.example','ev"
        "icted'),(110,'remote.example',10,'remote.example',2,'remote.example','retained'),(11"
        "9,'remote.example',11,'remote.example',2,'remote.example','other room')",
        "INSERT INTO messages (id,origin_domain,channel_id,channel_domain,author_id,author_do"
        "main,content,referenced_message_id,referenced_message_domain) VALUES (120,'remote.ex"
        "ample',10,'remote.example',2,'remote.example','opaque reply',50,'remote.example'),(1"
        "21,'remote.example',10,'remote.example',2,'remote.example','live reply',110,'remote."
        "example')",
        "INSERT INTO read_states (user_id,user_domain,channel_id,channel_domain,last_message_"
        "id,last_message_domain) VALUES (1,'local.example',10,'remote.example',50,'remote.exa"
        "mple'),(3,'local.example',10,'remote.example',110,'remote.example')",
        "SET CONSTRAINTS ALL IMMEDIATE",
        "DELETE FROM messages WHERE id=50 AND origin_domain='remote.example'",
    )
    for statement in statements:
        await postgres_schema.execute(text(statement))
    assert (
        await postgres_schema.scalar(
            text("SELECT referenced_message_id FROM messages WHERE id=120")
        )
        == 50
    )
    assert (
        await postgres_schema.scalar(
            text("SELECT last_message_id FROM read_states WHERE user_id=1")
        )
        == 50
    )
    for identifier, domain in (
        (119, "remote.example"),
        (105, "remote.example"),
        (50, "local.example"),
        (50, "nonparticipant.example"),
    ):
        for statement in (
            "UPDATE messages SET "
            "referenced_message_id=:id,referenced_message_domain=:domain WHERE id=121",
            "UPDATE read_states SET last_message_id=:id,last_message_domain=:domain WHERE "
            "user_id=3",
        ):
            with pytest.raises(IntegrityError) as denied:
                async with postgres_schema.begin_nested():
                    await postgres_schema.execute(
                        text(statement), dict(id=identifier, domain=domain)
                    )
            assert denied.value.orig.sqlstate == "23503"
    for statement in (
        "DELETE FROM messages WHERE id=110 AND origin_domain='remote.example'",
        "UPDATE dm_conversations SET history_truncated_before_id=40 WHERE id=10",
    ):
        with pytest.raises(IntegrityError) as denied:
            async with postgres_schema.begin_nested():
                await postgres_schema.execute(text(statement))
                await postgres_schema.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert denied.value.orig.sqlstate == "23503"
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(
                text("UPDATE instances SET capabilities='[]' WHERE domain='remote.example'")
            )
            await postgres_schema.execute(
                text(
                    "UPDATE messages SET "
                    "referenced_message_id=50,referenced_message_domain='remote.example' "
                    "WHERE id=121"
                )
            )
    migration = import_module("migrations.versions.b72c9e4a1f63_federated_dm_rolling_cache")

    def downgrade(sync):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()

    await postgres_schema.run_sync(downgrade)
    assert (
        await postgres_schema.execute(
            text(
                "SELECT id,referenced_message_id,referenced_message_domain FROM messages "
                "WHERE id IN (120,121) ORDER BY id"
            )
        )
    ).all() == [(120, None, None), (121, 110, "remote.example")]
    assert (
        await postgres_schema.execute(
            text(
                "SELECT user_id,last_message_id,last_message_domain FROM read_states ORDER "
                "BY user_id"
            )
        )
    ).all() == [(1, None, None), (3, 110, "remote.example")]
    assert set(await postgres_schema.scalars(text("SELECT id FROM messages"))) == {
        110,
        119,
        120,
        121,
    }
    with pytest.raises(IntegrityError) as restored:
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(
                text(
                    "UPDATE messages SET "
                    "referenced_message_id=50,referenced_message_domain='remote.example' "
                    "WHERE id=120"
                )
            )
    assert restored.value.orig.sqlstate == "23503"


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
    valid_scope = {
        "conversation_ref": (1, "authority.example"),
        "message_ref": (2, "authority.example"),
        "attachment_ref": (3, "authority.example"),
        "variant": "original",
    }
    for field, wrong in (
        ("conversation_ref", (4, "authority.example")),
        ("conversation_ref", (1, "other.example")),
        ("message_ref", (4, "authority.example")),
        ("message_ref", (2, "other.example")),
        ("attachment_ref", (4, "authority.example")),
        ("attachment_ref", (3, "other.example")),
        ("variant", "thumbnail_128"),
    ):
        assert not verify_history_media_capability(
            configured,
            **(valid_scope | {field: wrong}),
            expires=int(values["expires"]),
            token=values["token"],
            now=now,
        ), field
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
    conversation_channel = Channel(
        id=1,
        origin_domain="authority.example",
        type=1,
        created_floor_id=1,
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
    get_calls: list[tuple[object, dict[str, object]]] = []

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        get_calls.append((model, _kwargs))
        if model is DMConversation and key == (1, "authority.example"):
            return conversation
        if model is Channel and key == (1, "authority.example"):
            return conversation_channel
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
    assert any(
        model is RemoteMediaCache
        and kwargs.get("populate_existing") is True
        and kwargs.get("with_for_update") is True
        for model, kwargs in get_calls
    )


@pytest.mark.asyncio
async def test_history_media_origin_rejects_cross_conversation_or_message_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        SimpleNamespace(
            get=get,
            scalar=AsyncMock(side_effect=[1, attachment, message_row, 1]),
            execute=AsyncMock(),
            commit=AsyncMock(),
        ),
    )
    record_recipients = AsyncMock(return_value={(7, configured.domain)})
    monkeypatch.setattr(
        federation_api,
        "record_attachment_recipients",
        record_recipients,
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
    record_recipients.assert_awaited_once()
    session.commit.assert_awaited_once()  # type: ignore[attr-defined]

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


@pytest.mark.parametrize("terminal_field", ["scan_status", "deleted_at"])
async def test_history_media_origin_rechecks_terminal_state_after_recipient_commit(
    monkeypatch: pytest.MonkeyPatch,
    terminal_field: str,
) -> None:
    configured = settings()
    attachment = Attachment(
        id=8,
        origin_domain=configured.domain,
        uploader_id=1,
        uploader_domain=configured.domain,
        filename="image.png",
        content_type="image/png",
        size=128,
        purpose="attachment",
        scan_status="clean",
        message_id=21,
        message_domain=configured.domain,
    )
    message_row = Message(
        id=21,
        origin_domain=configured.domain,
        channel_id=31,
        channel_domain="authority.example",
        author_id=1,
        author_domain=configured.domain,
        content="image",
    )
    channel = Channel(
        id=31,
        origin_domain="authority.example",
        type=1,
        name=None,
        guild_id=None,
        guild_domain=None,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Attachment and key == (8, configured.domain):
            return attachment
        if model is Message:
            return message_row
        if model is Channel:
            return channel
        return None

    async def commit() -> None:
        if terminal_field == "scan_status":
            attachment.scan_status = "quarantined"
        else:
            attachment.deleted_at = datetime.now(UTC)

    session = cast(
        AsyncSession,
        SimpleNamespace(
            get=get,
            scalar=AsyncMock(side_effect=[1, attachment]),
            execute=AsyncMock(),
            commit=commit,
        ),
    )
    monkeypatch.setattr(
        federation_api,
        "record_attachment_recipients",
        AsyncMock(return_value={(8, configured.domain)}),
    )

    with pytest.raises(HTTPException) as raised:
        await _federation_media_attachment(
            session,
            cast(Any, object()),
            configured,
            FederationPrincipal(origin="requester.example", key_id="ed25519:test"),
            8,
            "original",
            expected_conversation=(31, "authority.example"),
            expected_message=(21, configured.domain),
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
    with pytest.raises(HTTPException) as raised:
        raise_proxy_rejection(response, {403, 404, 429, 507})
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
