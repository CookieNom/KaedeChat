from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import automod as automod_api
from app.automod.engine import evaluate_trigger
from app.automod.schemas import AutoModRuleCreate, AutoModRuleUpdate
from app.automod.service import (
    AutoModPostCommit,
    _active_rules_statement,
    _create_alert_message,
    _queue_timeout_member_projection,
    _validate_refs,
    create_rule,
    evaluate_member_profile,
    evaluate_message,
    require_member_interactions_allowed,
    update_rule,
)
from app.chat.mentions import syntactic_mention_count
from app.chat.rich_content import PollCreate, message_automod_text
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import AutoModAction, AutoModMemberBlock, AutoModRule, Guild, GuildMember, User


def _rule(**overrides: object) -> AutoModRuleCreate:
    payload: dict[str, object] = {
        "name": "Keyword safety",
        "event_type": "message_send",
        "trigger_type": "keyword",
        "trigger_metadata": {"keyword_filter": ["cat"]},
        "actions": [{"type": "block_message", "custom_message": "Please rephrase."}],
    }
    payload.update(overrides)
    return AutoModRuleCreate.model_validate(payload)


def test_admitted_automod_evaluations_lock_a_coherent_rule_snapshot() -> None:
    guild = SimpleNamespace(id=10, origin_domain="home.example")

    message_statement = _active_rules_statement(guild, "message_send", limit=100)
    profile_statement = _active_rules_statement(
        guild,
        "member_update",
        trigger_type="member_profile",
    )

    assert message_statement._for_update_arg is not None
    assert message_statement._for_update_arg.read is True
    assert message_statement._limit_clause is not None
    assert profile_statement._for_update_arg is not None
    assert profile_statement._for_update_arg.read is True


@pytest.mark.asyncio
async def test_rule_create_queues_gateway_event_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Safety guild",
        owner_id=1,
        owner_domain="home.example",
    )
    actor = User(
        id=1,
        origin_domain="home.example",
        is_local=True,
        account_type="human",
        username="owner",
        password_hash="test-only",
    )
    lifecycle: list[str] = []
    session = SimpleNamespace(
        execute=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(side_effect=lambda: lifecycle.append("commit")),
    )
    rendered = {"id": "20", "origin_domain": "home.example"}
    queued_dispatch = Mock()
    monkeypatch.setattr(
        "app.automod.service._require_rule_capacity",
        AsyncMock(),
    )
    monkeypatch.setattr("app.automod.service._replace_children", AsyncMock())
    monkeypatch.setattr("app.automod.service.add_audit_entry", AsyncMock())
    monkeypatch.setattr("app.automod.service.queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(
        "app.automod.service.rule_payload",
        AsyncMock(return_value=rendered),
    )
    monkeypatch.setattr(
        "app.automod.service.queue_postcommit_dispatch",
        queued_dispatch,
    )
    publish = AsyncMock(side_effect=lambda *_args: lifecycle.append("publish"))
    monkeypatch.setattr("app.automod.service.publish_committed_dispatches", publish)
    monkeypatch.setattr("app.automod.service.wake_queued_guild_federation", AsyncMock())
    redis = SimpleNamespace()

    await create_rule(
        session,
        SimpleNamespace(domain="home.example"),
        SimpleNamespace(mint=AsyncMock(return_value=20)),
        guild,
        actor,
        _rule(),
        redis=redis,
        reason=None,
    )

    queued_dispatch.assert_called_once_with(
        session,
        "guild:home.example:10",
        "AUTO_MODERATION_RULE_CREATE",
        rendered,
    )
    session.commit.assert_awaited_once()
    publish.assert_awaited_once_with(session, redis)
    assert lifecycle == ["commit", "publish"]


