from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.bots as bots_api
import app.api.moderation as moderation_api
from app.chat.audit_access import filter_restricted_bot_audit_entries
from app.chat.audit_payloads import (
    REDACTED_AUDIT_VALUE,
    AuditLogEntryPayload,
    audit_log_payload,
)
from app.chat.permissions import BotGuildPermissionGrant
from app.chat.schemas import OverwritePut
from app.core.types import EntityRef
from app.db.models import Channel


def test_audit_log_payload_is_typed_and_redacts_nested_credentials() -> None:
    entry = SimpleNamespace(
        id=9,
        guild_id=10,
        guild_domain="chat.example",
        actor_id=11,
        actor_domain="chat.example",
        action_type=51,
        target_type="webhook",
        target_ref={"id": "12", "webhook_token": "do-not-disclose"},
        reason="rotated after a leak",
        changes=[
            {
                "key": "configuration",
                "old_value": {"token": "old", "name": "builds"},
                "new_value": {"secret": "new", "name": "deploys"},
            }
        ],
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )

    payload = audit_log_payload(entry)

    assert payload.target_ref == {"id": "12", "webhook_token": REDACTED_AUDIT_VALUE}
    assert payload.changes[0].old_value == {
        "token": REDACTED_AUDIT_VALUE,
        "name": "builds",
    }
    assert payload.changes[0].new_value == {
        "secret": REDACTED_AUDIT_VALUE,
        "name": "deploys",
    }
    serialized_change = payload.changes[0].model_dump(exclude_unset=True)
    assert "added" not in serialized_change
    assert "removed" not in serialized_change


def typed_audit_entry(
    entry_id: int,
    target_type: str,
    target_ref: dict[str, object],
    *,
    changes: list[dict[str, object]] | None = None,
) -> AuditLogEntryPayload:
    return AuditLogEntryPayload.model_validate(
        {
            "id": str(entry_id),
            "guild_id": "10",
            "guild_domain": "chat.example",
            "actor_id": "1",
            "actor_domain": "chat.example",
            "action_type": 11,
            "target_type": target_type,
            "target_ref": target_ref,
            "reason": None,
            "changes": changes or [],
            "created_at": "2026-08-29T00:00:00Z",
        }
    )


@pytest.mark.asyncio
async def test_restricted_bot_audit_filter_hides_blocked_channels_and_partial_order_changes() -> (
    None
):
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    channels = {
        (20, "chat.example"): SimpleNamespace(
            id=20,
            origin_domain="chat.example",
            guild_id=10,
            guild_domain="chat.example",
            parent_id=None,
            parent_domain=None,
            unavailable=False,
        ),
        (21, "chat.example"): SimpleNamespace(
            id=21,
            origin_domain="chat.example",
            guild_id=10,
            guild_domain="chat.example",
            parent_id=None,
            parent_domain=None,
            unavailable=False,
        ),
    }

    async def get(model: object, key: object) -> object | None:
        assert model is Channel
        return channels.get(key)

    entries = [
        typed_audit_entry(1, "channel", {"id": "20", "name": "allowed"}),
        typed_audit_entry(2, "channel", {"id": "21", "name": "secret"}),
        typed_audit_entry(
            3,
            "channel_order",
            {"guild_id": "10"},
            changes=[
                {
                    "key": "20@chat.example",
                    "old_value": {"position": 1, "parent_id": None},
                    "new_value": {"position": 0, "parent_id": None},
                },
                {
                    "key": "21@chat.example",
                    "old_value": {"position": 0, "parent_id": None},
                    "new_value": {"position": 1, "parent_id": None},
                },
            ],
        ),
        typed_audit_entry(4, "role", {"id": "30", "name": "Moderator"}),
    ]

    filtered = await filter_restricted_bot_audit_entries(
        SimpleNamespace(get=AsyncMock(side_effect=get)),
        guild,
        BotGuildPermissionGrant(99, 1, 0, ("20@chat.example",)),
        entries,
    )

    assert [entry.id for entry in filtered] == ["1", "3", "4"]
    assert [change.key for change in filtered[1].changes] == ["20@chat.example"]
    assert "secret" not in str([entry.model_dump(mode="json") for entry in filtered])


