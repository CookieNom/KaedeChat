"""Execute aggregation against small SQL tables, including fan-out and empty peers."""

import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import sqlite

from app.admin.auth import AdminPrincipal
from app.admin.instance_statistics import instance_statistics_statement
from app.api.admin_portal import administration_instance_statistics
from app.db.models import User


def test_instance_statistics_counts_local_footprint_without_join_fanout():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE instances (domain TEXT, is_self BOOLEAN, display_name TEXT,
                software_version TEXT, last_seen_at TEXT, federation_inbox_events INTEGER,
                federation_inbox_event_bytes INTEGER);
            INSERT INTO instances VALUES ('local.test', 1, NULL, NULL, NULL, 99, 999);
            INSERT INTO instances VALUES ('peer.test', 0, 'Peer', '1.0', '2026-09-13', 3, 300);
            INSERT INTO instances VALUES ('empty.test', 0, NULL, NULL, NULL, 0, 0);
            CREATE TABLE users (origin_domain TEXT, is_local BOOLEAN);
            INSERT INTO users VALUES ('peer.test', 0), ('peer.test', 0), ('local.test', 1);
            CREATE TABLE messages (author_domain TEXT, deleted_at TEXT);
            INSERT INTO messages VALUES ('peer.test', NULL), ('peer.test', NULL),
                ('peer.test', '2026-09-12'), ('local.test', NULL);
            CREATE TABLE federation_replica_usage (guild_domain TEXT, total_bytes INTEGER);
            INSERT INTO federation_replica_usage VALUES ('peer.test', 100), ('peer.test', 200);
            CREATE TABLE federated_dm_storage_usage (remote_origin_domain TEXT,
                total_bytes INTEGER);
            INSERT INTO federated_dm_storage_usage VALUES ('peer.test', 400);
            CREATE TABLE remote_media_cache (origin_domain TEXT, size INTEGER);
            INSERT INTO remote_media_cache VALUES ('peer.test', 1000), ('peer.test', 500);
            CREATE TABLE federation_outbox (destination TEXT, status TEXT);
            INSERT INTO federation_outbox VALUES ('peer.test', 'pending'), ('peer.test', 'retry'),
                ('peer.test', 'circuit'), ('peer.test', 'delivered'), ('peer.test', 'failed'),
                ('peer.test', 'expired');
        """)
        statement = instance_statistics_statement().compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
        rows = [dict(row) for row in connection.execute(str(statement))]
    assert [row["domain"] for row in rows] == ["peer.test", "empty.test"]
    assert rows[0] == {
        "domain": "peer.test",
        "display_name": "Peer",
        "software_version": "1.0",
        "last_seen_at": "2026-09-13",
        "retained_events": 3,
        "event_storage_bytes": 300,
        "users": 2,
        "messages": 2,
        "guilds": 2,
        "guild_storage_bytes": 300,
        "dm_conversations": 1,
        "dm_storage_bytes": 400,
        "cached_files": 2,
        "media_storage_bytes": 1500,
        "pending_deliveries": 3,
        "failed_deliveries": 2,
    }
    assert all(
        value == 0
        for key, value in rows[1].items()
        if key in rows[0] and isinstance(rows[0][key], int)
    )


async def test_instance_statistics_requires_admin_read_before_querying():
    session = AsyncMock()
    principal = AdminPrincipal(user=User(), roles=frozenset(), capabilities=frozenset())
    with pytest.raises(HTTPException) as error:
        await administration_instance_statistics(principal, session)
    assert error.value.status_code == 403
    session.execute.assert_not_awaited()