@pytest.mark.asyncio
async def test_rule_update_refreshes_database_timestamp_before_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed_at = datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Safety guild",
        owner_id=1,
        owner_domain="home.example",
    )
    actor = User(
        id=1,
        origin_domain="home.example",
        is_local=True,
        account_type="human",
        username="owner",
        password_hash="test-only",
    )
    rule = AutoModRule(
        id=20,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Keyword safety",
        creator_id=1,
        creator_domain="home.example",
        event_type="message_send",
        trigger_type="keyword",
        trigger_metadata={"keyword_filter": ["cat"]},
        enabled=True,
        version=1,
        created_at=now,
        updated_at=now,
    )
    refreshed = False

    async def flush() -> None:
        rule.updated_at = None  # type: ignore[assignment]

    async def refresh(
        value: object,
        *,
        attribute_names: tuple[str, ...],
    ) -> None:
        nonlocal refreshed
        assert value is rule
        assert attribute_names == ("updated_at",)
        rule.updated_at = refreshed_at
        refreshed = True

    async def scalars(_statement: object) -> list[object]:
        # These child reads are where AsyncSession autoflush exposed the
        # expired server-onupdate value in production.
        assert refreshed
        return []

    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=scalars),
        flush=AsyncMock(side_effect=flush),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(),
    )
    monkeypatch.setattr("app.automod.service._replace_children", AsyncMock())
    monkeypatch.setattr("app.automod.service.add_audit_entry", AsyncMock())
    queued = AsyncMock()
    monkeypatch.setattr("app.automod.service.queue_guild_mutation", queued)
    monkeypatch.setattr("app.automod.service.queue_postcommit_dispatch", Mock())
    monkeypatch.setattr("app.automod.service.publish_committed_dispatches", AsyncMock())
    monkeypatch.setattr("app.automod.service.wake_queued_guild_federation", AsyncMock())

    await update_rule(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="home.example")),
        cast(Any, SimpleNamespace()),
        guild,
        actor,
        rule,
        AutoModRuleUpdate(
            name="Updated safety",
            actions=[{"type": "block_message", "custom_message": "Please rephrase."}],
        ),
        redis=cast(Any, SimpleNamespace()),
        reason=None,
    )

    session.refresh.assert_awaited_once_with(rule, attribute_names=("updated_at",))
    rendered = queued.await_args.args[5]["rule"]
    assert rendered["updated_at"] == refreshed_at.isoformat()
    assert rendered["version"] == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "trigger_type": "mention_spam",
            "trigger_metadata": {"mention_total_limit": True},
        },
        {"actions": [{"type": "timeout", "duration_seconds": False}]},
    ],
)
def test_automod_rejects_booleans_for_integer_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="must be an integer"):
        _rule(**overrides)


def test_keyword_matching_uses_discord_style_boundaries_and_wildcards() -> None:
    metadata = {"keyword_filter": ["cat"]}
    match = evaluate_trigger("keyword", metadata, "a CAT naps")
    assert match.matched
    assert match.matched_content == "CAT"
    assert not evaluate_trigger("keyword", metadata, "education").matched

    assert evaluate_trigger("keyword", {"keyword_filter": ["cat*"]}, "a catapult launches").matched
    assert evaluate_trigger("keyword", {"keyword_filter": ["*cat"]}, "a copycat appears").matched
    assert evaluate_trigger("keyword", {"keyword_filter": ["*cat*"]}, "education").matched
    assert evaluate_trigger("keyword", {"keyword_filter": ["cat*dog"]}, "cat*dog").matched
    assert not evaluate_trigger("keyword", {"keyword_filter": ["cat*dog"]}, "catZZdog").matched


def test_poll_text_is_part_of_authoritative_automod_plaintext() -> None:
    poll = PollCreate.model_validate(
        {
            "question": {"text": "Choose a release"},
            "answers": [
                {"poll_media": {"text": "Ship safely"}},
                {"poll_media": {"text": "blocked phrase"}},
            ],
            "duration": 24,
        }
    )
    text = message_automod_text(None, poll=poll)
    assert text is not None
    assert evaluate_trigger(
        "keyword",
        {"keyword_filter": ["blocked phrase"]},
        text,
    ).matched


def test_mention_spam_uses_more_than_boundary_and_unique_syntax() -> None:
    metadata = {"mention_total_limit": 2}
    assert not evaluate_trigger("mention_spam", metadata, "", mention_count=2).matched
    assert evaluate_trigger("mention_spam", metadata, "", mention_count=3).matched
    content = "<@10> <@10@home.example> <@&20@home.example> <@&20@HOME.EXAMPLE>"
    assert syntactic_mention_count(content, default_domain="home.example") == 2


