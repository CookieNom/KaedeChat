from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from app.api.bots import (
    bot_commit_webhook_avatar,
    bot_create_webhook,
    bot_create_webhook_avatar_ticket,
    bot_get_webhook,
    bot_guild_webhook,
    bot_list_webhooks,
)
from app.api.channels import (
    commit_local_message_deletion,
    load_webhook_capability_channel_access,
)
from app.api.webhooks import (
    WebhookExecute,
    WebhookPatch,
    WebhookTokenRecoveryError,
    apply_webhook_avatar,
    clear_webhook_avatar,
    delete_webhook_message,
    follower_webhook_payload,
    get_webhook,
    list_channel_webhooks,
    managed_webhook_payload,
    patch_follower_webhook,
    patch_webhook_with_token,
    publish_webhook_update,
    recover_webhook_token,
    require_webhook_message_thread,
    store_webhook_token,
    token_digest,
    validate_webhook_components_v2_body,
    validate_webhook_thread_target,
    webhook_bot_installation,
    webhook_execution_components,
    webhook_payload,
)
from app.api.webhooks import (
    list_webhooks as list_guild_webhooks,
)
from app.chat.channel_access import ChannelAccess
from app.chat.webhook_limits import require_webhook_capacity, webhook_capacity_counts
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.media.schemas import AssetCommitRequest


def webhook(*, avatar_hash: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        guild_id=11,
        guild_domain="chat.example",
        channel_id=13,
        channel_domain="chat.example",
        name="Build relay",
        avatar_hash=avatar_hash,
        revoked_at=None,
        type=1,
        application_id=None,
        application_domain=None,
        token_hash=token_digest("kwh_initial"),
        token_ciphertext=None,
    )


