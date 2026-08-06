from __future__ import annotations

import asyncio
import base64

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.bootstrap import DomainMismatchError, IdentityKeyError, bootstrap_instance
from app.core.settings import get_settings
from app.db.partitions import ensure_message_partitions, month_snowflake_bound
from app.db.session import create_engine_and_sessionmaker


async def verify() -> None:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as first_session, sessionmaker() as second_session:
            first, concurrent = await asyncio.gather(
                bootstrap_instance(first_session, settings),
                bootstrap_instance(second_session, settings),
            )
            if first.domain != concurrent.domain:
                raise RuntimeError("concurrent bootstrap did not converge")

        async def maintain_partitions() -> None:
            async with engine.begin() as connection:
                await ensure_message_partitions(connection)

        await asyncio.gather(maintain_partitions(), maintain_partitions())

        async with sessionmaker() as session:
            wrong_secret = settings.model_copy(
                update={
                    "secret_key": SecretStr(
                        base64.urlsafe_b64encode(
                            bytes(value ^ 0xFF for value in settings.secret_key_bytes)
                        ).decode("ascii")
                    )
                }
            )
            try:
                await bootstrap_instance(session, wrong_secret)
            except IdentityKeyError:
                pass
            else:
                raise RuntimeError("bootstrap accepted an incorrect secret key")

            mismatched = settings.model_copy(update={"domain": "mismatch.localhost"})
            try:
                await bootstrap_instance(session, mismatched)
            except DomainMismatchError:
                await session.rollback()
            else:
                raise RuntimeError("bootstrap accepted a mismatched domain")

        async with sessionmaker() as session:
            base_id = month_snowflake_bound(2026, 7) + 1
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, origin_domain, is_local, username, password_hash, email) "
                    "VALUES (:id, :domain, true, 'schema_test', 'not-a-real-hash', "
                    "'schema-test@example.invalid')"
                ),
                {"id": base_id, "domain": settings.domain},
            )
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "UPDATE users SET username = 'changed_handle' "
                            "WHERE id = :id AND origin_domain = :domain"
                        ),
                        {"id": base_id, "domain": settings.domain},
                    )
            except IntegrityError:
                pass
            else:
                raise RuntimeError("database allowed an immutable user handle to change")
            await session.execute(
                text(
                    "INSERT INTO dm_conversations "
                    "(id, origin_domain, pair_key, type, authority_domain) "
                    "VALUES (:id, :domain, :pair_key, 'direct', :domain)"
                ),
                {"id": base_id + 1, "domain": settings.domain, "pair_key": "0" * 64},
            )
            await session.execute(
                text(
                    "INSERT INTO channels (id, origin_domain, type, created_floor_id) "
                    "VALUES (:id, :domain, 1, :id)"
                ),
                {"id": base_id + 1, "domain": settings.domain},
            )
            await session.execute(
                text(
                    "INSERT INTO messages "
                    "(id, origin_domain, channel_id, channel_domain, author_id, "
                    "author_domain, content) "
                    "VALUES (:id, :domain, :channel_id, :domain, :author_id, "
                    ":domain, 'partition check')"
                ),
                {
                    "id": base_id + 2,
                    "channel_id": base_id + 1,
                    "author_id": base_id,
                    "domain": settings.domain,
                },
            )
            routed_to = await session.scalar(
                text(
                    "SELECT tableoid::regclass::text FROM messages "
                    "WHERE id = :id AND origin_domain = :domain"
                ),
                {"id": base_id + 2, "domain": settings.domain},
            )
            if routed_to != "messages_2026_07":
                raise RuntimeError(f"message routed to unexpected partition: {routed_to}")

            # Deleting either half of a DM identity must remove the other half.
            await session.execute(
                text(
                    "INSERT INTO dm_conversations "
                    "(id, origin_domain, pair_key, type, authority_domain) "
                    "VALUES (:id, :domain, :pair_key, 'direct', :domain)"
                ),
                {"id": base_id + 4, "domain": settings.domain, "pair_key": "1" * 64},
            )
            await session.execute(
                text(
                    "INSERT INTO channels (id, origin_domain, type, created_floor_id) "
                    "VALUES (:id, :domain, 1, :id)"
                ),
                {"id": base_id + 4, "domain": settings.domain},
            )
            dm_delete = await session.begin_nested()
            await session.execute(
                text("DELETE FROM dm_conversations WHERE id = :id AND origin_domain = :domain"),
                {"id": base_id + 4, "domain": settings.domain},
            )
            remaining_dm_channel = await session.scalar(
                text("SELECT count(*) FROM channels WHERE id = :id AND origin_domain = :domain"),
                {"id": base_id + 4, "domain": settings.domain},
            )
            if remaining_dm_channel != 0:
                raise RuntimeError("deleting a DM conversation left its channel orphaned")
            await dm_delete.rollback()

            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "INSERT INTO channels (id, origin_domain, type, created_floor_id) "
                            "VALUES (:id, :domain, 1, :id)"
                        ),
                        {"id": base_id + 3, "domain": settings.domain},
                    )
                    await session.execute(
                        text("SET CONSTRAINTS fk_channels_dm_conversation_identity IMMEDIATE")
                    )
            except IntegrityError:
                pass
            else:
                raise RuntimeError("database accepted an orphan type-1 channel")

            owner_id = base_id
            member_id = base_id + 10
            guild_id = base_id + 11
            other_guild_id = base_id + 12
            guild_channel_id = base_id + 13
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, origin_domain, is_local, username, password_hash, email) "
                    "VALUES (:id, :domain, true, 'schema_member', 'not-a-real-hash', "
                    "'schema-member@example.invalid')"
                ),
                {"id": member_id, "domain": settings.domain},
            )
            await session.execute(
                text(
                    "INSERT INTO guilds "
                    "(id, origin_domain, name, owner_id, owner_domain) "
                    "VALUES (:id, :domain, 'Schema guild', :owner_id, :domain), "
                    "(:other_id, :domain, 'Other schema guild', :owner_id, :domain)"
                ),
                {
                    "id": guild_id,
                    "other_id": other_guild_id,
                    "owner_id": owner_id,
                    "domain": settings.domain,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO guild_members "
                    "(guild_id, guild_domain, user_id, user_domain, joined_at) "
                    "VALUES (:guild_id, :domain, :owner_id, :domain, now()), "
                    "(:guild_id, :domain, :member_id, :domain, now()), "
                    "(:other_guild_id, :domain, :owner_id, :domain, now()), "
                    "(:other_guild_id, :domain, :member_id, :domain, now())"
                ),
                {
                    "guild_id": guild_id,
                    "other_guild_id": other_guild_id,
                    "owner_id": owner_id,
                    "member_id": member_id,
                    "domain": settings.domain,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO channels "
                    "(id, origin_domain, guild_id, guild_domain, type, name, created_floor_id) "
                    "VALUES (:id, :domain, :guild_id, :domain, 0, 'schema', :id)"
                ),
                {"id": guild_channel_id, "guild_id": guild_id, "domain": settings.domain},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_overwrites "
                    "(channel_id, channel_domain, guild_id, guild_domain, "
                    "target_id, target_domain, target_type, allow, deny) "
                    "VALUES (:channel_id, :domain, :guild_id, :domain, "
                    ":member_id, :domain, 'member', 1, 0)"
                ),
                {
                    "channel_id": guild_channel_id,
                    "guild_id": guild_id,
                    "member_id": member_id,
                    "domain": settings.domain,
                },
            )
            await session.execute(
                text(
                    "DELETE FROM guild_members "
                    "WHERE guild_id = :guild_id AND guild_domain = :domain "
                    "AND user_id = :member_id AND user_domain = :domain"
                ),
                {"guild_id": guild_id, "member_id": member_id, "domain": settings.domain},
            )
            remaining_overwrites = await session.scalar(
                text(
                    "SELECT count(*) FROM channel_overwrites "
                    "WHERE channel_id = :channel_id AND channel_domain = :domain"
                ),
                {"channel_id": guild_channel_id, "domain": settings.domain},
            )
            if remaining_overwrites != 0:
                raise RuntimeError("member removal left a stale channel overwrite")

            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "INSERT INTO channel_overwrites "
                            "(channel_id, channel_domain, guild_id, guild_domain, "
                            "target_id, target_domain, target_type, allow, deny) "
                            "VALUES (:channel_id, :domain, :guild_id, :domain, "
                            ":member_id, :domain, 'member', 1, 0)"
                        ),
                        {
                            "channel_id": guild_channel_id,
                            "guild_id": guild_id,
                            "member_id": member_id,
                            "domain": settings.domain,
                        },
                    )
                    await session.execute(
                        text("SET CONSTRAINTS fk_channel_overwrites_member_target IMMEDIATE")
                    )
            except IntegrityError:
                pass
            else:
                raise RuntimeError("database accepted an overwrite target from another guild")

            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "DELETE FROM guild_members "
                            "WHERE guild_id = :guild_id AND guild_domain = :domain "
                            "AND user_id = :owner_id AND user_domain = :domain"
                        ),
                        {"guild_id": guild_id, "owner_id": owner_id, "domain": settings.domain},
                    )
                    await session.execute(
                        text("SET CONSTRAINTS fk_guilds_owner_membership IMMEDIATE")
                    )
            except IntegrityError:
                pass
            else:
                raise RuntimeError("database allowed a guild owner membership to be removed")
            await session.rollback()

        async with engine.connect() as connection:
            partitions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT child.relname "
                            "FROM pg_inherits "
                            "JOIN pg_class parent ON pg_inherits.inhparent = parent.oid "
                            "JOIN pg_class child ON pg_inherits.inhrelid = child.oid "
                            "WHERE parent.relname = 'messages'"
                        )
                    )
                ).scalars()
            )
            expected = {f"messages_2026_{month:02d}" for month in range(1, 10)}
            if not expected <= partitions:
                raise RuntimeError(f"missing message partitions: {sorted(expected - partitions)}")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(verify())
    print("schema verification passed")


if __name__ == "__main__":
    main()