def test_allow_list_masks_only_the_allowed_phrase() -> None:
    result = evaluate_trigger(
        "keyword",
        {
            "keyword_filter": ["cat", "dog"],
            "allow_list": ["cat"],
        },
        "cat and dog",
    )
    assert result.matched
    assert result.keyword == "dog"
    assert result.matched_content == "dog"


def test_presets_and_spam_are_functional() -> None:
    assert evaluate_trigger(
        "keyword_preset", {"presets": ["profanity"]}, "this is bullshit"
    ).matched
    assert evaluate_trigger("spam", {}, " ".join(["repeat"] * 8)).matched


def test_user_regexes_run_in_linear_time_and_reject_unsupported_syntax() -> None:
    # This shape is catastrophic in Python's backtracking ``re`` engine. RE2
    # evaluates it in linear time even against the maximum inspected body.
    rule = _rule(trigger_metadata={"regex_patterns": ["(a+)+$"]})
    assert not evaluate_trigger(
        "keyword",
        rule.trigger_metadata.model_dump(mode="json"),
        "a" * 3_999 + "!",
    ).matched

    with pytest.raises(ValidationError, match="lookarounds"):
        _rule(trigger_metadata={"regex_patterns": [r"secret(?=token)"]})


def test_rule_schema_enforces_discord_action_compatibility() -> None:
    with pytest.raises(ValidationError, match="timeout actions support only"):
        _rule(
            trigger_type="spam",
            trigger_metadata={},
            actions=[{"type": "timeout", "duration_seconds": 60}],
        )

    with pytest.raises(ValidationError, match="requires a member-profile rule"):
        _rule(actions=[{"type": "block_member_interaction"}])


def test_member_profile_and_alert_action_contracts() -> None:
    rule = _rule(
        event_type="member_update",
        trigger_type="member_profile",
        trigger_metadata={"keyword_filter": ["blocked name"]},
        actions=[
            {"type": "block_member_interaction"},
            {"type": "send_alert_message", "channel_id": "123"},
        ],
    )
    assert rule.event_type == "member_update"

    with pytest.raises(ValidationError, match="require keyword_filter or regex_patterns"):
        _rule(
            event_type="member_update",
            trigger_type="member_profile",
            trigger_metadata={},
            actions=[{"type": "block_member_interaction"}],
        )

    with pytest.raises(ValidationError, match="requires channel_id only"):
        _rule(
            actions=[
                {
                    "type": "send_alert_message",
                    "channel_id": "123",
                    "custom_message": "not supported",
                }
            ]
        )


