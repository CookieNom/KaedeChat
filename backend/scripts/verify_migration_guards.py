from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.session import create_engine_and_sessionmaker
from scripts.verification import VerificationFailure, failure_message

REVISION = "b46d9a1f2c73"
FEDERATION_DOMAINS = (
    "migration-federation-guard-a.invalid",
    "migration-federation-guard-b.invalid",
)
FEDERATION_EVENT_ID = "migration-guard-shared-federation-event"
GUILD_DOMAINS = ("migration-guild-guard-a.invalid", "migration-guild-guard-b.invalid")
GUILD_OWNER_IDS = (8_900_000_000_000_001, 8_900_000_000_000_002)
GUILD_IDS = (8_900_000_000_000_011, 8_900_000_000_000_012)
GUILD_EVENT_ID = "migration-guard-shared-guild-event"


async def cleanup_federation_fixture(session: AsyncSession) -> None:
    domains = {"domain_a": FEDERATION_DOMAINS[0], "domain_b": FEDERATION_DOMAINS[1]}
    await session.execute(
        text("DELETE FROM federation_events WHERE origin_domain IN (:domain_a, :domain_b)"),
        domains,
    )
    await session.execute(
        text("DELETE FROM instances WHERE domain IN (:domain_a, :domain_b)"), domains
    )


async def prepare_federation_fixture(session: AsyncSession) -> None:
    await cleanup_federation_fixture(session)
    for domain in FEDERATION_DOMAINS:
        await session.execute(
            text("INSERT INTO instances (domain, is_self) VALUES (:domain, false)"),
            {"domain": domain},
        )
        await session.execute(
            text(
                "INSERT INTO federation_events "
                "(origin_domain, event_id, event_type, envelope) "
                "VALUES (:domain, :event_id, 'migration.guard', '{}'::jsonb)"
            ),
            {"domain": domain, "event_id": FEDERATION_EVENT_ID},
        )


async def cleanup_guild_fixture(session: AsyncSession) -> None:
    domains = {"domain_a": GUILD_DOMAINS[0], "domain_b": GUILD_DOMAINS[1]}
    # Guild deletion cascades through guild_events and guild_members before the
    # users and their instance identities are removed.
    await session.execute(
        text("DELETE FROM guilds WHERE origin_domain IN (:domain_a, :domain_b)"),
        domains,
    )
    await session.execute(
        text("DELETE FROM users WHERE origin_domain IN (:domain_a, :domain_b)"),
        domains,
    )
    await session.execute(
        text("DELETE FROM instances WHERE domain IN (:domain_a, :domain_b)"), domains
    )


async def prepare_guild_fixture(session: AsyncSession) -> None:
    await cleanup_guild_fixture(session)
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    for index, domain in enumerate(GUILD_DOMAINS):
        owner_id = GUILD_OWNER_IDS[index]
        guild_id = GUILD_IDS[index]
        await session.execute(
            text("INSERT INTO instances (domain, is_self) VALUES (:domain, false)"),
            {"domain": domain},
        )
        await session.execute(
            text(
                "INSERT INTO users "
                "(id, origin_domain, is_local, username) "
                "VALUES (:id, :domain, false, :username)"
            ),
            {
                "id": owner_id,
                "domain": domain,
                "username": f"guard_owner_{index}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO guilds "
                "(id, origin_domain, name, owner_id, owner_domain) "
                "VALUES (:id, :domain, :name, :owner_id, :domain)"
            ),
            {
                "id": guild_id,
                "domain": domain,
                "name": f"Migration guard {index}",
                "owner_id": owner_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO guild_members "
                "(guild_id, guild_domain, user_id, user_domain, joined_at) "
                "VALUES (:guild_id, :domain, :owner_id, :domain, now())"
            ),
            {"guild_id": guild_id, "domain": domain, "owner_id": owner_id},
        )
        await session.execute(
            text(
                "INSERT INTO guild_events "
                "(guild_id, guild_domain, seq, event_id, envelope) "
                "VALUES (:guild_id, :domain, 1, :event_id, '{}'::jsonb)"
            ),
            {"guild_id": guild_id, "domain": domain, "event_id": GUILD_EVENT_ID},
        )


