from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from app.api import expressions
from app.api.expressions import EmojiUpdate, StickerUpdate
from app.chat.custom_emojis import (
    canonical_reaction_emoji,
    custom_emoji_refs,
    resolve_rich_custom_emojis,
    validate_custom_emoji_use,
)
from app.chat.rich_content import ActionRow, Button, PollCreate
from app.chat.schemas import ReactionCreate
from app.core.permissions import Permission
from app.core.types import EntityRef


def test_custom_emoji_refs_parse_and_deduplicate_federated_tokens() -> None:
    refs = custom_emoji_refs(
        "hello <:party:75512661369970688@alpha.example> "
        "<a:dance:75512661369970689@beta.example> "
        "<:party:75512661369970688@alpha.example>"
    )

    assert [(ref.id, ref.origin_domain, ref.name, ref.animated) for ref in refs] == [
        (75512661369970688, "alpha.example", "party", False),
        (75512661369970689, "beta.example", "dance", True),
    ]
    assert refs[1].token == "<a:dance:75512661369970689@beta.example>"


def test_custom_emoji_refs_ignore_noncanonical_tokens() -> None:
    assert (
        custom_emoji_refs(
            "<:x:0@bad> <:valid:123@example.invalid/path> "
            "<:huge:9999999999999999999@example.invalid>"
        )
        == ()
    )


def test_reaction_emoji_is_one_canonical_unicode_or_qualified_custom_token() -> None:
    assert canonical_reaction_emoji("❤️") == "❤"
    assert canonical_reaction_emoji("👨‍👩‍👧‍👦") == "👨‍👩‍👧‍👦"
    assert canonical_reaction_emoji("1️⃣") == "1⃣"
    assert canonical_reaction_emoji("🇺🇸") == "🇺🇸"
    assert (
        ReactionCreate(emoji="<a:dance:75512661369970689@BETA.EXAMPLE.>").emoji
        == "<a:dance:75512661369970689@beta.example>"
    )
    for invalid in (
        "hello",
        "😀 trailing",
        "😀😀",
        "<:party:123@alpha.example>junk",
        "<party:123@alpha.example>",
        "<:x:123@alpha.example>",
    ):
        with pytest.raises(ValueError, match="reaction"):
            ReactionCreate(emoji=invalid)


class EmojiSession:
    def __init__(self, emoji: object | None, membership: int | None = 1) -> None:
        self.emoji = emoji
        self.membership = membership

    async def get(self, _model: object, _identity: object) -> object | None:
        return self.emoji

    async def scalar(self, _query: object) -> int | None:
        return self.membership


@pytest.mark.asyncio
async def test_external_custom_emoji_requires_destination_permission() -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="party",
        animated=False,
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=30, origin_domain="beta.example")

    with pytest.raises(HTTPException) as raised:
        await validate_custom_emoji_use(
            cast(Any, EmojiSession(emoji)),
            cast(Any, actor),
            "<:party:123@alpha.example>",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES,
        )
    assert raised.value.detail == {"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}


@pytest.mark.asyncio
async def test_external_custom_emoji_requires_source_membership() -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="party",
        animated=False,
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=30, origin_domain="beta.example")

    with pytest.raises(HTTPException) as raised:
        await validate_custom_emoji_use(
            cast(Any, EmojiSession(emoji, membership=None)),
            cast(Any, actor),
            "<:party:123@alpha.example>",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_EMOJIS,
        )
    assert raised.value.detail == {
        "code": "CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED",
        "message": "You must be a member of the emoji's guild to use it.",
    }


@pytest.mark.asyncio
async def test_bot_can_use_its_application_emoji_without_guild_entitlement() -> None:
    application_emoji = SimpleNamespace(
        id=123,
        application_id=30,
        application_domain="apps.example",
        name="party",
        animated=False,
        available=True,
    )
    actor = SimpleNamespace(
        id=20,
        origin_domain="apps.example",
        account_type="bot",
    )

    class Session:
        async def scalar(self, _query: object) -> object:
            return application_emoji

        async def get(self, _model: object, _identity: object) -> object:
            raise AssertionError("application emoji use must not require guild emoji lookup")

    await validate_custom_emoji_use(
        cast(Any, Session()),
        cast(Any, actor),
        "<:party:123@apps.example>",
        target_guild=cast(Any, SimpleNamespace(id=40, origin_domain="guild.example")),
        target_permissions=Permission.SEND_MESSAGES,
    )