@pytest.mark.asyncio
async def test_bot_webhook_avatar_ticket_requires_attachment_write_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace()
    webhook_ref = EntityRef("7@chat.example")
    principal = SimpleNamespace()
    require_scope = Mock(
        side_effect=HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": "attachments.write"},
        )
    )
    monkeypatch.setattr(
        "app.api.bots.bot_guild_webhook",
        AsyncMock(
            return_value=(
                SimpleNamespace(),
                installation,
                webhook_ref,
                SimpleNamespace(),
            )
        ),
    )
    monkeypatch.setattr("app.api.bots.require_installation_scope", require_scope)
    issue_ticket = AsyncMock()
    monkeypatch.setattr("app.api.bots.create_webhook_avatar_ticket", issue_ticket)

    with pytest.raises(HTTPException) as denied:
        await bot_create_webhook_avatar_ticket(
            guild_ref=EntityRef("11@chat.example"),
            webhook_id=7,
            payload=SimpleNamespace(),
            response=SimpleNamespace(),
            principal=principal,
            session=SimpleNamespace(),
            redis=SimpleNamespace(),
            snowflake=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

    assert denied.value.detail == {
        "code": "BOT_SCOPE_REQUIRED",
        "scope": "attachments.write",
    }
    require_scope.assert_called_once_with(principal, installation, "attachments.write")
    issue_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_webhook_avatar_commit_rechecks_attachment_ownership_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace()
    webhook_ref = EntityRef("7@chat.example")
    principal = SimpleNamespace()
    session = SimpleNamespace()
    settings = SimpleNamespace()
    monkeypatch.setattr(
        "app.api.bots.bot_guild_webhook",
        AsyncMock(
            return_value=(
                SimpleNamespace(),
                installation,
                webhook_ref,
                SimpleNamespace(),
            )
        ),
    )
    require_owned = AsyncMock(
        side_effect=HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    )
    monkeypatch.setattr(
        "app.api.bots.require_owned_attachments_for_installation",
        require_owned,
    )
    commit_avatar = AsyncMock()
    monkeypatch.setattr("app.api.bots.commit_webhook_avatar", commit_avatar)
    payload = AssetCommitRequest(attachment_id="41")

    with pytest.raises(HTTPException) as denied:
        await bot_commit_webhook_avatar(
            guild_ref=EntityRef("11@chat.example"),
            webhook_id=7,
            payload=payload,
            response=SimpleNamespace(),
            principal=principal,
            session=session,
            redis=SimpleNamespace(),
            snowflake=SimpleNamespace(),
            settings=settings,
            reason=None,
        )

    assert denied.value.detail == {"code": "ATTACHMENT_NOT_FOUND"}
    require_owned.assert_awaited_once_with(
        session,
        settings,
        principal,
        installation,
        [41],
    )
    commit_avatar.assert_not_awaited()


def test_webhook_patch_cleans_name_and_requires_a_destination() -> None:
    payload = WebhookPatch(name="  Release relay  ", channel_id="15@chat.example")

    assert payload.name == "Release relay"
    assert str(payload.channel_id) == "15@chat.example"
    with pytest.raises(ValidationError, match="channel_id cannot be null"):
        WebhookPatch.model_validate({"channel_id": None})
    with pytest.raises(ValidationError, match="name cannot be null"):
        WebhookPatch.model_validate({"name": None})
    assert WebhookPatch.model_validate({}).model_fields_set == set()


def test_webhook_execution_rejects_boolean_message_flags() -> None:
    with pytest.raises(ValidationError, match="flags must be an integer"):
        WebhookExecute.model_validate({"content": "build complete", "flags": True})


def test_token_bearing_webhook_payload_includes_authority_execution_url() -> None:
    production = webhook_payload(
        webhook(),
        token="kwh_secret",
        settings=SimpleNamespace(environment="production"),
    )
    development = webhook_payload(
        webhook(),
        token="kwh_secret",
        settings=SimpleNamespace(environment="test"),
    )

    assert production["execution_url"] == ("https://chat.example/api/v1/webhooks/7/kwh_secret")
    assert development["execution_url"] == ("http://chat.example/api/v1/webhooks/7/kwh_secret")


def test_recoverable_webhook_token_is_hash_bound_and_resource_context_bound() -> None:
    settings = SimpleNamespace(
        environment="production",
        secret_key_bytes=bytes(range(32)),
    )
    item = webhook()
    store_webhook_token(item, "kwh_secret", settings)  # type: ignore[arg-type]

    assert item.token_hash == token_digest("kwh_secret")
    assert item.token_ciphertext != b"kwh_secret"
    assert recover_webhook_token(item, settings) == "kwh_secret"  # type: ignore[arg-type]

    original_hash = item.token_hash
    original_ciphertext = item.token_ciphertext
    store_webhook_token(item, "kwh_rotated", settings)  # type: ignore[arg-type]
    assert item.token_hash == token_digest("kwh_rotated")
    assert item.token_hash != original_hash
    assert item.token_ciphertext != original_ciphertext
    assert recover_webhook_token(item, settings) == "kwh_rotated"  # type: ignore[arg-type]

    wrong_resource = webhook()
    wrong_resource.id = 8
    wrong_resource.token_hash = item.token_hash
    wrong_resource.token_ciphertext = item.token_ciphertext
    with pytest.raises(WebhookTokenRecoveryError):
        recover_webhook_token(wrong_resource, settings)  # type: ignore[arg-type]

    tampered = webhook()
    tampered.token_hash = item.token_hash
    tampered.token_ciphertext = item.token_ciphertext[:-1] + bytes([item.token_ciphertext[-1] ^ 1])
    with pytest.raises(WebhookTokenRecoveryError):
        recover_webhook_token(tampered, settings)  # type: ignore[arg-type]


def test_only_explicit_management_capability_discloses_recoverable_webhook_url() -> None:
    settings = SimpleNamespace(
        environment="production",
        secret_key_bytes=bytes(range(32)),
    )
    item = webhook()
    store_webhook_token(item, "kwh_secret", settings)  # type: ignore[arg-type]

    manager = managed_webhook_payload(
        item,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        recover_token=True,
    )
    read_only = managed_webhook_payload(
        item,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
    )
    public = webhook_payload(item)  # type: ignore[arg-type]

    assert manager["execution_url"] == ("https://chat.example/api/v1/webhooks/7/kwh_secret")
    assert manager["token_recovery_required"] is False
    assert "execution_url" not in read_only and "token" not in read_only
    assert "execution_url" not in public and "token" not in public

    legacy = webhook()
    legacy_projection = managed_webhook_payload(
        legacy,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        recover_token=True,
    )
    assert legacy_projection["token_recovery_required"] is True
    assert "execution_url" not in legacy_projection


@pytest.mark.asyncio
async def test_remote_guild_webhook_get_preserves_authority_execution_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "id": "7",
        "origin_domain": "guild.example",
        "execution_url": "https://guild.example/api/v1/webhooks/7/kwh_secret",
    }
    target = AsyncMock(return_value=(7, SimpleNamespace(body=expected)))
    monkeypatch.setattr("app.api.webhooks._webhook_management_target", target)

    rendered = await get_webhook(
        webhook_id=EntityRef("7@guild.example"),
        guild_ref=EntityRef("11@guild.example"),
        auth=SimpleNamespace(user=SimpleNamespace(account_type="human")),  # type: ignore[arg-type]
        session=SimpleNamespace(),  # type: ignore[arg-type]
        redis=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="home.example"),  # type: ignore[arg-type]
    )

    assert rendered == expected


