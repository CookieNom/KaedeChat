import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.models import MessageSearchPage
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def client() -> Client:
    bot = Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )
    bot._targets["https://guild.example"] = AsyncMock()  # noqa: SLF001
    return bot


def search_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "message": {
                    "id": "30",
                    "origin_domain": "guild.example",
                    "channel_id": "20",
                    "channel_domain": "guild.example",
                    "author_id": "10",
                    "author_domain": "apps.example",
                    "author": {
                        "id": "10",
                        "origin_domain": "apps.example",
                        "username": "release-bot",
                        "account_type": "bot",
                        "bot": True,
                    },
                    "content": "deploy complete",
                    "attachments": [],
                    "created_at": "2026-08-28T12:00:00Z",
                    "bot_installation_id": "88",
                },
                "channel": {
                    "id": "20",
                    "origin_domain": "guild.example",
                    "guild_id": "40",
                    "guild_domain": "guild.example",
                    "type": 0,
                    "name": "release-room",
                    "bot_installation_id": "88",
                },
                "guild": {
                    "id": "40",
                    "origin_domain": "guild.example",
                    "name": "Release Guild",
                    "owner_id": "5",
                    "owner_domain": "guild.example",
                    "installation_id": "88",
                    "capability_revision": "3",
                    "granted_scopes": ["messages.history", "messages.content"],
                    "granted_intents": ["message_content"],
                    "channel_restrictions": ["20@guild.example"],
                },
                "snippet": "deploy complete",
            }
        ],
        "next_cursor": "MjU",
        "encrypted_channel_refs": ["21@guild.example"],
        "coverage": {"local": "complete", "authority": "not_needed"},
        "indexing": False,
    }


@pytest.mark.asyncio
async def test_search_guild_messages_uses_authority_and_retains_runtime() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=search_payload())  # type: ignore[method-assign]
    guild = EntityRef(40, "guild.example")

    page = await bot.search_guild_messages(
        guild,
        "deploy",
        target="https://replica.example",
        channels=(EntityRef(20, "guild.example"),),
        authors=(EntityRef(10, "apps.example"),),
        mentions=(EntityRef(11, "apps.example"),),
        mention_roles=(EntityRef(12, "guild.example"),),
        mention_everyone=False,
        replied_to_users=(EntityRef(13, "apps.example"),),
        replied_to_messages=(EntityRef(14, "guild.example"),),
        has_content=("file", "poll", "-video"),
        embed_types=("article",),
        embed_providers=("YouTube",),
        link_hostnames=("EXAMPLE.com.",),
        attachment_filenames=("Release.TXT",),
        attachment_extensions=(".TXT",),
        max_id=EntityRef(100, "guild.example"),
        min_id=EntityRef(5, "guild.example"),
        author_types=("bot", "-webhook"),
        after=datetime(2026, 8, 1, tzinfo=UTC),
        include_nsfw=True,
    )

    assert isinstance(page, MessageSearchPage)
    assert page.results[0].message.content == "deploy complete"
    assert page.results[0].message.bot_installation_id == 88
    assert page.results[0].channel.bot_installation_id == 88
    assert page.results[0].guild is not None
    assert page.results[0].guild.installation_revision == 3
    assert page.encrypted_channel_refs == (EntityRef(21, "guild.example"),)
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["target"] == "https://guild.example"
    body = bot.request.await_args.kwargs["json"]
    assert body["sort"] == "newest"

    assert body["include_nsfw"] is True
    assert body["filters"] == {
        "channel_ids": ["20@guild.example"],
        "authors": ["10@apps.example"],
        "mentions": ["11@apps.example"],
        "mentions_role_ids": ["12@guild.example"],
        "mention_everyone": False,
        "replied_to_user_ids": ["13@apps.example"],
        "replied_to_message_ids": ["14@guild.example"],
        "has": ["file", "poll", "-video"],
        "embed_types": ["article"],
        "embed_providers": ["YouTube"],
        "link_hostnames": ["example.com"],
        "attachment_filenames": ["release.txt"],
        "attachment_extensions": ["txt"],
        "max_id": "100@guild.example",
        "min_id": "5@guild.example",
        "before": None,
        "after": "2026-08-01T00:00:00+00:00",
        "pinned": None,
        "author_type": None,
        "author_types": ["bot", "-webhook"],
    }


@pytest.mark.asyncio
async def test_search_guild_messages_rejects_substituted_result_authority() -> None:
    bot = client()
    guild = EntityRef(40, "guild.example")

    substituted_guild = copy.deepcopy(search_payload())
    results = substituted_guild["results"]
    assert isinstance(results, list)
    result = results[0]
    assert isinstance(result, dict)
    raw_guild = result["guild"]
    raw_channel = result["channel"]
    assert isinstance(raw_guild, dict)
    assert isinstance(raw_channel, dict)
    raw_guild["id"] = "41"
    raw_channel["guild_id"] = "41"
    bot.request = AsyncMock(return_value=substituted_guild)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="changed the requested guild"):
        await bot.search_guild_messages(guild, target="https://guild.example")

    substituted_encrypted = search_payload()
    substituted_encrypted["encrypted_channel_refs"] = ["21@other.example"]
    bot.request = AsyncMock(return_value=substituted_encrypted)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="changed the requested guild"):
        await bot.search_guild_messages(guild, target="https://guild.example")


@pytest.mark.asyncio
async def test_search_guild_messages_rejects_ambiguous_and_malformed_inputs() -> None:
    bot = client()
    guild = EntityRef(40, "guild.example")

    with pytest.raises(ValueError, match="limit"):
        await bot.search_guild_messages(guild, "deploy", limit=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            before=datetime(2026, 8, 28),
        )
    with pytest.raises(ValueError, match="author type"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            author_type="application",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="entity references"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            authors="10@apps.example",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="authority"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            channels=(EntityRef(20, "other.example"),),
        )
    with pytest.raises(ValueError, match="conflict"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            has_content=("poll", "-poll"),
        )
    with pytest.raises(ValueError, match="include_nsfw"):
        await bot.search_guild_messages(
            guild,
            "deploy",
            include_nsfw=1,  # type: ignore[arg-type]
        )
    malformed = search_payload()
    malformed["indexing"] = 1
    with pytest.raises(ValueError, match="response"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
    malformed = search_payload()
    malformed["encrypted_channel_refs"] = [True]
    with pytest.raises(ValueError, match="response"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
    malformed = search_payload()
    malformed["results"][0]["guild"] = None  # type: ignore[index]
    with pytest.raises(ValueError, match="result"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
    malformed = search_payload()
    malformed["results"][0]["message"]["channel_id"] = "21"  # type: ignore[index]
    with pytest.raises(ValueError, match="linkage"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
    malformed = search_payload()
    malformed["results"][0]["channel"]["guild_id"] = "41"  # type: ignore[index]
    with pytest.raises(ValueError, match="linkage"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
    malformed = search_payload()
    malformed["coverage"] = {f"key-{index}": "complete" for index in range(9)}
    with pytest.raises(ValueError, match="response"):
        MessageSearchPage.from_payload(bot, "https://guild.example", malformed)