@pytest.mark.asyncio
async def test_rich_custom_emoji_is_authorized_and_canonicalized() -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="alpha.example",
        guild_id=10,
        guild_domain="alpha.example",
        name="party",
        animated=True,
        available=True,
    )
    actor = SimpleNamespace(id=20, origin_domain="alpha.example")
    destination = SimpleNamespace(id=10, origin_domain="alpha.example")
    button = Button(custom_id="party", emoji={"id": "123@alpha.example"})
    poll = PollCreate.model_validate(
        {
            "question": {"text": "Choose"},
            "answers": [
                {"poll_media": {"text": "One"}},
                {"poll_media": {"emoji": {"id": "123@alpha.example"}}},
            ],
            "duration": 1,
        }
    )

    await resolve_rich_custom_emojis(
        cast(Any, EmojiSession(emoji)),
        cast(Any, actor),
        components=[ActionRow(components=[button])],
        poll=poll,
        default_domain="alpha.example",
        target_guild=cast(Any, destination),
        target_permissions=Permission.SEND_MESSAGES,
    )

    assert button.emoji is not None
    assert button.emoji.model_dump(mode="json", exclude_none=True) == {
        "id": "123@alpha.example",
        "name": "party",
        "animated": True,
    }
    assert poll.answers[1].poll_media.emoji is not None
    assert poll.answers[1].poll_media.emoji.name == "party"
    assert poll.answers[1].poll_media.emoji.animated is True


@pytest.mark.asyncio
async def test_federated_rich_emoji_attestation_is_limited_to_actor_home() -> None:
    actor = SimpleNamespace(id=20, origin_domain="actor.example")
    destination = SimpleNamespace(id=30, origin_domain="guild.example")
    third_instance = Button(
        custom_id="spoof",
        emoji={"id": "123@third.example", "name": "party", "animated": False},
    )

    with pytest.raises(HTTPException) as raised:
        await resolve_rich_custom_emojis(
            cast(Any, EmojiSession(None)),
            cast(Any, actor),
            components=[ActionRow(components=[third_instance])],
            poll=None,
            default_domain="actor.example",
            target_guild=cast(Any, destination),
            target_permissions=Permission.SEND_MESSAGES | Permission.USE_EXTERNAL_EMOJIS,
            trusted_external_domain="actor.example",
        )

    assert raised.value.detail == {"code": "CUSTOM_EMOJI_NOT_FOUND"}


@pytest.mark.asyncio
async def test_removing_emoji_role_restrictions_records_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="chat.example",
        guild_id=10,
        guild_domain="chat.example",
        name="party",
        animated=False,
        available=True,
        media_hash="a" * 64,
        creator_id=20,
        creator_domain="chat.example",
        updated_at=None,
    )
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    actor = SimpleNamespace(id=20, origin_domain="chat.example")
    validations: list[list[str]] = []
    audits: list[tuple[tuple[object, ...], dict[str, object]]] = []
    role_reads = iter([["30@chat.example"], []])

    class Session:
        async def execute(self, _query: object) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(
            self,
            value: object,
            *,
            attribute_names: tuple[str, ...],
        ) -> None:
            assert value is emoji
            assert attribute_names == ("updated_at",)
            emoji.updated_at = datetime.now(UTC)

        async def commit(self) -> None:
            return None

    async def get_emoji(*_args: object, **_kwargs: object) -> object:
        return emoji

    async def emoji_roles(*_args: object, **_kwargs: object) -> list[str]:
        return next(role_reads)

    async def validate_roles(
        _session: object,
        _settings: object,
        _guild: object,
        refs: list[object],
    ) -> list[object]:
        validations.append([str(item) for item in refs])
        return [
            SimpleNamespace(id=int(str(item).split("@", 1)[0]), origin_domain="chat.example")
            for item in refs
        ]

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def capture_audit(*args: object, **kwargs: object) -> None:
        audits.append((args, kwargs))

    monkeypatch.setattr(expressions, "_get_emoji", get_emoji)
    monkeypatch.setattr(expressions, "_emoji_roles", emoji_roles)
    monkeypatch.setattr(expressions, "_validate_roles", validate_roles)
    monkeypatch.setattr(expressions, "add_audit_entry", capture_audit)
    monkeypatch.setattr(expressions, "queue_guild_mutation", no_op)
    monkeypatch.setattr(expressions, "wake_queued_guild_federation", no_op)

    await expressions._patch_emoji(
        cast(Any, Session()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, guild),
        cast(Any, actor),
        emoji.id,
        EmojiUpdate(role_ids=[]),
        reason=None,
    )

    assert validations == [[]]
    assert len(audits) == 1
    assert audits[0][0][4] == 61
    assert audits[0][1]["changes"] == [
        {
            "key": "roles",
            "old_value": ["30@chat.example"],
            "new_value": [],
        }
    ]