def test_application_webhooks_always_honor_components() -> None:
    payload = WebhookExecute.model_validate(
        {
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 1,
                            "label": "Approve",
                            "custom_id": "approve",
                        }
                    ],
                }
            ]
        }
    )

    assert (
        webhook_execution_components(
            payload,
            application_owned=True,
            with_components=False,
        )
        == payload.components
    )
    assert (
        webhook_execution_components(
            payload,
            application_owned=False,
            with_components=False,
        )
        == []
    )
    assert (
        webhook_execution_components(
            payload,
            application_owned=False,
            with_components=True,
        )
        == payload.components
    )


def test_components_v2_webhook_execute_rejects_attachments() -> None:
    with pytest.raises(HTTPException) as raised:
        validate_webhook_components_v2_body(
            flags=1 << 15,
            content=None,
            embeds=[],
            components=[{"type": 10, "content": "Release ready"}],
            attachment_ids=[41],
            poll=None,
            sticker_ids=[],
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == {"code": "COMPONENTS_V2_BODY_INVALID"}


@pytest.mark.parametrize(
    ("channel_type", "has_thread_id", "thread_name", "applied_tags", "code"),
    [
        (0, True, "new", [], "WEBHOOK_THREAD_TARGET_AMBIGUOUS"),
        (15, False, None, [], "WEBHOOK_THREAD_NAME_REQUIRED"),
        (0, False, "new", [], "WEBHOOK_THREAD_NAME_UNEXPECTED"),
        (0, False, None, [5], "WEBHOOK_APPLIED_TAGS_UNEXPECTED"),
        (15, True, None, [5], "WEBHOOK_APPLIED_TAGS_UNEXPECTED"),
    ],
)
def test_webhook_thread_fields_match_the_destination_contract(
    channel_type: int,
    has_thread_id: bool,
    thread_name: str | None,
    applied_tags: list[int],
    code: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        validate_webhook_thread_target(
            channel_type=channel_type,
            has_thread_id=has_thread_id,
            thread_name=thread_name,
            applied_tags=applied_tags,
        )

    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == code


@pytest.mark.asyncio
async def test_webhook_capacity_counts_incoming_and_both_follower_kinds() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[10, 4, 20, 3, 30, 2]),
    )
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    channel = SimpleNamespace(id=13, origin_domain="chat.example")

    counts = await webhook_capacity_counts(
        session,  # type: ignore[arg-type]
        guild,  # type: ignore[arg-type]
        channel,  # type: ignore[arg-type]
    )

    assert counts == (60, 9)
    statements = [str(call.args[0]) for call in session.scalar.await_args_list]
    assert sum("webhooks" in statement for statement in statements) == 2
    assert sum("channel_follows" in statement for statement in statements) == 4
    assert any("local_role" in statement for statement in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counts", "code"),
    [
        ((999, 15), "WEBHOOK_CHANNEL_LIMIT_REACHED"),
        ((1_000, 14), "WEBHOOK_GUILD_LIMIT_REACHED"),
    ],
)
async def test_webhook_capacity_enforces_documented_limits(
    monkeypatch: pytest.MonkeyPatch,
    counts: tuple[int, int],
    code: str,
) -> None:
    lock = AsyncMock()
    monkeypatch.setattr("app.chat.webhook_limits.lock_webhook_capacity_guild", lock)
    monkeypatch.setattr(
        "app.chat.webhook_limits.webhook_capacity_counts",
        AsyncMock(return_value=counts),
    )

    with pytest.raises(HTTPException) as limited:
        await require_webhook_capacity(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            adding_to_guild=True,
        )

    assert limited.value.status_code == 409
    assert limited.value.detail["code"] == code
    lock.assert_awaited_once()


def test_webhook_message_thread_query_is_authoritatively_bound() -> None:
    message = SimpleNamespace(channel_id=22, channel_domain="chat.example")
    settings = SimpleNamespace(domain="home.example")

    require_webhook_message_thread(
        message,  # type: ignore[arg-type]
        EntityRef("22@chat.example"),
        settings,  # type: ignore[arg-type]
    )
    with pytest.raises(HTTPException) as missing:
        require_webhook_message_thread(
            message,  # type: ignore[arg-type]
            EntityRef("23@chat.example"),
            settings,  # type: ignore[arg-type]
        )
    assert missing.value.detail["code"] == "WEBHOOK_MESSAGE_NOT_FOUND"