@pytest.mark.asyncio
async def test_restricted_bot_audit_filter_fails_closed_for_unresolved_channel_resource() -> None:
    entry = typed_audit_entry(1, "webhook", {"id": "55", "name": "secret hook"})
    session = SimpleNamespace(get=AsyncMock(return_value=None))

    filtered = await filter_restricted_bot_audit_entries(
        session,
        SimpleNamespace(id=10, origin_domain="chat.example"),
        BotGuildPermissionGrant(99, 1, 0, ("20@chat.example",)),
        [entry],
    )

    assert filtered == []


@pytest.mark.asyncio
async def test_restricted_bot_audit_filter_includes_category_thread_descendant() -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    category = SimpleNamespace(
        id=20,
        origin_domain="chat.example",
        guild_id=10,
        guild_domain="chat.example",
        parent_id=None,
        parent_domain=None,
        unavailable=False,
        type=4,
    )
    forum = SimpleNamespace(
        id=21,
        origin_domain="chat.example",
        guild_id=10,
        guild_domain="chat.example",
        parent_id=20,
        parent_domain="chat.example",
        unavailable=False,
        type=15,
    )
    thread = SimpleNamespace(
        id=22,
        origin_domain="chat.example",
        guild_id=10,
        guild_domain="chat.example",
        parent_id=21,
        parent_domain="chat.example",
        unavailable=False,
        type=11,
    )
    channels = {
        (20, "chat.example"): category,
        (21, "chat.example"): forum,
        (22, "chat.example"): thread,
    }
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda _model, ref, **_kwargs: channels.get(ref))
    )

    filtered = await filter_restricted_bot_audit_entries(
        session,
        guild,
        BotGuildPermissionGrant(99, 1, 0, ("20@chat.example",)),
        [typed_audit_entry(1, "channel", {"id": "22", "name": "post"})],
    )

    assert [entry.id for entry in filtered] == ["1"]


@pytest.mark.asyncio
async def test_human_audit_query_applies_cursor_actor_action_and_target_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    monkeypatch.setattr(moderation_api, "local_guild", AsyncMock(return_value=guild))
    permission_check = AsyncMock()
    monkeypatch.setattr(moderation_api, "require_permissions", permission_check)
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]))
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="chat.example"))

    result = await moderation_api.list_audit_logs(
        EntityRef("10@chat.example"),
        25,
        900,
        None,
        EntityRef("11@remote.example"),
        25,
        "instance",
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert result == []
    statement = str(session.scalars.await_args.args[0])
    assert "audit_log_entries.id <" in statement
    assert "audit_log_entries.actor_id =" in statement
    assert "audit_log_entries.actor_domain =" in statement
    assert "audit_log_entries.action_type =" in statement
    assert "audit_log_entries.target_type =" in statement
    permission_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_audit_query_supports_ascending_after_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    monkeypatch.setattr(moderation_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(moderation_api, "require_permissions", AsyncMock())
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]))
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="chat.example"))

    await moderation_api.list_audit_logs(
        EntityRef("10@chat.example"),
        25,
        None,
        900,
        None,
        None,
        None,
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    statement = str(session.scalars.await_args.args[0])
    assert "audit_log_entries.id >" in statement
    assert "audit_log_entries.id ASC" in statement


@pytest.mark.asyncio
async def test_human_audit_query_rejects_conflicting_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    monkeypatch.setattr(moderation_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(moderation_api, "require_permissions", AsyncMock())

    with pytest.raises(HTTPException) as rejected:
        await moderation_api.list_audit_logs(
            EntityRef("10@chat.example"),
            25,
            800,
            900,
            None,
            None,
            None,
            SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="chat.example")),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
        )

    assert rejected.value.detail == {
        "code": "AUDIT_LOG_CURSOR_CONFLICT",
        "message": "Choose either a before cursor or an after cursor, not both.",
    }