@pytest.mark.asyncio
async def test_emoji_role_restrictions_reject_canonical_alias_duplicates() -> None:
    with pytest.raises(HTTPException) as raised:
        await expressions._validate_roles(
            cast(Any, object()),
            cast(Any, SimpleNamespace(domain="chat.example")),
            cast(Any, SimpleNamespace(id=10, origin_domain="chat.example")),
            [EntityRef("30"), EntityRef("30@chat.example")],
        )
    assert raised.value.detail == {
        "code": "EMOJI_ROLE_DUPLICATE",
        "message": "Choose each emoji restriction role only once.",
    }


def test_expression_updates_reject_null_non_nullable_fields() -> None:
    with pytest.raises(ValueError, match="emoji name cannot be null"):
        EmojiUpdate.model_validate({"name": None})
    with pytest.raises(ValueError, match="sticker name cannot be null"):
        StickerUpdate.model_validate({"name": None})
    with pytest.raises(ValueError, match="sticker tags cannot be null"):
        StickerUpdate.model_validate({"tags": None})


@pytest.mark.asyncio
async def test_null_emoji_roles_clear_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emoji = SimpleNamespace(
        id=123,
        origin_domain="chat.example",
        guild_id=10,
        guild_domain="chat.example",
        name="party",
        animated=False,
        available=True,
        media_hash="a" * 64,
        creator_id=20,
        creator_domain="chat.example",
        updated_at=None,
    )
    guild = SimpleNamespace(id=10, origin_domain="chat.example")
    actor = SimpleNamespace(id=20, origin_domain="chat.example")
    role_reads = iter([["30@chat.example"], []])
    validations: list[list[str]] = []

    class Session:
        async def execute(self, _query: object) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(
            self,
            value: object,
            *,
            attribute_names: tuple[str, ...],
        ) -> None:
            assert value is emoji
            assert attribute_names == ("updated_at",)
            emoji.updated_at = datetime.now(UTC)

        async def commit(self) -> None:
            return None

    async def get_emoji(*_args: object, **_kwargs: object) -> object:
        return emoji

    async def emoji_roles(*_args: object, **_kwargs: object) -> list[str]:
        return next(role_reads)

    async def validate_roles(
        _session: object,
        _settings: object,
        _guild: object,
        refs: list[object],
    ) -> list[object]:
        validations.append([str(item) for item in refs])
        return []

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(expressions, "_get_emoji", get_emoji)
    monkeypatch.setattr(expressions, "_emoji_roles", emoji_roles)
    monkeypatch.setattr(expressions, "_validate_roles", validate_roles)
    monkeypatch.setattr(expressions, "add_audit_entry", no_op)
    monkeypatch.setattr(expressions, "queue_guild_mutation", no_op)
    monkeypatch.setattr(expressions, "wake_queued_guild_federation", no_op)

    await expressions._patch_emoji(
        cast(Any, Session()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, guild),
        cast(Any, actor),
        emoji.id,
        EmojiUpdate.model_validate({"role_ids": None}),
        reason=None,
    )

    assert validations == [[]]
