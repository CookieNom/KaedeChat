from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import unwrap
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app import tasks
from app.api import media as media_api
from app.core.settings import Settings
from app.db.bot_models import ApplicationEmoji
from app.db.models import Attachment, Emoji, Guild, Message, Role, SoundboardSound, User
from app.media import asset_invalidation, digest_revocation, service, tombstones
from app.media import jobs as media_jobs

LOCAL_DOMAIN = "alpha.localhost"
REMOTE_DOMAIN = "beta.localhost"
DIGEST = "a" * 64


@pytest.fixture
async def terminal_worker_database(postgres_schema, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    for table in (
        "instances",
        "attachments",
        "messages",
        "media_tombstone_sources",
        "federation_events",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "INSERT INTO instances (domain,is_self,current_key_id) VALUES "
            "(:domain,true,'ed25519:current')"
        ),
        dict(domain=LOCAL_DOMAIN),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    config = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "unused"),
        dragonfly_url=SimpleNamespace(get_secret_value=lambda: "redis://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: config)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, async_sessionmaker(bind=postgres_schema, expire_on_commit=False)),
    )
    return postgres_schema, engine


def settings() -> Settings:
    return cast(Settings, SimpleNamespace(domain=LOCAL_DOMAIN))


def attachment(binding: str) -> Attachment:
    return Attachment(
        id=30,
        origin_domain=LOCAL_DOMAIN,
        uploader_id=20,
        uploader_domain=LOCAL_DOMAIN,
        filename="asset.png",
        content_type="image/png",
        size=128,
        object_key="alpha.localhost/30/clean/original",
        content_sha256=DIGEST,
        variants={},
        scan_status="rejected",
        purpose="guild_icon",
        asset_binding=binding,
    )


def test_neutral_rejection_is_not_digest_revocation_evidence() -> None:
    assert {"infected", "quarantined"} == digest_revocation.DIGEST_REVOCATION_STATUSES
    assert "rejected" in digest_revocation.TERMINAL_ATTACHMENT_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "field_name"),
    (("avatar", "avatar_hash"), ("banner", "banner_hash")),
)
async def test_terminal_user_asset_is_cleared_and_friend_update_is_queued_atomically(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    field_name: str,
) -> None:
    user = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=4,
        profile_resolved=True,
    )
    setattr(user, field_name, DIGEST)
    item = attachment(f"user:{LOCAL_DOMAIN}:{user.id}:{kind}")
    statements: list[object] = []

    class Session:
        async def scalar(self, statement: object) -> object:
            statements.append(statement)
            return user

        async def commit(self) -> None:
            raise AssertionError("asset invalidation must not commit independently")

    queue_profiles = AsyncMock(return_value={REMOTE_DOMAIN})
    monkeypatch.setattr(asset_invalidation, "queue_profile_updates", queue_profiles)

    result = await asset_invalidation.invalidate_terminal_asset_binding(
        cast(Any, Session()), settings(), item
    )

    assert result is not None
    assert result.user is user
    assert result.profile_destinations == {REMOTE_DOMAIN}
    assert getattr(user, field_name) is None
    assert user.profile_version == 5
    assert item.asset_binding is None
    queue_profiles.assert_awaited_once_with(ANY, settings(), user)
    statement_sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE NOWAIT" in statement_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "field_name"),
    (("icon", "icon_hash"), ("banner", "banner_hash")),
)
async def test_terminal_guild_asset_queues_signed_partial_guild_update(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    field_name: str,
) -> None:
    owner = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain=LOCAL_DOMAIN,
        name="Paper Lantern",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    setattr(guild, field_name, DIGEST)
    item = attachment(f"guild:{LOCAL_DOMAIN}:{guild.id}:{kind}")

    class Session:
        async def scalar(self, _statement: object) -> object:
            return guild

        async def get(self, model: object, key: object) -> object:
            assert model is User
            assert key == (owner.id, owner.origin_domain)
            return owner

    queue_mutation = AsyncMock(return_value=1)
    monkeypatch.setattr(asset_invalidation, "queue_guild_mutation", queue_mutation)

    result = await asset_invalidation.invalidate_terminal_asset_binding(
        cast(Any, Session()), settings(), item
    )

    assert result is not None
    assert result.guild is guild
    assert result.dispatch_type == "GUILD_UPDATE"
    assert getattr(guild, field_name) is None
    assert item.asset_binding is None
    queue_mutation.assert_awaited_once_with(
        ANY,
        settings(),
        guild,
        owner,
        "guild.update",
        {
            "guild": {
                "id": str(guild.id),
                "origin_domain": guild.origin_domain,
                field_name: None,
            }
        },
    )