@pytest.mark.asyncio
async def test_channel_installation_restrictions_allow_exact_or_parent_reference() -> None:
    channel = SimpleNamespace(
        id=20,
        origin_domain="chat.example",
        unavailable=False,
        encryption_mode="plaintext",
        e2ee_required=False,
        guild_id=10,
        guild_domain="chat.example",
        parent_id=19,
        parent_domain="chat.example",
        type=0,
    )
    category = SimpleNamespace(
        id=19,
        origin_domain="chat.example",
        unavailable=False,
        guild_id=10,
        guild_domain="chat.example",
        parent_id=None,
        parent_domain=None,
        type=4,
    )
    installation = SimpleNamespace(
        guild_id=10,
        guild_domain="chat.example",
        granted_scopes=["channels.overwrites.read"],
        channel_restrictions=["19@chat.example"],
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda _model, ref, **_kwargs: {
                (20, "chat.example"): channel,
                (19, "chat.example"): category,
            }.get(ref)
        ),
        scalar=AsyncMock(return_value=installation),
    )
    principal = SimpleNamespace(
        scopes={"channels.overwrites.read"},
        application=SimpleNamespace(id=1, origin_domain="apps.example"),
        user=SimpleNamespace(id=2, origin_domain="apps.example"),
        dm_capability_grant_id=None,
    )

    resolved, installed = await bots_api.installation_for_channel(
        session,
        SimpleNamespace(domain="chat.example"),
        principal,
        EntityRef("20@chat.example"),
        "channels.overwrites.read",
    )
    assert (resolved, installed) == (channel, installation)

    installation.channel_restrictions = ["99@chat.example"]
    with pytest.raises(HTTPException) as restricted:
        await bots_api.installation_for_channel(
            session,
            SimpleNamespace(domain="chat.example"),
            principal,
            EntityRef("20@chat.example"),
            "channels.overwrites.read",
        )
    assert restricted.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}


@pytest.mark.asyncio
async def test_bot_audit_wrapper_requires_scope_then_delegates_live_permission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock()
    delegate = AsyncMock(return_value=[])
    monkeypatch.setattr(bots_api, "installation_for_guild", authorize)
    monkeypatch.setattr(bots_api, "list_audit_logs", delegate)
    principal = SimpleNamespace(user=SimpleNamespace(id=2, origin_domain="apps.example"))
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="chat.example")

    result = await bots_api.bot_list_audit_logs(
        EntityRef("10@chat.example"),
        principal,
        session,
        SimpleNamespace(),
        settings,
        50,
        None,
        None,
        None,
        None,
        None,
    )

    assert result == []
    authorize.assert_awaited_once_with(
        session, settings, principal, EntityRef("10@chat.example"), "audit_logs.read"
    )
    delegate.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_overwrite_wrapper_uses_channel_scope_and_human_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock()
    delegate = AsyncMock(return_value=[])
    monkeypatch.setattr(bots_api, "installation_for_channel", authorize)
    monkeypatch.setattr(bots_api, "list_overwrites", delegate)
    principal = SimpleNamespace(user=SimpleNamespace(id=2, origin_domain="apps.example"))
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="chat.example")
    channel = EntityRef("20@chat.example")

    result = await bots_api.bot_list_channel_overwrites(
        EntityRef("10@chat.example"),
        channel,
        principal,
        session,
        SimpleNamespace(),
        settings,
    )

    assert result == []
    authorize.assert_awaited_once_with(
        session, settings, principal, channel, "channels.overwrites.read"
    )
    delegate.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_overwrite_mutation_uses_manage_scope_and_preserves_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock()
    delegate = AsyncMock(return_value={"status": "updated"})
    monkeypatch.setattr(bots_api, "installation_for_channel", authorize)
    monkeypatch.setattr(bots_api, "put_overwrite", delegate)
    principal = SimpleNamespace(user=SimpleNamespace(id=2, origin_domain="apps.example"))
    session = SimpleNamespace()
    redis = SimpleNamespace()
    snowflake = SimpleNamespace()
    settings = SimpleNamespace(domain="chat.example")
    guild = EntityRef("10@chat.example")
    channel = EntityRef("20@chat.example")
    payload = OverwritePut(target_id="30@chat.example", target_type="role", allow="4", deny="8")

    result = await bots_api.bot_put_channel_overwrite(
        guild,
        channel,
        payload,
        principal,
        session,
        redis,
        snowflake,
        settings,
        "staff only",
    )

    assert result == {"status": "updated"}
    authorize.assert_awaited_once_with(
        session, settings, principal, channel, "channels.overwrites.manage"
    )
    delegate.assert_awaited_once_with(
        guild,
        channel,
        payload,
        principal,
        session,
        redis,
        snowflake,
        settings,
        "staff only",
    )