async def verify_scoped_revision(session: AsyncSession) -> None:
    revisions = {
        str(value)
        for value in await session.scalars(text("SELECT version_num FROM alembic_version"))
    }
    if revisions != {REVISION}:
        raise VerificationFailure(
            f"downgrade expected Alembic revision {REVISION}; received {sorted(revisions)}"
        )

    constraint_names = {
        str(value)
        for value in await session.scalars(
            text(
                "SELECT constraint_name "
                "FROM information_schema.table_constraints "
                "WHERE table_schema = current_schema() "
                "AND table_name IN "
                "('federation_events', 'federation_outbox', 'guild_events')"
            )
        )
    }
    required = {
        "fk_federation_outbox_event_ref",
        "uq_federation_outbox_destination_event_ref",
        "uq_guild_events_guild_domain_event_id",
    }
    forbidden = {
        "fk_federation_outbox_event_id_federation_events",
        "uq_federation_outbox_destination_event_id",
        "uq_guild_events_event_id",
    }
    if not required.issubset(constraint_names) or forbidden & constraint_names:
        raise VerificationFailure(
            "downgrade did not preserve B46 scoped constraints; missing "
            f"{sorted(required - constraint_names)}, forbidden present "
            f"{sorted(forbidden & constraint_names)}"
        )

    primary_key_columns = tuple(
        str(value)
        for value in await session.scalars(
            text(
                "SELECT key_column_usage.column_name "
                "FROM information_schema.table_constraints AS table_constraints "
                "JOIN information_schema.key_column_usage AS key_column_usage "
                "ON key_column_usage.constraint_catalog = "
                "table_constraints.constraint_catalog "
                "AND key_column_usage.constraint_schema = "
                "table_constraints.constraint_schema "
                "AND key_column_usage.constraint_name = "
                "table_constraints.constraint_name "
                "WHERE table_constraints.table_schema = current_schema() "
                "AND table_constraints.table_name = 'federation_events' "
                "AND table_constraints.constraint_name = 'pk_federation_events' "
                "ORDER BY key_column_usage.ordinal_position"
            )
        )
    )
    if primary_key_columns != ("origin_domain", "event_id"):
        raise VerificationFailure(
            "federation event primary key expected ('origin_domain', 'event_id'); "
            f"received {primary_key_columns!r}"
        )


async def verify_federation_fixture(session: AsyncSession) -> None:
    await verify_scoped_revision(session)
    event_count = await session.scalar(
        text(
            "SELECT count(*) FROM federation_events "
            "WHERE event_id = :event_id "
            "AND origin_domain IN (:domain_a, :domain_b)"
        ),
        {
            "event_id": FEDERATION_EVENT_ID,
            "domain_a": FEDERATION_DOMAINS[0],
            "domain_b": FEDERATION_DOMAINS[1],
        },
    )
    if event_count != 2:
        raise VerificationFailure(
            f"federation collision fixture expected 2 events; received {event_count}"
        )


async def verify_guild_fixture(session: AsyncSession) -> None:
    await verify_scoped_revision(session)
    event_count = await session.scalar(
        text(
            "SELECT count(*) FROM guild_events "
            "WHERE event_id = :event_id "
            "AND guild_domain IN (:domain_a, :domain_b)"
        ),
        {
            "event_id": GUILD_EVENT_ID,
            "domain_a": GUILD_DOMAINS[0],
            "domain_b": GUILD_DOMAINS[1],
        },
    )
    if event_count != 2:
        raise VerificationFailure(
            f"guild collision fixture expected 2 events; received {event_count}"
        )


async def run(action: str) -> None:
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker.begin() as session:
            if action == "prepare-federation":
                await prepare_federation_fixture(session)
            elif action == "verify-federation":
                await verify_federation_fixture(session)
            elif action == "cleanup-federation":
                await cleanup_federation_fixture(session)
            elif action == "prepare-guild":
                await prepare_guild_fixture(session)
            elif action == "verify-guild":
                await verify_guild_fixture(session)
            elif action == "cleanup-guild":
                await cleanup_guild_fixture(session)
            else:  # pragma: no cover - argparse enforces the choices
                raise VerificationFailure(f"unsupported migration-guard action: {action!r}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "prepare-federation",
            "verify-federation",
            "cleanup-federation",
            "prepare-guild",
            "verify-guild",
            "cleanup-guild",
        ),
    )
    args = parser.parse_args()
    asyncio.run(run(args.action))


if __name__ == "__main__":
    try:
        main()
    except VerificationFailure as error:
        raise SystemExit(
            failure_message("migration guard", error, "make migration-check")
        ) from None