@pytest.mark.asyncio
async def test_remote_bot_webhook_resolves_installation_storage_owner() -> None:
    item = webhook()
    actor = SimpleNamespace(
        id=90,
        origin_domain="apps.example",
        account_type="bot",
    )
    installation = SimpleNamespace(id=71)
    session = SimpleNamespace(scalar=AsyncMock(return_value=installation))

    assert (
        await webhook_bot_installation(
            session,  # type: ignore[arg-type]
            item,  # type: ignore[arg-type]
            actor,  # type: ignore[arg-type]
        )
        is installation
    )
    statement = str(session.scalar.await_args.args[0])
    assert "bot_installations.guild_domain" in statement
    assert "bot_installations.bot_user_domain" in statement


@pytest.mark.asyncio
async def test_bot_webhook_lookup_includes_local_type_two_followers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    installation = SimpleNamespace(
        id=70,
        guild_id=11,
        guild_domain="chat.example",
        channel_restrictions=[],
    )
    follow = SimpleNamespace(
        id=7,
        target_channel_id=13,
        target_channel_domain="chat.example",
    )
    channel = SimpleNamespace(
        id=13,
        origin_domain="chat.example",
        guild_id=11,
        guild_domain="chat.example",
        unavailable=False,
        parent_id=None,
        parent_domain=None,
    )
    monkeypatch.setattr(
        "app.api.bots.installation_for_guild_any_scope",
        AsyncMock(return_value=(guild, installation)),
    )
    monkeypatch.setattr(
        "app.api.bots.target_follower_webhook",
        AsyncMock(return_value=follow),
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[None, channel]))

    resolved_guild, resolved, webhook_ref, resolved_channel = await bot_guild_webhook(
        session,  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        EntityRef("11@chat.example"),
        7,
        manage=True,
    )

    assert resolved_guild is guild
    assert resolved is installation
    assert webhook_ref == EntityRef("7@chat.example")
    assert resolved_channel is channel


@pytest.mark.asyncio
async def test_follower_patch_clears_bound_avatar_and_queues_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow = SimpleNamespace(
        id=7,
        name="Release relay",
        avatar_hash="a" * 64,
        active=True,
        target_channel_id=13,
        target_channel_domain="chat.example",
        source_channel_id=17,
        source_channel_domain="source.example",
    )
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    channel = SimpleNamespace(id=13, origin_domain="chat.example")
    actor = SimpleNamespace(id=3, origin_domain="chat.example", account_type="human")
    auth = SimpleNamespace(user=actor)
    previous = SimpleNamespace(id=41, origin_domain="chat.example")
    session = SimpleNamespace(commit=AsyncMock())
    clear = AsyncMock(
        side_effect=lambda _session, item: setattr(item, "avatar_hash", None) or previous
    )
    purge = AsyncMock()
    monkeypatch.setattr(
        "app.api.webhooks.locked_follower_webhook_management",
        AsyncMock(return_value=(follow, guild, channel)),
    )
    monkeypatch.setattr("app.api.webhooks.clear_follower_avatar", clear)
    monkeypatch.setattr(
        "app.api.webhooks.follower_webhook_payload",
        AsyncMock(return_value={"id": "7", "name": "Release relay", "avatar_hash": None}),
    )
    monkeypatch.setattr("app.api.webhooks.add_audit_entry", AsyncMock())
    monkeypatch.setattr("app.api.webhooks.publish_follower_webhook_update", AsyncMock())
    monkeypatch.setattr("app.api.webhooks.enqueue_best_effort", purge)

    rendered = await patch_follower_webhook(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        auth,  # type: ignore[arg-type]
        follow,  # type: ignore[arg-type]
        guild,  # type: ignore[arg-type]
        channel,  # type: ignore[arg-type]
        WebhookPatch(avatar_hash=None),
        reason="cleanup",
    )

    assert rendered["avatar_hash"] is None
    clear.assert_awaited_once_with(session, follow)
    assert purge.await_args.args[1:] == (41, "chat.example")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manage", "scopes"),
    [
        (False, ("webhooks.read", "webhooks.manage")),
        (True, ("webhooks.manage",)),
    ],
)
async def test_bot_webhook_lookup_uses_read_scope_for_reads_only(
    monkeypatch: pytest.MonkeyPatch,
    manage: bool,
    scopes: tuple[str, ...],
) -> None:
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    installation = SimpleNamespace(
        id=70,
        guild_id=11,
        guild_domain="chat.example",
        channel_restrictions=[],
    )
    installer = AsyncMock(return_value=(guild, installation))
    monkeypatch.setattr("app.api.bots.installation_for_guild_any_scope", installer)
    item = webhook()
    channel = SimpleNamespace(
        id=13,
        origin_domain="chat.example",
        guild_id=11,
        guild_domain="chat.example",
        unavailable=False,
        parent_id=None,
        parent_domain=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda model, _ref: item if model.__name__ == "Webhook" else channel
        )
    )

    resolved_guild, resolved_installation, webhook_ref, resolved_channel = await bot_guild_webhook(
        session,  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        EntityRef("11@chat.example"),
        7,
        manage=manage,
    )

    assert installer.await_args.args[4:] == scopes
    assert resolved_guild is guild
    assert resolved_installation is installation
    assert webhook_ref == EntityRef("7@chat.example")
    assert resolved_channel is channel