@pytest.mark.asyncio
async def test_bot_automod_nested_channels_obey_installation_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="home.example")
    actor = SimpleNamespace(id=5, origin_domain="apps.example", account_type="bot")
    channels = {
        (20, "home.example"): SimpleNamespace(
            id=20,
            origin_domain="home.example",
            guild_id=10,
            guild_domain="home.example",
            parent_id=None,
            parent_domain=None,
            unavailable=False,
            type=0,
        ),
        (21, "home.example"): SimpleNamespace(
            id=21,
            origin_domain="home.example",
            guild_id=10,
            guild_domain="home.example",
            parent_id=None,
            parent_domain=None,
            unavailable=False,
            type=0,
        ),
    }
    session = SimpleNamespace(get=AsyncMock(side_effect=lambda _model, ref: channels.get(ref)))
    permission_check = AsyncMock()
    monkeypatch.setattr(automod_api, "require_permissions", permission_check)
    installation = SimpleNamespace(
        guild_id=10,
        guild_domain="home.example",
        channel_restrictions=["20@home.example"],
    )
    payload = _rule(
        actions=[{"type": "send_alert_message", "channel_id": "20@home.example"}],
        exempt_channels=["21@home.example"],
    )

    with pytest.raises(HTTPException) as caught:
        await automod_api._require_bot_automod_channel_access(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="home.example"),
            guild,
            actor,
            installation,
            actions=payload.actions,
            exempt_channels=payload.exempt_channels,
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    permission_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_automod_rule_routes_hide_or_deny_inaccessible_nested_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="home.example")
    installation = SimpleNamespace(channel_restrictions=["20@home.example"])
    principal = SimpleNamespace(
        user=SimpleNamespace(id=5, origin_domain="apps.example", account_type="bot")
    )
    rule = SimpleNamespace(id=70, origin_domain="home.example")
    authorize = AsyncMock(return_value=(guild, installation))
    access = AsyncMock(return_value=False)
    render = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(automod_api, "authorize_bot_guild_feature_grant", authorize)
    monkeypatch.setattr(automod_api, "_rules", AsyncMock(return_value=[rule]))
    monkeypatch.setattr(automod_api, "get_rule", AsyncMock(return_value=rule))
    monkeypatch.setattr(automod_api, "_require_bot_automod_channel_access", access)
    monkeypatch.setattr(automod_api, "rule_payload", render)
    monkeypatch.setattr(automod_api, "delete_rule", delete)
    common = (
        EntityRef("10@home.example"),
        principal,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="home.example"),
    )

    assert await automod_api.bot_list_auto_mod_rules(*common) == []
    render.assert_not_awaited()

    with pytest.raises(HTTPException) as hidden:
        await automod_api.bot_get_auto_mod_rule(
            common[0],
            70,
            *common[1:],
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail["code"] == "AUTO_MOD_RULE_NOT_FOUND"

    access.side_effect = HTTPException(
        status_code=403,
        detail={"code": "BOT_CHANNEL_RESTRICTED"},
    )
    with pytest.raises(HTTPException) as denied:
        await automod_api.bot_remove_auto_mod_rule(
            common[0],
            70,
            common[1],
            common[2],
            common[3],
            SimpleNamespace(),
            common[4],
        )
    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_automod_patch_cannot_replace_channels_on_hidden_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="home.example")
    installation = SimpleNamespace(channel_restrictions=["20@home.example"])
    principal = SimpleNamespace(
        user=SimpleNamespace(id=5, origin_domain="apps.example", account_type="bot")
    )
    locked_rule = SimpleNamespace(id=70, origin_domain="home.example")
    access = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail={"code": "BOT_CHANNEL_RESTRICTED"},
        )
    )
    update = AsyncMock()
    monkeypatch.setattr(
        automod_api,
        "authorize_bot_guild_feature_grant",
        AsyncMock(return_value=(guild, installation)),
    )
    monkeypatch.setattr(automod_api, "get_rule", AsyncMock(return_value=locked_rule))
    monkeypatch.setattr(automod_api, "_require_bot_automod_channel_access", access)
    monkeypatch.setattr(automod_api, "_require_action_permissions", AsyncMock())
    monkeypatch.setattr(automod_api, "update_rule", update)
    payload = AutoModRuleUpdate.model_validate(
        {
            "actions": [{"type": "block_message"}],
            "exempt_channels": [],
        }
    )

    with pytest.raises(HTTPException) as denied:
        await automod_api.bot_patch_auto_mod_rule(
            EntityRef("10@home.example"),
            70,
            payload,
            principal,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="home.example"),
        )

    assert denied.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    assert access.await_args is not None
    assert access.await_args.kwargs == {
        "actions": None,
        "exempt_channels": None,
        "rule": locked_rule,
    }
    update.assert_not_awaited()