@pytest.mark.asyncio
async def test_terminal_role_icon_is_cleared_and_federated_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain=LOCAL_DOMAIN,
        name="Paper Lantern",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    role = Role(
        id=11,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Guard",
        icon_hash=DIGEST,
        color=0,
        permissions=0,
        position=1,
        hoist=False,
        mentionable=False,
    )
    item = attachment(f"role:{LOCAL_DOMAIN}:{role.id}:icon")
    calls = 0

    class Session:
        async def get(self, model: object, key: object) -> object:
            if model is Role:
                return role
            assert model is User
            return owner

        async def scalar(self, _statement: object) -> object:
            nonlocal calls
            calls += 1
            return guild if calls == 1 else role

        async def flush(self) -> None:
            return None

        async def refresh(
            self,
            _row: object,
            *,
            attribute_names: tuple[str, ...],
        ) -> None:
            assert attribute_names == ("updated_at",)

    queue_mutation = AsyncMock(return_value=1)
    monkeypatch.setattr(asset_invalidation, "queue_guild_mutation", queue_mutation)

    result = await asset_invalidation.invalidate_terminal_asset_binding(
        cast(Any, Session()), settings(), item
    )

    assert result is not None
    assert result.dispatch_type == "GUILD_ROLE_UPDATE"
    assert result.dispatch_payload is not None
    assert result.dispatch_payload["icon_hash"] is None
    assert role.icon_hash is None
    assert item.asset_binding is None
    queue_mutation.assert_awaited_once_with(
        ANY,
        settings(),
        guild,
        owner,
        "guild.role.update",
        {"role": result.dispatch_payload},
        snapshot_required=True,
    )


@pytest.mark.asyncio
async def test_terminal_emoji_asset_deletes_emoji_and_queues_signed_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain=LOCAL_DOMAIN,
        name="Paper Lantern",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    emoji = Emoji(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="kaede",
        object_key="alpha.localhost/30/thumbnail_128",
        media_hash=DIGEST,
        animated=False,
        creator_id=owner.id,
        creator_domain=owner.origin_domain,
    )
    item = attachment(f"emoji:{LOCAL_DOMAIN}:{emoji.id}")
    scalar_values = iter((guild, emoji))
    deleted: list[object] = []

    class Session:
        async def get(self, model: object, key: object) -> object:
            if model is Emoji:
                assert key == (emoji.id, emoji.origin_domain)
                return emoji
            assert model is User
            assert key == (owner.id, owner.origin_domain)
            return owner

        async def scalar(self, _statement: object) -> object:
            return next(scalar_values)

        async def delete(self, value: object) -> None:
            deleted.append(value)

    queue_mutation = AsyncMock(return_value=1)
    monkeypatch.setattr(asset_invalidation, "queue_guild_mutation", queue_mutation)

    result = await asset_invalidation.invalidate_terminal_asset_binding(
        cast(Any, Session()), settings(), item
    )

    assert result is not None
    assert result.guild is guild
    assert result.dispatch_type == "GUILD_EMOJI_DELETE"
    assert result.dispatch_payload is not None
    assert result.dispatch_payload["id"] == str(emoji.id)
    assert deleted == [emoji]
    assert item.asset_binding is None
    assert queue_mutation.await_args.args[4] == "guild.emoji.delete"


@pytest.mark.asyncio
async def test_terminal_soundboard_asset_deletes_its_playable_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain=LOCAL_DOMAIN,
        name="Paper Lantern",
        owner_id=20,
        owner_domain=LOCAL_DOMAIN,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    sound = SoundboardSound(
        id=44,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Horn",
        media_hash=DIGEST,
        object_key="alpha.localhost/30/clean/original",
        content_type="audio/ogg",
        volume=1,
        duration_ms=900,
        created_by_id=20,
        created_by_domain=LOCAL_DOMAIN,
        version=1,
    )
    item = attachment(f"soundboard:{LOCAL_DOMAIN}:{sound.id}")
    deleted: list[object] = []
    scalar_values = iter((guild, sound))

    class Session:
        async def get(self, model: object, key: object) -> object:
            if model is SoundboardSound:
                assert key == (sound.id, sound.origin_domain)
                return sound
            assert model is User
            assert key == (owner.id, owner.origin_domain)
            return owner

        async def scalar(self, _statement: object) -> object:
            return next(scalar_values)

        async def delete(self, value: object) -> None:
            deleted.append(value)

        async def scalars(self, _statement: object) -> list[object]:
            return []

    queue_mutation = AsyncMock(return_value=1)
    monkeypatch.setattr(asset_invalidation, "queue_guild_mutation", queue_mutation)

    result = await asset_invalidation.invalidate_terminal_asset_binding(
        cast(Any, Session()), settings(), item
    )

    assert result is not None
    assert result.guild is guild
    assert result.dispatch_type == "GUILD_SOUNDBOARD_SOUND_DELETE"
    assert result.dispatch_payload is not None
    assert result.dispatch_payload["id"] == str(sound.id)
    assert deleted == [sound]
    assert item.asset_binding is None
    assert [call.args[4] for call in queue_mutation.await_args_list] == [
        "guild.soundboard.sound.delete",
        "guild.soundboard.sounds.update",
    ]