@pytest.mark.asyncio
async def test_bot_webhook_get_passes_the_authority_qualified_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace(granted_scopes=("webhooks.read",))
    lookup = AsyncMock(
        return_value=(
            SimpleNamespace(),
            installation,
            EntityRef("7@guild.example"),
            SimpleNamespace(),
        )
    )
    get = AsyncMock(return_value={"id": "7", "origin_domain": "guild.example"})
    monkeypatch.setattr("app.api.bots.bot_guild_webhook", lookup)
    monkeypatch.setattr("app.api.bots.get_webhook", get)
    principal = SimpleNamespace(user=SimpleNamespace(), scopes=("webhooks.read",))

    result = await bot_get_webhook(
        EntityRef("11@guild.example"),
        7,
        principal,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(domain="home.example"),  # type: ignore[arg-type]
    )

    assert result["origin_domain"] == "guild.example"
    assert get.await_args.args[0] == EntityRef("7@guild.example")
    assert get.await_args.args[1] == EntityRef("11@guild.example")


@pytest.mark.asyncio
async def test_bot_manage_create_receives_fresh_execution_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = {
        "id": "7",
        "token": "secret",
        "execution_url": "https://chat.example/api/v1/webhooks/7/secret",
    }
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    installation = SimpleNamespace()
    installer = AsyncMock(return_value=(guild, installation))
    create = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.bots.installation_for_guild", installer)
    monkeypatch.setattr(
        "app.api.bots._require_bot_requested_channel",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.bots.create_webhook", create)
    principal = SimpleNamespace(user=SimpleNamespace())

    result = await bot_create_webhook(
        EntityRef("11@chat.example"),
        EntityRef("13@chat.example"),
        SimpleNamespace(name="relay"),  # type: ignore[arg-type]
        principal,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert result == created
    installer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_scopes", "granted_scopes", "recover"),
    [
        (("webhooks.read",), ("webhooks.manage",), False),
        (("webhooks.manage",), ("webhooks.manage",), True),
        (("webhooks.manage",), ("webhooks.read",), False),
    ],
)
async def test_bot_webhook_list_secret_recovery_requires_both_manage_grants(
    monkeypatch: pytest.MonkeyPatch,
    runtime_scopes: tuple[str, ...],
    granted_scopes: tuple[str, ...],
    recover: bool,
) -> None:
    installation = SimpleNamespace(granted_scopes=granted_scopes)
    installer = AsyncMock(return_value=(SimpleNamespace(), installation))
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.bots.installation_for_guild_any_scope", installer)
    monkeypatch.setattr("app.api.bots.list_webhooks", listing)
    principal = SimpleNamespace(
        user=SimpleNamespace(),
        scopes=frozenset(runtime_scopes),
    )

    await bot_list_webhooks(
        EntityRef("11@chat.example"),
        principal,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert listing.await_args.args[-1] is recover


@pytest.mark.asyncio
async def test_bot_guild_webhook_list_filters_restricted_or_hidden_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = webhook()
    blocked = webhook()
    blocked.id = 8
    blocked.channel_id = 14
    allowed_follower = SimpleNamespace(
        id=9,
        target_channel_id=13,
        target_channel_domain="chat.example",
    )
    blocked_follower = SimpleNamespace(
        id=10,
        target_channel_id=14,
        target_channel_domain="chat.example",
    )
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    channels = {
        (13, "chat.example"): SimpleNamespace(
            id=13,
            origin_domain="chat.example",
            guild_id=11,
            guild_domain="chat.example",
            unavailable=False,
        ),
        (14, "chat.example"): SimpleNamespace(
            id=14,
            origin_domain="chat.example",
            guild_id=11,
            guild_domain="chat.example",
            unavailable=False,
        ),
    }
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[allowed, blocked], [], []]),
        get=AsyncMock(side_effect=lambda _model, ref: channels.get(ref)),
    )
    actor = SimpleNamespace(
        id=3,
        origin_domain="apps.example",
        account_type="bot",
    )
    monkeypatch.setattr(
        "app.api.webhooks.local_guild",
        AsyncMock(return_value=guild),
    )
    monkeypatch.setattr(
        "app.api.webhooks.target_follower_webhooks",
        AsyncMock(return_value=[allowed_follower, blocked_follower]),
    )
    follower_payload = AsyncMock(
        side_effect=lambda _session, _redis, _actor, item: {
            "id": str(item.id),
            "channel_id": str(item.target_channel_id),
            "channel_domain": item.target_channel_domain,
        }
    )
    monkeypatch.setattr(
        "app.api.webhooks.follower_webhook_payload",
        follower_payload,
    )
    permission_check = AsyncMock()
    monkeypatch.setattr("app.api.webhooks.require_permissions", permission_check)
    monkeypatch.setattr(
        "app.api.webhooks.get_permissions",
        AsyncMock(
            side_effect=lambda *_args, channel=None, **_kwargs: (
                int(Permission.MANAGE_WEBHOOKS) if channel.id == 13 else 0
            )
        ),
    )

    rendered = await list_guild_webhooks(
        EntityRef("11@chat.example"),
        SimpleNamespace(user=actor),
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        False,
    )

    assert [item["id"] for item in rendered] == ["7", "9"]
    follower_payload.assert_awaited_once()
    assert follower_payload.await_args.args[-1] is allowed_follower
    permission_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_authenticated_get_and_channel_list_recheck_channel_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    channel = SimpleNamespace(id=13, origin_domain="chat.example")
    auth = SimpleNamespace(user=SimpleNamespace(id=3, origin_domain="chat.example"))
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=item),
        scalars=AsyncMock(side_effect=[[item], [], []]),
    )
    permission_check = AsyncMock()
    monkeypatch.setattr("app.api.webhooks.local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr("app.api.webhooks.guild_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr("app.api.webhooks.require_permissions", permission_check)

    fetched = await get_webhook(
        webhook_id=EntityRef("7@chat.example"),
        guild_ref=None,
        auth=auth,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        redis=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )
    listed = await list_channel_webhooks(
        EntityRef("11@chat.example"),
        EntityRef("13@chat.example"),
        auth,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert fetched["id"] == "7"
    assert [entry["id"] for entry in listed] == ["7"]
    assert permission_check.await_count == 2
    assert all(call.kwargs["channel"] is channel for call in permission_check.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_visible", [True, False])
async def test_follower_webhook_payload_is_type_two_and_hides_inaccessible_source(
    monkeypatch: pytest.MonkeyPatch,
    source_visible: bool,
) -> None:
    follow = SimpleNamespace(
        id=71,
        source_channel_id=17,
        source_channel_domain="source.example",
        target_channel_id=13,
        target_channel_domain="chat.example",
        creator_id=3,
        creator_domain="chat.example",
        active=True,
    )
    source_channel = SimpleNamespace(
        id=17,
        origin_domain="source.example",
        guild_id=19,
        guild_domain="source.example",
        name="releases",
        unavailable=False,
    )
    source_guild = SimpleNamespace(
        id=19,
        origin_domain="source.example",
        name="Upstream",
        icon_hash="a" * 64,
        unavailable=False,
    )
    creator = SimpleNamespace(
        id=3,
        origin_domain="chat.example",
        username="alice",
        display_name="Alice",
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        e2ee_device_generation=0,
        profile_resolved=True,
        account_type="human",
    )
    target_channel = SimpleNamespace(
        id=13,
        origin_domain="chat.example",
        guild_id=11,
        guild_domain="chat.example",
        unavailable=False,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[source_channel, source_guild, creator, target_channel])
    )
    permissions = AsyncMock(return_value=int(Permission.VIEW_CHANNEL) if source_visible else 0)
    monkeypatch.setattr("app.api.webhooks.get_permissions", permissions)

    rendered = await follower_webhook_payload(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        creator,  # type: ignore[arg-type]
        follow,  # type: ignore[arg-type]
    )

    assert rendered["type"] == 2
    assert rendered["guild_id"] == "11"
    assert rendered["channel_id"] == "13"
    assert rendered["name"] == "Upstream"
    assert "token" not in rendered
    assert ("source_guild" in rendered) is source_visible
    assert ("source_channel" in rendered) is source_visible


@pytest.mark.asyncio
async def test_clean_webhook_avatar_rebinds_and_updates_historical_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_hash = "a" * 64
    new_hash = "b" * 64
    item = webhook(avatar_hash=old_hash)
    attachment = SimpleNamespace(
        id=41,
        origin_domain="chat.example",
        asset_binding="webhook-avatar-stage:7:41",
        scan_status="clean",
        content_sha256=new_hash,
        detected_content_type="image/png",
    )
    previous = SimpleNamespace(id=40, origin_domain="chat.example")
    session = SimpleNamespace(execute=AsyncMock())
    bind = AsyncMock(return_value=previous)
    monkeypatch.setattr("app.api.webhooks.finalize_attachment", AsyncMock(return_value=attachment))
    monkeypatch.setattr("app.api.webhooks.bind_asset", bind)
    monkeypatch.setattr("app.api.webhooks.require_image_type", lambda _: None)

    rendered, replaced, previous_hash = await apply_webhook_avatar(
        session,  # type: ignore[arg-type]
        Response(),
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        SimpleNamespace(
            id=3,
            origin_domain="chat.example",
            account_type="human",
            is_local=True,
        ),  # type: ignore[arg-type]
        AssetCommitRequest(attachment_id="41"),
    )

    assert rendered["avatar_hash"] == new_hash
    assert replaced is previous
    assert previous_hash == old_hash
    assert item.avatar_hash == new_hash
    assert attachment.asset_binding is None
    assert bind.await_args.args[2] == "webhook:chat.example:7:avatar"
    statement = str(session.execute.await_args.args[0])
    assert "messages.webhook_id" in statement
    assert "messages.webhook_avatar_hash" in statement


@pytest.mark.asyncio
async def test_pending_webhook_avatar_is_durable_and_queues_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    attachment = SimpleNamespace(
        id=41,
        origin_domain="chat.example",
        asset_binding="webhook-avatar-stage:7:41",
        scan_status="pending",
    )
    session = SimpleNamespace(commit=AsyncMock())
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhooks.finalize_attachment", AsyncMock(return_value=attachment))
    monkeypatch.setattr("app.api.webhooks.attachment_payload", lambda value: {"id": str(value.id)})
    monkeypatch.setattr("app.api.webhooks.enqueue_best_effort", enqueue)

    response = Response()
    rendered, previous, old_hash = await apply_webhook_avatar(
        session,  # type: ignore[arg-type]
        response,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        SimpleNamespace(
            id=3,
            origin_domain="chat.example",
            account_type="human",
            is_local=True,
        ),  # type: ignore[arg-type]
        AssetCommitRequest(attachment_id="41"),
    )

    assert response.status_code == 202
    assert rendered == {"status": "processing", "attachment": {"id": "41"}}
    assert previous is None
    assert old_hash is None
    session.commit.assert_awaited_once()
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_clearing_avatar_unbinds_media_and_historical_defaults() -> None:
    item = webhook(avatar_hash="a" * 64)
    attachment = SimpleNamespace(asset_binding="webhook:chat.example:7:avatar")
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=attachment),
        execute=AsyncMock(),
    )

    result = await clear_webhook_avatar(session, item)  # type: ignore[arg-type]

    assert result is attachment
    assert item.avatar_hash is None
    assert attachment.asset_binding is None
    assert "messages.webhook_avatar_hash" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_channel_move_publishes_source_and_destination_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    item.channel_id = 15
    publish = AsyncMock()
    monkeypatch.setattr("app.api.webhooks.publish_dispatch", publish)

    await publish_webhook_update(SimpleNamespace(), item, previous_channel_id=13)  # type: ignore[arg-type]

    assert publish.await_count == 2
    assert publish.await_args_list[0].args[2:] == (
        "WEBHOOKS_UPDATE",
        {
            "guild_id": "11",
            "guild_domain": "chat.example",
            "channel_id": "13",
            "channel_domain": "chat.example",
        },
    )
    assert publish.await_args_list[1].args[3]["channel_id"] == "15"