def test_trigger_metadata_rejects_fields_for_the_wrong_trigger() -> None:
    with pytest.raises(ValidationError, match="do not accept trigger metadata"):
        _rule(trigger_type="spam", trigger_metadata={"allow_list": ["safe"]})

    with pytest.raises(ValidationError, match="limited to 100"):
        _rule(
            trigger_metadata={
                "keyword_filter": ["cat"],
                "allow_list": [f"safe-{index}" for index in range(101)],
            }
        )


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "event_type",
        "trigger_metadata",
        "actions",
        "enabled",
        "exempt_roles",
        "exempt_channels",
    ],
)
def test_rule_update_rejects_explicit_null_fields(field: str) -> None:
    with pytest.raises(ValidationError, match=rf"cannot be null: {field}"):
        AutoModRuleUpdate.model_validate({field: None})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("roles", "channels", "code"),
    [
        (
            [EntityRef("12"), EntityRef("12@home.example")],
            [],
            "AUTO_MOD_EXEMPT_ROLE_DUPLICATE",
        ),
        (
            [],
            [EntityRef("15"), EntityRef("15@home.example")],
            "AUTO_MOD_EXEMPT_CHANNEL_DUPLICATE",
        ),
    ],
)
async def test_automod_rejects_qualified_alias_duplicates_before_storage(
    roles: list[EntityRef],
    channels: list[EntityRef],
    code: str,
) -> None:
    session = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await _validate_refs(
            session,
            SimpleNamespace(domain="home.example"),
            SimpleNamespace(id=10, origin_domain="home.example"),
            roles,
            channels,
        )

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == code
    session.execute.assert_not_awaited()


