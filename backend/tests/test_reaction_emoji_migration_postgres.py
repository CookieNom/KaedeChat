"""PostgreSQL integration guard for the reaction-key data migration.

Run explicitly with:
    KAEDE_REACTION_MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://... \
      pytest -q tests/test_reaction_emoji_migration_postgres.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

migration = import_module("migrations.versions.3d9a5e1c7b42_reaction_emoji_canonicalization")

DATABASE_URL = os.environ.get("KAEDE_REACTION_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set KAEDE_REACTION_MIGRATION_TEST_DATABASE_URL to run PostgreSQL migration tests",
)


async def apply_reaction_migration(connection: AsyncConnection) -> None:
    await connection.execute(text(migration.REACTION_MAPPING_SQL))
    await connection.execute(text(migration.REACTION_MERGE_SQL))
    await connection.execute(text(migration.REACTION_DELETE_LEGACY_SQL))


@pytest.mark.asyncio
async def test_reaction_alias_collisions_merge_without_losing_distinct_reactors() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    schema = f"reaction_migration_{uuid4().hex}"
    canonical_time = datetime(2026, 8, 29, 10, tzinfo=UTC)
    legacy_time = datetime(2026, 8, 29, 11, tzinfo=UTC)
    actor_two_time = datetime(2026, 8, 29, 12, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(
                text(
                    """
                    CREATE TABLE reactions (
                        message_id bigint NOT NULL,
                        message_domain varchar(253) NOT NULL,
                        user_id bigint NOT NULL,
                        user_domain varchar(253) NOT NULL,
                        emoji_key varchar(320) NOT NULL,
                        created_at timestamptz NOT NULL,
                        PRIMARY KEY (
                            message_id,
                            message_domain,
                            user_id,
                            user_domain,
                            emoji_key
                        )
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE guild_history_staged_messages (
                        export_id bigint NOT NULL,
                        export_domain varchar(253) NOT NULL,
                        message_id bigint NOT NULL,
                        message_domain varchar(253) NOT NULL,
                        payload jsonb NOT NULL,
                        PRIMARY KEY (
                            export_id,
                            export_domain,
                            message_id,
                            message_domain
                        )
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE channels (
                        id bigint NOT NULL,
                        origin_domain varchar(253) NOT NULL,
                        default_reaction_emoji jsonb,
                        PRIMARY KEY (id, origin_domain)
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO reactions (
                        message_id,
                        message_domain,
                        user_id,
                        user_domain,
                        emoji_key,
                        created_at
                    ) VALUES (
                        :message_id,
                        'guild.example',
                        :user_id,
                        'users.example',
                        :emoji,
                        :created_at
                    )
                    """
                ),
                [
                    {
                        "message_id": 1,
                        "user_id": 1,
                        "emoji": "❤️",
                        "created_at": legacy_time,
                    },
                    {
                        "message_id": 1,
                        "user_id": 1,
                        "emoji": "❤",
                        "created_at": canonical_time,
                    },
                    {
                        "message_id": 1,
                        "user_id": 2,
                        "emoji": "❤️",
                        "created_at": actor_two_time,
                    },
                    {
                        "message_id": 1,
                        "user_id": 3,
                        "emoji": "🏮",
                        "created_at": legacy_time,
                    },
                    {
                        "message_id": 1,
                        "user_id": 4,
                        "emoji": "<:lantern:7@HOME.EXAMPLE.>",
                        "created_at": legacy_time,
                    },
                    {
                        "message_id": 1,
                        "user_id": 4,
                        "emoji": "<:lantern:7@home.example>",
                        "created_at": canonical_time,
                    },
                ],
            )
            await apply_reaction_migration(connection)
            await connection.execute(
                text(
                    """
                    INSERT INTO channels (id, origin_domain, default_reaction_emoji)
                    VALUES (:id, 'guild.example', CAST(:payload AS jsonb))
                    """
                ),
                [
                    {
                        "id": 1,
                        "payload": json.dumps(
                            {"emoji_id": None, "emoji_name": "❤️", "extension": True}
                        ),
                    },
                    {
                        "id": 2,
                        "payload": json.dumps({"emoji_id": None, "emoji_name": "lantern"}),
                    },
                    {
                        "id": 3,
                        "payload": json.dumps({"emoji_id": "7", "emoji_name": None}),
                    },
                    {
                        "id": 4,
                        "payload": json.dumps({"emoji_id": None, "emoji_name": "❤"}),
                    },
                ],
            )
            await connection.run_sync(migration._canonicalize_forum_defaults)  # noqa: SLF001
            await connection.execute(
                text(
                    """
                    INSERT INTO guild_history_staged_messages (
                        export_id,
                        export_domain,
                        message_id,
                        message_domain,
                        payload
                    ) VALUES (1, 'guild.example', 9, 'guild.example', CAST(:payload AS jsonb))
                    """
                ),
                {
                    "payload": json.dumps(
                        {
                            "id": "9",
                            "reactions": [
                                {
                                    "user_id": "1",
                                    "user_domain": "users.example",
                                    "emoji": "❤️",
                                    "created_at": legacy_time.isoformat(),
                                },
                                {
                                    "user_id": "1",
                                    "user_domain": "users.example",
                                    "emoji": "❤",
                                    "created_at": canonical_time.isoformat(),
                                },
                                {
                                    "user_id": "2",
                                    "user_domain": "users.example",
                                    "emoji": "lantern",
                                    "created_at": legacy_time.isoformat(),
                                },
                            ],
                        }
                    )
                },
            )
            await connection.run_sync(
                migration._canonicalize_staged_history_reactions  # noqa: SLF001
            )

        async with engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT user_id, emoji_key, created_at
                        FROM reactions
                        ORDER BY user_id, emoji_key
                        """
                    )
                )
            ).all()
            assert rows == [
                (1, "❤", canonical_time),
                (2, "❤", actor_two_time),
                (3, "🏮", legacy_time),
                (4, "<:lantern:7@home.example>", canonical_time),
            ]
            forum_defaults = dict(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT id, default_reaction_emoji
                            FROM channels
                            ORDER BY id
                            """
                        )
                    )
                ).all()
            )
            assert forum_defaults == {
                1: {"emoji_id": None, "emoji_name": "❤", "extension": True},
                2: None,
                3: {"emoji_id": "7", "emoji_name": None},
                4: {"emoji_id": None, "emoji_name": "❤"},
            }
            staged_payload = await connection.scalar(
                text("SELECT payload FROM guild_history_staged_messages")
            )
            assert staged_payload == {
                "id": "9",
                "reactions": [
                    {
                        "user_id": "1",
                        "user_domain": "users.example",
                        "emoji": "❤",
                        "created_at": canonical_time.isoformat(),
                    }
                ],
            }
            await apply_reaction_migration(connection)
            await connection.run_sync(migration._canonicalize_forum_defaults)  # noqa: SLF001
            await connection.run_sync(
                migration._canonicalize_staged_history_reactions  # noqa: SLF001
            )

        async with engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            assert (await connection.scalar(text("SELECT count(*) FROM reactions"))) == 4
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