@pytest.mark.asyncio
async def test_token_authenticated_patch_cannot_move_a_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=webhook()))

    with pytest.raises(HTTPException) as denied:
        await patch_webhook_with_token(
            7,
            "kwh_secret",
            WebhookPatch(channel_id="15@chat.example"),
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "WEBHOOK_TOKEN_CANNOT_MOVE_CHANNEL"


@pytest.mark.asyncio
async def test_shared_message_deletion_converges_thread_media_and_federation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    thread = SimpleNamespace(
        id=13,
        origin_domain="chat.example",
        type=11,
        starter_message_id=80,
        starter_message_domain="chat.example",
        last_message_id=90,
        last_message_domain="chat.example",
        message_count=3,
    )
    message = SimpleNamespace(
        id=90,
        origin_domain="chat.example",
        content="build complete",
        e2ee={"ciphertext": "old"},
        deleted_at=None,
    )
    actor = SimpleNamespace(id=3, origin_domain="chat.example")
    attachment = SimpleNamespace(id=41, origin_domain="chat.example")
    session = SimpleNamespace(
        execute=AsyncMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )
    refresh_last = AsyncMock(
        side_effect=lambda _session, channel: (
            setattr(channel, "last_message_id", 80),
            setattr(channel, "last_message_domain", "chat.example"),
        )
    )
    tombstones = AsyncMock(return_value=([attachment], {"remote.example"}))
    queue_mutation = AsyncMock()
    wake = AsyncMock()
    publish = AsyncMock()
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.channels.refresh_thread_last_message_after_delete", refresh_last)
    monkeypatch.setattr("app.api.channels.queue_attachment_tombstones", tombstones)
    monkeypatch.setattr("app.api.channels.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr("app.api.channels.wake_queued_guild_federation", wake)
    monkeypatch.setattr("app.api.channels.publish_channel_dispatch", publish)
    monkeypatch.setattr("app.api.channels.enqueue_best_effort", enqueue)
    monkeypatch.setattr(
        "app.api.channels.federation_channel_state", lambda channel: {"id": str(channel.id)}
    )
    monkeypatch.setattr("app.api.channels.channel_payload", lambda channel: {"id": str(channel.id)})

    changed = await commit_local_message_deletion(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        ChannelAccess(channel=thread, guild=guild, participants=[]),  # type: ignore[arg-type]
        actor,  # type: ignore[arg-type]
        message,  # type: ignore[arg-type]
    )

    assert changed is True
    assert message.content is None
    assert message.e2ee is None
    assert message.deleted_at is not None
    assert thread.message_count == 2
    assert (thread.last_message_id, thread.last_message_domain) == (80, "chat.example")
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(thread, attribute_names=("updated_at",))
    refresh_last.assert_awaited_once()
    tombstones.assert_awaited_once()
    assert [call.args[4] for call in queue_mutation.await_args_list] == [
        "guild.message.delete",
        "guild.channel.update",
    ]
    session.commit.assert_awaited_once()
    wake.assert_awaited_once_with(guild)
    assert [call.args[2] for call in publish.await_args_list] == [
        "MESSAGE_DELETE",
        "THREAD_UPDATE",
    ]
    assert enqueue.await_count == 2


@pytest.mark.asyncio
async def test_webhook_capability_access_is_bound_to_its_channel_or_child_thread() -> None:
    child = SimpleNamespace(
        id=20,
        origin_domain="chat.example",
        type=11,
        parent_id=13,
        parent_domain="chat.example",
        guild_id=11,
        guild_domain="chat.example",
    )
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[child, guild]))

    access = await load_webhook_capability_channel_access(
        session,  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        EntityRef("20@chat.example"),
        webhook_channel_id=13,
        webhook_channel_domain="chat.example",
    )

    assert access.channel is child
    assert access.guild is guild

    child.parent_id = 99
    denied_session = SimpleNamespace(scalar=AsyncMock(return_value=child))
    with pytest.raises(HTTPException) as denied:
        await load_webhook_capability_channel_access(
            denied_session,  # type: ignore[arg-type]
            SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
            EntityRef("20@chat.example"),
            webhook_channel_id=13,
            webhook_channel_domain="chat.example",
        )
    assert denied.value.status_code == 404
    assert denied.value.detail["code"] == "WEBHOOK_DESTINATION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_webhook_message_delete_uses_capability_and_shared_tombstone_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = webhook()
    item.creator_id = 3
    item.creator_domain = "chat.example"
    preview = SimpleNamespace(
        id=90,
        origin_domain="chat.example",
        channel_id=13,
        channel_domain="chat.example",
        webhook_id=7,
        webhook_domain="chat.example",
        deleted_at=None,
    )
    locked = SimpleNamespace(**vars(preview))
    guild = SimpleNamespace(id=11, origin_domain="chat.example")
    channel = SimpleNamespace(
        id=13,
        origin_domain="chat.example",
        guild_id=11,
        guild_domain="chat.example",
        unavailable=False,
    )
    creator = SimpleNamespace(id=3, origin_domain="chat.example")
    session = SimpleNamespace(get=AsyncMock(side_effect=[channel, creator]))
    token = AsyncMock(return_value=item)
    token_message = AsyncMock(return_value=preview)
    lock_access = AsyncMock(
        return_value=ChannelAccess(channel=channel, guild=guild, participants=[])
    )
    lock_target = AsyncMock(return_value=locked)
    commit = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhooks.token_webhook", token)
    monkeypatch.setattr("app.api.webhooks.token_webhook_message", token_message)
    monkeypatch.setattr("app.api.webhooks.local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr("app.api.webhooks.lock_message_delete_access", lock_access)
    monkeypatch.setattr("app.api.webhooks.lock_message_delete_target", lock_target)
    monkeypatch.setattr("app.api.webhooks.commit_local_message_deletion", commit)

    response = await delete_webhook_message(
        webhook_id=7,
        path_token="secret",
        message_id="90@chat.example",  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        redis=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    assert token.await_args.kwargs["for_update"] is True
    assert token_message.await_args.kwargs.get("for_update") is None
    lock_access.assert_awaited_once()
    lock_target.assert_awaited_once()
    commit.assert_awaited_once()