@pytest.mark.asyncio
async def test_public_asset_requires_a_current_non_null_binding(
    monkeypatch: pytest.MonkeyPatch, postgres_schema
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    for table in ("attachments", "media_tombstone_sources"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    redirect = Mock(return_value=SimpleNamespace(status_code=307))
    monkeypatch.setattr(media_api, "redirect_to_object", redirect)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        bound = attachment(f"guild:{LOCAL_DOMAIN}:10:icon")
        bound.scan_status = "clean"
        unbound = attachment("")
        unbound.id, unbound.asset_binding, unbound.scan_status = 31, None, "clean"
        foreign = attachment(f"guild:{REMOTE_DOMAIN}:10:icon")
        foreign.origin_domain, foreign.scan_status = REMOTE_DOMAIN, "clean"
        session.add_all([bound, unbound, foreign])
        await session.flush()
        assert (
            await media_api.public_asset(DIGEST, "original", session, settings())
            is redirect.return_value
        )
        redirect.assert_called_once_with(settings(), bound, "original", public=True)
        bound.asset_binding = None
        await session.flush()
        with pytest.raises(HTTPException) as denied:
            await media_api.public_asset(DIGEST, "original", session, settings())
        assert denied.value.status_code == 404
        assert denied.value.detail == {"code": "MEDIA_NOT_FOUND"}
        redirect.assert_called_once()


@pytest.mark.asyncio
async def test_public_emoji_rejects_a_hash_with_any_terminal_duplicate(
    monkeypatch: pytest.MonkeyPatch, postgres_schema
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    for table in ("attachments", "emojis", "media_tombstone_sources"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    redirect = Mock(return_value=SimpleNamespace(status_code=307))
    monkeypatch.setattr(media_api, "redirect_to_object", redirect)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        emoji = Emoji(id=40, origin_domain=LOCAL_DOMAIN, media_hash=DIGEST, available=True)
        clean = attachment(f"emoji:{LOCAL_DOMAIN}:40")
        clean.scan_status = "clean"
        duplicate = attachment("")
        duplicate.id, duplicate.asset_binding, duplicate.scan_status = 31, None, "infected"
        duplicate.content_sha256 = "b" * 64
        session.add_all([emoji, clean, duplicate])
        await session.flush()
        assert (
            await media_api.public_emoji(40, "original", session, settings())
            is redirect.return_value
        )
        redirect.assert_called_once_with(settings(), clean, "original", public=True)
        duplicate.content_sha256 = DIGEST
        await session.flush()
        with pytest.raises(HTTPException) as denied:
            await media_api.public_emoji(40, "original", session, settings())
        assert denied.value.status_code == 404
        assert denied.value.detail == {"code": "EMOJI_NOT_FOUND"}
        redirect.assert_called_once()


@pytest.mark.asyncio
async def test_public_emoji_resolves_application_emoji_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emoji = SimpleNamespace(
        id=41,
        application_id=12,
        application_domain=LOCAL_DOMAIN,
        media_hash=DIGEST,
        available=True,
    )
    statements: list[object] = []
    scalar_values = iter((emoji, emoji, None))
    digest_lock = AsyncMock()

    class Session:
        async def get(self, model: object, key: object) -> object | None:
            assert model is Emoji
            assert key == (emoji.id, emoji.application_domain)
            return None

        async def scalar(self, statement: object) -> object:
            statements.append(statement)
            return next(scalar_values)

    monkeypatch.setattr(media_api, "lock_asset_digest", digest_lock)

    with pytest.raises(HTTPException) as raised:
        await media_api.public_emoji(
            emoji_id=cast(Any, emoji.id),
            variant="original",
            session=cast(Any, Session()),
            settings=settings(),
        )

    assert raised.value.status_code == 404
    digest_lock.assert_awaited_once_with(ANY, DIGEST)
    initial_sql = str(statements[0].compile(dialect=postgresql.dialect()))
    revalidated_sql = str(statements[1].compile(dialect=postgresql.dialect()))
    attachment_statement = statements[2].compile(dialect=postgresql.dialect())
    assert ApplicationEmoji.__tablename__ in initial_sql
    assert "application_emojis.media_hash =" in revalidated_sql
    assert (
        f"application:{LOCAL_DOMAIN}:{emoji.application_id}:emoji:{emoji.id}"
        in attachment_statement.params.values()
    )


@pytest.mark.asyncio
async def test_bind_asset_rejects_retained_terminal_digest_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = attachment(f"guild:{LOCAL_DOMAIN}:10:icon")
    item.scan_status = "clean"
    item.asset_binding = None
    events: list[str] = []
    statements: list[object] = []

    class Session:
        async def scalar(self, statement: object) -> int:
            events.append("evidence")
            statements.append(statement)
            return 99

    async def lock(_session: object, digest: str) -> bool:
        assert digest == DIGEST
        events.append("digest_lock")
        return True

    monkeypatch.setattr(service, "try_lock_asset_digest", lock)

    with pytest.raises(HTTPException) as raised:
        await service.bind_asset(
            cast(Any, Session()),
            item,
            f"guild:{LOCAL_DOMAIN}:10:icon",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == {"code": "MEDIA_NOT_AVAILABLE"}
    assert events == ["digest_lock", "evidence"]
    assert item.asset_binding is None
    statement_sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "attachments.content_sha256 =" in statement_sql
    assert "attachments.scan_status IN" in statement_sql
    assert set(statements[0].compile().params["scan_status_1"]) == (
        digest_revocation.DIGEST_REVOCATION_STATUSES
    )


@pytest.mark.asyncio
async def test_bind_asset_allows_clean_duplicate_without_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = f"guild:{LOCAL_DOMAIN}:10:icon"
    item = attachment(binding)
    item.scan_status = "clean"
    item.asset_binding = None
    digest_lock = AsyncMock(return_value=True)

    class Session:
        def __init__(self) -> None:
            self.values = iter((None, None))

        async def scalar(self, _statement: object) -> object | None:
            return next(self.values)

    monkeypatch.setattr(service, "try_lock_asset_digest", digest_lock)

    previous = await service.bind_asset(cast(Any, Session()), item, binding)

    assert previous is None
    assert item.asset_binding == binding
    digest_lock.assert_awaited_once_with(ANY, DIGEST)


@pytest.mark.asyncio
async def test_bind_asset_returns_retryable_error_instead_of_waiting_on_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = attachment("")
    item.scan_status = "clean"
    digest_try_lock = AsyncMock(return_value=False)
    session = SimpleNamespace(scalar=AsyncMock())
    monkeypatch.setattr(service, "try_lock_asset_digest", digest_try_lock)

    with pytest.raises(HTTPException) as raised:
        await service.bind_asset(
            cast(Any, session),
            item,
            f"user:{LOCAL_DOMAIN}:20:avatar",
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == {"code": "MEDIA_NOT_AVAILABLE"}
    assert raised.value.headers == {"Retry-After": "1"}
    session.scalar.assert_not_awaited()


def test_digest_fence_uses_transaction_advisory_lock_shapes() -> None:
    blocking_sql = str(
        digest_revocation._digest_lock_statement(DIGEST).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    try_sql = str(
        digest_revocation._digest_lock_statement(DIGEST, try_lock=True).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "pg_advisory_xact_lock" in blocking_sql
    assert "pg_try_advisory_xact_lock" in try_sql
    assert f"kaede-public-asset-digest:{DIGEST}" in try_sql


def test_attachment_digest_index_supports_parameterized_terminal_probes() -> None:
    digest_index = next(
        index
        for index in Attachment.__table__.indexes
        if index.name == "ix_attachments_content_digest"
    )

    assert [column.name for column in digest_index.columns] == [
        "origin_domain",
        "content_sha256",
    ]
    predicate = digest_index.dialect_options["postgresql"]["where"]
    assert str(predicate.compile(dialect=postgresql.dialect())) == ("content_sha256 IS NOT NULL")

    pending_gc_index = next(
        index for index in Attachment.__table__.indexes if index.name == "ix_attachments_pending_gc"
    )
    pending_gc_predicate = pending_gc_index.dialect_options["postgresql"]["where"]
    assert str(pending_gc_predicate.compile(dialect=postgresql.dialect())) == (
        "finalized_at IS NULL AND deleted_at IS NULL"
    )


@pytest.mark.asyncio
async def test_terminal_verdict_aborts_before_callback_when_digest_fence_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = attachment(f"guild:{LOCAL_DOMAIN}:10:icon")
    item.deleted_at = datetime.now(UTC)
    scalar_values = iter((None, item))
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=lambda _statement: next(scalar_values)),
        commit=AsyncMock(),
    )
    digest_try_lock = AsyncMock(return_value=False)
    callback = AsyncMock()
    monkeypatch.setattr(media_jobs, "try_lock_asset_digest", digest_try_lock)

    with pytest.raises(media_jobs.TerminalCommitPreparationError, match="digest fence"):
        await media_jobs.process_attachment_record(
            cast(Any, session),
            settings(),
            item.id,
            item.origin_domain,
            before_terminal_commit=callback,
        )

    digest_try_lock.assert_awaited_once_with(ANY, DIGEST)
    callback.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bounded_digest_repair_clears_duplicate_user_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = attachment("")
    terminal.id = 31
    duplicate = attachment(f"user:{LOCAL_DOMAIN}:20:avatar")
    duplicate.scan_status = "clean"
    user = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=4,
        profile_resolved=True,
        avatar_hash=DIGEST,
    )
    scalar_values = iter((terminal.id, user, None))
    scalar_statements: list[object] = []
    scalars_statements: list[object] = []
    events: list[str] = []

    class Session:
        async def scalar(self, statement: object) -> object | None:
            scalar_statements.append(statement)
            return next(scalar_values)

        async def scalars(self, statement: object) -> list[Attachment]:
            events.append("attachment_batch")
            scalars_statements.append(statement)
            return [duplicate]

        async def flush(self) -> None:
            events.append("flush")

    async def lock(_session: object, digest: str) -> None:
        assert digest == DIGEST
        events.append("digest_lock")

    monkeypatch.setattr(asset_invalidation, "lock_asset_digest", lock)
    queue_profiles = AsyncMock(return_value={REMOTE_DOMAIN})
    monkeypatch.setattr(asset_invalidation, "queue_profile_updates", queue_profiles)

    (
        invalidations,
        duplicate_purge_refs,
        processed,
        more,
    ) = await asset_invalidation.invalidate_terminal_digest_binding_batch(
        cast(Any, Session()),
        settings(),
        DIGEST,
        limit=25,
    )

    assert events == ["digest_lock", "attachment_batch", "flush"]
    assert (processed, more) == (1, False)
    assert duplicate_purge_refs == [(duplicate.id, duplicate.origin_domain)]
    assert len(invalidations) == 1
    assert invalidations[0].user is user
    assert invalidations[0].profile_destinations == {REMOTE_DOMAIN}
    assert user.avatar_hash is None
    assert user.profile_version == 5
    assert duplicate.asset_binding is None
    assert duplicate.scan_status == "rejected"
    assert terminal.scan_status == "rejected"
    queue_profiles.assert_awaited_once_with(ANY, settings(), user)
    batch_sql = str(scalars_statements[0].compile(dialect=postgresql.dialect()))
    assert "LIMIT" in batch_sql
    assert "FOR UPDATE SKIP LOCKED" in batch_sql


@pytest.mark.asyncio
async def test_digest_repair_terminal_marker_advances_past_first_page(postgres_schema) -> None:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    for table in ("attachments", "media_tombstone_sources"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        terminal = attachment("")
        terminal.id, terminal.asset_binding, terminal.scan_status = 1, None, "infected"
        session.add(terminal)
        for identifier in range(100, 130):
            duplicate = attachment("")
            duplicate.id, duplicate.asset_binding, duplicate.scan_status = identifier, None, "clean"
            session.add(duplicate)
        unrelated = attachment("")
        unrelated.id, unrelated.asset_binding, unrelated.scan_status = 2, None, "clean"
        unrelated.content_sha256 = "b" * 64
        session.add(unrelated)
        await session.flush()
        first = await asset_invalidation.invalidate_terminal_digest_binding_batch(
            session, settings(), DIGEST, limit=25
        )
        assert first == (
            [],
            [(identifier, LOCAL_DOMAIN) for identifier in range(100, 125)],
            25,
            True,
        )
        second = await asset_invalidation.invalidate_terminal_digest_binding_batch(
            session, settings(), DIGEST, limit=25
        )
        assert second == (
            [],
            [(identifier, LOCAL_DOMAIN) for identifier in range(125, 130)],
            5,
            False,
        )
        assert set(
            (
                await session.execute(select(Attachment.scan_status).where(Attachment.id >= 100))
            ).scalars()
        ) == {"rejected"}
        assert unrelated.scan_status == "clean"


@pytest.mark.asyncio
async def test_duplicate_purge_stages_source_in_canonical_order_before_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = attachment("")
    duplicate.scan_status = "rejected"
    duplicate.asset_binding = None
    events: list[str] = []
    scalar_values = iter((duplicate, 31))
    scalar_statements: list[object] = []

    class Session:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, statement: object) -> object:
            scalar_statements.append(statement)
            events.append("attachment" if len(scalar_statements) == 1 else "evidence")
            return next(scalar_values)

        async def commit(self) -> None:
            events.append("commit")
            self.committed = True

    class Engine:
        dispose = AsyncMock()

    session = Session()
    engine = Engine()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )

    async def lock_ref(*_args: object) -> None:
        events.append("media_ref")

    async def lock_digest(*_args: object) -> bool:
        events.append("digest")
        return True

    async def queue_source(*_args: object, **kwargs: object) -> set[str]:
        assert kwargs == {"force_authoritative": True}
        assert not session.committed
        events.append("source")
        return {REMOTE_DOMAIN}

    async def enqueue_after_commit(*_args: object) -> None:
        assert session.committed
        events.append("wake")

    monkeypatch.setattr(tasks, "lock_media_tombstone_ref", lock_ref)
    monkeypatch.setattr(tasks, "try_lock_asset_digest", lock_digest)
    queue_tombstone = AsyncMock(side_effect=queue_source)
    monkeypatch.setattr(tasks, "queue_terminal_attachment_tombstone", queue_tombstone)
    enqueue = AsyncMock(side_effect=enqueue_after_commit)
    monkeypatch.setattr(tasks, "enqueue_best_effort", enqueue)

    result = await unwrap(tasks.media_terminal_asset_duplicate_purge.original_func)(
        duplicate.id,
        duplicate.origin_domain,
        DIGEST,
    )

    assert result == "queued"
    assert events == [
        "media_ref",
        "attachment",
        "digest",
        "evidence",
        "source",
        "commit",
        "wake",
        "wake",
    ]
    queue_tombstone.assert_awaited_once_with(
        session,
        worker_settings,
        duplicate,
        force_authoritative=True,
    )
    assert enqueue.await_args_list[0].args == (tasks.federation_deliver, REMOTE_DOMAIN)
    assert enqueue.await_args_list[1].args == (
        tasks.media_local_purge,
        duplicate.id,
        duplicate.origin_domain,
    )
    locked_sql = str(scalar_statements[0].compile(dialect=postgresql.dialect()))
    evidence_sql = str(scalar_statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in locked_sql
    assert "attachments.scan_status IN" in evidence_sql
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_digest_repair_wakes_durable_duplicate_purge_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

    class Engine:
        dispose = AsyncMock()

    session = Session()
    engine = Engine()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    repair = AsyncMock(return_value=([], [(30, LOCAL_DOMAIN)], 1, False))
    monkeypatch.setattr(tasks, "invalidate_terminal_digest_binding_batch", repair)

    async def enqueue_after_commit(*_args: object) -> None:
        assert session.committed

    enqueue = AsyncMock(side_effect=enqueue_after_commit)
    monkeypatch.setattr(tasks, "enqueue_best_effort", enqueue)

    result = await unwrap(tasks.media_terminal_asset_digest_repair.original_func)(DIGEST)

    assert result == 1
    repair.assert_awaited_once_with(session, worker_settings, DIGEST)
    enqueue.assert_awaited_once_with(
        tasks.media_terminal_asset_duplicate_purge,
        30,
        LOCAL_DOMAIN,
        DIGEST,
    )
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_sweep_rediscovers_lost_source_purge_wake(
    monkeypatch: pytest.MonkeyPatch, terminal_worker_database
) -> None:
    from sqlalchemy import text

    connection, engine = terminal_worker_database
    await connection.execute(
        text(
            "INSERT INTO attachments (id,origin_domain,purpose,scan_status,deleted_at) "
            "VALUES "
            "(30,:local,'attachment','clean',NULL),(31,:local,'attachment','clean',NULL),(32,:local,'attachment','clean',now()),(30,:remote,'attachment','clean',NULL)"
        ),
        dict(local=LOCAL_DOMAIN, remote=REMOTE_DOMAIN),
    )
    await connection.execute(
        text(
            "INSERT INTO media_tombstone_sources "
            "(attachment_id,attachment_domain,event_id,key_id,generation) VALUES "
            "(30,:local,'proof30','ed25519:current',1),(32,:local,'proof32','ed25519:current',1),(30,:remote,'foreign','ed25519:current',1)"
        ),
        dict(local=LOCAL_DOMAIN, remote=REMOTE_DOMAIN),
    )
    await connection.execute(
        text(
            "INSERT INTO federation_events (origin_domain,event_id) VALUES "
            "(:local,'proof30'),(:local,'proof32'),(:remote,'foreign')"
        ),
        dict(local=LOCAL_DOMAIN, remote=REMOTE_DOMAIN),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(tasks, "enqueue_best_effort", enqueue)
    assert await unwrap(tasks.media_terminal_tombstone_sweep.original_func)() == 1
    enqueue.assert_awaited_once_with(tasks.media_local_purge, 30, LOCAL_DOMAIN)
    engine.dispose.assert_awaited_once()


def test_public_asset_redirect_has_a_short_revocable_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = attachment(f"guild:{LOCAL_DOMAIN}:10:icon")
    item.scan_status = "clean"
    item.encryption_mode = "plaintext"
    captured: dict[str, object] = {}

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        def presign(self, method: str, bucket: str, key: str, **kwargs: object) -> str:
            captured.update(method=method, bucket=bucket, key=key, **kwargs)
            return "https://objects.example/asset"

    configured = cast(
        Settings,
        SimpleNamespace(
            domain=LOCAL_DOMAIN,
            media_attachments_bucket="attachments",
            media_derived_bucket="derived",
        ),
    )
    monkeypatch.setattr(media_api, "S3Storage", Storage)

    response = media_api.redirect_to_object(configured, item, "original", public=True)

    assert captured["expires"] == media_api.PUBLIC_MEDIA_CAPABILITY_SECONDS == 300
    assert response.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
    assert response.headers["X-Kaede-Media-Origin"] == "https://objects.example"


async def test_terminal_cleanup_retains_proof_for_bound_or_clean_unbound_public_duplicates(
    media_cleanup_session,
) -> None:
    from sqlalchemy import text

    session = media_cleanup_session
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO media_tombstone_sources "
            "(attachment_id,attachment_domain,event_id,updated_at) SELECT "
            "id,:domain,'proof'||id,:old FROM generate_series(1,4) AS id"
        ),
        dict(domain=LOCAL_DOMAIN, old=now - timedelta(days=40)),
    )
    for identifier in range(1, 5):
        await session.execute(
            text(
                "INSERT INTO attachments "
                "(id,origin_domain,deleted_at,purpose,content_sha256,"
                "scan_status,staging_object_key) "
                "VALUES (:id,:domain,:old,'attachment',:digest,'infected',:staging)"
            ),
            dict(
                id=identifier,
                domain=LOCAL_DOMAIN,
                old=now - timedelta(days=40),
                digest=str(identifier) * 64,
                staging="staging" if identifier == 4 else None,
            ),
        )
    await session.execute(
        text(
            "INSERT INTO attachments "
            "(id,origin_domain,purpose,content_sha256,scan_status,asset_binding) VALUES "
            "(101,:domain,'avatar',:a,'rejected','user:1'),(102,:domain,'avatar',:b,'clean',NULL),(103,:domain,'avatar',:c,'rejected',NULL)"
        ),
        dict(domain=LOCAL_DOMAIN, a="1" * 64, b="2" * 64, c="3" * 64),
    )
    config = settings()
    config.federation_event_retention_days = 30
    config.federation_clock_skew_seconds = 60
    assert await tombstones.cleanup_media_tombstone_sources(session, config, now=now) == 1
    await session.flush()
    assert set(
        await session.scalars(text("SELECT attachment_id FROM media_tombstone_sources"))
    ) == {1, 2, 4}
    assert set(await session.scalars(text("SELECT id FROM attachments"))) == {
        1,
        2,
        4,
        101,
        102,
        103,
    }
    await session.execute(
        text(
            "UPDATE attachments SET asset_binding=NULL,scan_status='rejected' WHERE id IN (101,102)"
        )
    )
    await session.execute(text("UPDATE attachments SET staging_object_key=NULL WHERE id=4"))
    assert await tombstones.cleanup_media_tombstone_sources(session, config, now=now) == 3
    await session.flush()
    assert await session.scalar(text("SELECT count(*) FROM media_tombstone_sources")) == 0
    assert set(await session.scalars(text("SELECT id FROM attachments"))) == {101, 102, 103}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scan_status", "expects_invalidation"),
    (("rejected", True), ("clean", False)),
)
async def test_terminal_tombstone_sweep_repairs_only_terminal_bound_assets(
    monkeypatch: pytest.MonkeyPatch,
    scan_status: str,
    expects_invalidation: bool,
    terminal_worker_database,
) -> None:
    from sqlalchemy import text

    connection, engine = terminal_worker_database
    await connection.execute(
        text(
            "INSERT INTO attachments "
            "(id,origin_domain,purpose,scan_status,content_sha256,asset_binding) VALUES "
            "(30,:local,'avatar',:status,:digest,'user:30'),(31,:local,'avatar','clean',:clean,'user:31'),(32,:local,'attachment','rejected',:unbound,NULL),(30,:remote,'avatar','rejected',:foreign,'user:30')"
        ),
        dict(
            local=LOCAL_DOMAIN,
            remote=REMOTE_DOMAIN,
            status=scan_status,
            digest=DIGEST,
            clean="b" * 64,
            unbound="c" * 64,
            foreign="d" * 64,
        ),
    )
    redis = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(tasks.Redis, "from_url", Mock(return_value=redis))
    prepared = tasks.TerminalAssetInvalidation()
    repaired_session = []

    async def invalidate(session, _settings, item):
        assert session.in_transaction()
        assert (item.id, item.origin_domain, item.asset_binding) == (30, LOCAL_DOMAIN, "user:30")
        repaired_session.append(session)
        return prepared

    invalidate_mock = AsyncMock(side_effect=invalidate)
    monkeypatch.setattr(tasks, "invalidate_terminal_asset_binding", invalidate_mock)
    tombstone = AsyncMock(return_value=set())
    monkeypatch.setattr(tasks, "queue_terminal_attachment_tombstone", tombstone)

    async def after_commit(*args):
        assert repaired_session and not repaired_session[0].in_transaction()

    enqueue = AsyncMock(side_effect=after_commit)
    publish = AsyncMock(side_effect=after_commit)
    monkeypatch.setattr(tasks, "enqueue_best_effort", enqueue)
    monkeypatch.setattr(tasks, "_publish_terminal_asset_invalidation", publish)
    assert await unwrap(tasks.media_terminal_tombstone_sweep.original_func)() == int(
        expects_invalidation
    )
    if expects_invalidation:
        invalidate_mock.assert_awaited_once()
        assert (tombstone.await_args.args[2].id, tombstone.await_args.args[2].origin_domain) == (
            30,
            LOCAL_DOMAIN,
        )
        assert tombstone.await_args.kwargs == {"force_authoritative": True}
        enqueue.assert_awaited_once_with(tasks.media_terminal_asset_digest_repair, DIGEST)
        publish.assert_awaited_once_with(repaired_session[0], redis, prepared)
        redis.aclose.assert_awaited_once()
    else:
        invalidate_mock.assert_not_awaited()
        tombstone.assert_not_awaited()
        enqueue.assert_not_awaited()
        publish.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_process_wakes_guild_asset_mutation_only_after_verdict_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        id=30,
        origin_domain=LOCAL_DOMAIN,
        message_id=None,
        message_domain=None,
        asset_binding=f"guild:{LOCAL_DOMAIN}:10:icon",
        content_sha256=DIGEST,
    )
    guild = Guild(
        id=10,
        origin_domain=LOCAL_DOMAIN,
        name="Paper Lantern",
        owner_id=20,
        owner_domain=LOCAL_DOMAIN,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=2,
        last_event_seq=1,
        sync_status="ready",
        unavailable=False,
    )
    user = User(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=2,
        profile_resolved=True,
    )

    class Session:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

        async def get(self, model: object, _key: object) -> object | None:
            if model is Attachment:
                return item
            if model is Message:
                return None
            raise AssertionError("unexpected model lookup")

        async def refresh(self, value: object) -> None:
            assert self.committed
            assert value is guild or value is user

    class Engine:
        dispose = AsyncMock()

    class Redis:
        aclose = AsyncMock()

    session = Session()
    engine = Engine()
    redis = Redis()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
        dragonfly_url=SimpleNamespace(get_secret_value=lambda: "redis://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: redis)
    prepared = tasks.TerminalAssetInvalidation(
        user=user,
        guild=guild,
        dispatch_type="GUILD_UPDATE",
        profile_destinations={REMOTE_DOMAIN},
    )
    invalidate = AsyncMock(return_value=prepared)
    monkeypatch.setattr(tasks, "invalidate_terminal_asset_binding", invalidate)
    monkeypatch.setattr(
        tasks,
        "queue_terminal_attachment_tombstone",
        AsyncMock(return_value=set()),
    )

    async def process(
        _session: object,
        _settings: object,
        _attachment_id: int,
        _origin_domain: str,
        *,
        before_terminal_commit: Any,
    ) -> str:
        await before_terminal_commit(item)
        assert not session.committed
        await session.commit()
        return "rejected"

    monkeypatch.setattr(tasks, "process_attachment_record", process)

    async def after_commit(*_args: object, **_kwargs: object) -> None:
        assert session.committed

    wake = AsyncMock(side_effect=after_commit)
    publish = AsyncMock(side_effect=after_commit)
    enqueue = AsyncMock(side_effect=after_commit)
    monkeypatch.setattr(tasks, "wake_queued_guild_federation", wake)
    monkeypatch.setattr(tasks, "publish_dispatch", publish)
    monkeypatch.setattr(tasks, "enqueue_best_effort", enqueue)

    result = await unwrap(tasks.media_process.original_func)(item.id, item.origin_domain)

    assert result == "rejected"
    invalidate.assert_awaited_once()
    wake.assert_awaited_once_with(guild)
    assert [item.args[2] for item in publish.await_args_list] == ["USER_UPDATE", "GUILD_UPDATE"]
    assert enqueue.await_args_list[0].args == (
        tasks.media_terminal_asset_digest_repair,
        DIGEST,
    )
    assert enqueue.await_args_list[1].args == (tasks.federation_deliver, REMOTE_DOMAIN)
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