def _profile_runtime_models(
    display_name: str,
) -> tuple[Guild, User, GuildMember, AutoModRule, AutoModMemberBlock]:
    now = datetime.now(UTC)
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Safety guild",
        owner_id=1,
        owner_domain="home.example",
    )
    user = User(
        id=2,
        origin_domain="home.example",
        is_local=True,
        account_type="human",
        username="member",
        display_name=display_name,
        password_hash="test-only",
    )
    member = GuildMember(
        guild_id=10,
        guild_domain="home.example",
        user_id=2,
        user_domain="home.example",
        joined_at=now,
    )
    rule = AutoModRule(
        id=20,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Safe profiles",
        creator_id=1,
        creator_domain="home.example",
        event_type="member_update",
        trigger_type="member_profile",
        trigger_metadata={"keyword_filter": ["blocked*"]},
        enabled=True,
    )
    block = AutoModMemberBlock(
        rule_id=20,
        rule_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        user_id=2,
        user_domain="home.example",
        profile_digest="0" * 64,
        evidence={"profile_field": "display_name"},
    )
    return guild, user, member, rule, block


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor_domain", "username", "display_name", "nickname", "profile_field"),
    [
        ("home.example", "blocked_user", None, None, "username"),
        ("remote.example", "member", "Blocked Name", None, "display_name"),
        ("remote.example", "member", None, "Blocked Nickname", "nickname"),
    ],
)
async def test_member_profile_evaluation_quarantines_all_profile_fields_and_federates_execution(
    monkeypatch: pytest.MonkeyPatch,
    actor_domain: str,
    username: str,
    display_name: str | None,
    nickname: str | None,
    profile_field: str,
) -> None:
    guild, _user, member, rule, _block = _profile_runtime_models(display_name)
    actor = User(
        id=2,
        origin_domain=actor_domain,
        is_local=actor_domain == "home.example",
        account_type="human",
        username=username,
        display_name=display_name,
        password_hash="test-only" if actor_domain == "home.example" else None,
    )
    member.user_domain = actor_domain
    member.nickname = nickname
    action = AutoModAction(
        rule_id=rule.id,
        rule_domain=rule.origin_domain,
        position=0,
        action_type="block_member_interaction",
        action_metadata={},
    )
    added: list[object] = []
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[member, None]),
        scalars=AsyncMock(side_effect=[[rule], [], [action]]),
        get=AsyncMock(return_value=None),
        add=added.append,
        delete=AsyncMock(),
    )
    queued = AsyncMock()
    monkeypatch.setattr(
        "app.automod.service._is_member_profile_exempt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("app.automod.service.queue_guild_mutation", queued)

    post_commit = await evaluate_member_profile(
        session,
        SimpleNamespace(domain="home.example"),
        SimpleNamespace(mint=AsyncMock(return_value=99)),
        guild,
        actor,
    )

    block = next(item for item in added if isinstance(item, AutoModMemberBlock))
    assert block.evidence["profile_field"] == profile_field
    assert queued.await_args.args[4] == "guild.automod.execution"
    execution = queued.await_args.args[5]["execution"]
    assert execution["user_id"] == "2"
    assert execution["user_domain"] == actor_domain
    assert execution["content"] == ""
    assert execution["matched_content"] is None
    assert execution["content_digest"] is None
    local_execution = post_commit.dispatches[0][2]
    assert (
        local_execution["content"]
        == {
            "username": username,
            "display_name": display_name,
            "nickname": nickname,
        }[profile_field]
    )
    assert local_execution["content_digest"] == block.profile_digest
    assert post_commit.guilds == [guild]
    assert post_commit.dispatches[0][1] == "AUTO_MODERATION_ACTION_EXECUTION"


@pytest.mark.asyncio
async def test_member_profile_quarantine_denies_interactions_with_clear_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, user, member, rule, block = _profile_runtime_models("Blocked Name")

    class Rows:
        def all(self) -> list[tuple[AutoModMemberBlock, AutoModRule]]:
            return [(block, rule)]

    fake_session = SimpleNamespace(
        execute=AsyncMock(return_value=Rows()),
        get=AsyncMock(return_value=member),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.automod.service._is_member_profile_exempt",
        AsyncMock(return_value=False),
    )
    with pytest.raises(HTTPException) as exc:
        await require_member_interactions_allowed(
            fake_session,
            guild,
            user,
            Permission.SEND_MESSAGES,
        )
    assert exc.value.detail["code"] == "AUTO_MOD_MEMBER_INTERACTION_BLOCKED"
    assert exc.value.detail["profile_field"] == "display_name"
    assert "Update that profile field" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_member_profile_live_rule_closes_preprojection_quarantine_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, user, member, rule, _block = _profile_runtime_models("Blocked Name")

    class Rows:
        def all(self) -> list[tuple[None, AutoModRule]]:
            return [(None, rule)]

    fake_session = SimpleNamespace(
        execute=AsyncMock(return_value=Rows()),
        get=AsyncMock(return_value=member),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.automod.service._is_member_profile_exempt",
        AsyncMock(return_value=False),
    )

    with pytest.raises(HTTPException) as exc:
        await require_member_interactions_allowed(
            fake_session,
            guild,
            user,
            Permission.SEND_MESSAGES,
        )

    assert exc.value.detail["code"] == "AUTO_MOD_MEMBER_INTERACTION_BLOCKED"
    assert exc.value.detail["profile_field"] == "display_name"


@pytest.mark.asyncio
async def test_member_profile_correction_releases_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, user, member, rule, block = _profile_runtime_models("Compliant Name")

    class Rows:
        def all(self) -> list[tuple[AutoModMemberBlock, AutoModRule]]:
            return [(block, rule)]

    deleted = AsyncMock()
    fake_session = SimpleNamespace(
        execute=AsyncMock(return_value=Rows()),
        get=AsyncMock(return_value=member),
        delete=deleted,
    )
    monkeypatch.setattr(
        "app.automod.service._is_member_profile_exempt",
        AsyncMock(return_value=False),
    )
    await require_member_interactions_allowed(
        fake_session,
        guild,
        user,
        Permission.CONNECT,
    )
    deleted.assert_awaited_once_with(block)


@pytest.mark.asyncio
async def test_timeout_denies_interactions_before_automod_rule_lookup() -> None:
    guild, user, member, _rule, _block = _profile_runtime_models("Compliant Name")
    member.timeout_until = datetime.now(UTC) + timedelta(minutes=15)
    member.timeout_reason = "Cooling off"
    session = SimpleNamespace(
        get=AsyncMock(return_value=member),
        execute=AsyncMock(side_effect=AssertionError("rules must not be queried")),
    )

    with pytest.raises(HTTPException) as exc:
        await require_member_interactions_allowed(
            session,
            guild,
            user,
            Permission.ADD_REACTIONS,
        )

    assert exc.value.detail["code"] == "MEMBER_TIMED_OUT"
    assert exc.value.detail["reason"] == "Cooling off"
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_bot_and_read_only_exemptions_do_not_query_rules() -> None:
    guild, user, _, _, _ = _profile_runtime_models("Blocked Name")
    user.account_type = "bot"
    channel = SimpleNamespace(
        id=30,
        origin_domain="home.example",
        encryption_mode="plaintext",
        e2ee_required=False,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=AssertionError("rules must not be queried")),
        execute=AsyncMock(side_effect=AssertionError("blocks must not be queried")),
    )
    post_commit = await evaluate_message(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="home.example"),
        SimpleNamespace(),
        guild,
        channel,
        user,
        "blocked name",
        mention_count=0,
        actor_permissions=0,
    )
    assert post_commit.dispatches == []

    user.account_type = "human"
    await require_member_interactions_allowed(session, guild, user, Permission.VIEW_CHANNEL)


@pytest.mark.asyncio
async def test_automod_timeout_uses_federated_member_update_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = AsyncMock()
    monkeypatch.setattr("app.automod.service.queue_guild_mutation", queued)
    guild = SimpleNamespace(id=20, origin_domain="home.example")
    actor = SimpleNamespace(id=7, origin_domain="remote.example")
    until = datetime.now(UTC)
    member = SimpleNamespace(
        user_id=7,
        user_domain="remote.example",
        nickname="Remote member",
        timeout_until=until,
        timeout_indefinite=False,
        member_version=4,
    )
    post_commit = AutoModPostCommit()

    await _queue_timeout_member_projection(
        SimpleNamespace(),
        SimpleNamespace(domain="home.example"),
        guild,
        actor,
        member,
        post_commit,
    )

    assert member.member_version == 5
    queued.assert_awaited_once()
    assert queued.await_args.args[4] == "guild.member.update"
    assert queued.await_args.args[5] == {
        "member": {
            "user": {"id": "7", "origin_domain": "remote.example"},
            "nickname": "Remote member",
            "timeout_until": until.isoformat(),
            "timeout_indefinite": False,
            "member_version": "5",
        }
    }
    assert queued.await_args.kwargs == {"snapshot_required": True}
    assert post_commit.guilds == [guild]
    assert post_commit.dispatches == [
        (
            "guild:home.example:20",
            "GUILD_MEMBER_UPDATE",
            {
                "guild_id": "20",
                "guild_domain": "home.example",
                "user_id": "7",
                "user_domain": "remote.example",
            },
        )
    ]


@pytest.mark.asyncio
async def test_automod_alert_advances_channel_last_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=20, origin_domain="home.example")
    channel = SimpleNamespace(
        id=30,
        origin_domain="home.example",
        guild_id=20,
        guild_domain="home.example",
        type=0,
        unavailable=False,
        encryption_mode="plaintext",
        e2ee_required=False,
        encryption_policy_generation=0,
        encryption_epoch=None,
        name="moderator-log",
        last_message_id=None,
        last_message_domain=None,
    )
    creator = SimpleNamespace(id=5, origin_domain="home.example")
    actor = SimpleNamespace(
        id=7,
        origin_domain="remote.example",
        username="member",
        display_name=None,
    )
    rule = SimpleNamespace(
        id=40,
        origin_domain="home.example",
        creator_id=5,
        creator_domain="home.example",
        name="Keyword safety",
    )
    action = AutoModAction(
        rule_id=40,
        rule_domain="home.example",
        position=0,
        action_type="send_alert_message",
        action_metadata={"channel_id": "30@home.example"},
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[channel, creator]),
        add=lambda _: None,
        flush=AsyncMock(),
    )
    queued = AsyncMock()
    monkeypatch.setattr("app.automod.service.message_payload", lambda *_: {"id": "99"})
    monkeypatch.setattr("app.automod.service.profile_from_user", lambda _: {"id": "5"})
    monkeypatch.setattr("app.automod.service.queue_guild_mutation", queued)

    message = await _create_alert_message(
        session,
        SimpleNamespace(domain="home.example"),
        SimpleNamespace(mint=AsyncMock(return_value=99)),
        guild,
        rule,
        actor,
        action,
        AutoModPostCommit(),
        source_channel=None,
        evidence={},
    )

    assert message is not None
    assert (channel.last_message_id, channel.last_message_domain) == (99, "home.example")
    queued.assert_awaited_once()
