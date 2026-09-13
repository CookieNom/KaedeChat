"""Local federation footprint, grouped before joining to avoid multiplying counts."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.sql import Select

from app.db.models import (
    FederatedDMStorageUsage,
    FederationOutbox,
    FederationReplicaUsage,
    Instance,
    Message,
    RemoteMediaCache,
    User,
)


def instance_statistics_statement() -> Select[Any]:
    users = (
        select(User.origin_domain.label("domain"), func.count().label("users"))
        .where(User.is_local.is_(False))
        .group_by(User.origin_domain)
        .subquery()
    )
    messages = (
        select(Message.author_domain.label("domain"), func.count().label("messages"))
        .where(Message.deleted_at.is_(None))
        .group_by(Message.author_domain)
        .subquery()
    )
    guilds = (
        select(
            FederationReplicaUsage.guild_domain.label("domain"),
            func.count().label("guilds"),
            func.sum(FederationReplicaUsage.total_bytes).label("guild_storage_bytes"),
        )
        .group_by(FederationReplicaUsage.guild_domain)
        .subquery()
    )
    dms = (
        select(
            FederatedDMStorageUsage.remote_origin_domain.label("domain"),
            func.count().label("dm_conversations"),
            func.sum(FederatedDMStorageUsage.total_bytes).label("dm_storage_bytes"),
        )
        .group_by(FederatedDMStorageUsage.remote_origin_domain)
        .subquery()
    )
    media = (
        select(
            RemoteMediaCache.origin_domain.label("domain"),
            func.count().label("cached_files"),
            func.sum(RemoteMediaCache.size).label("media_storage_bytes"),
        )
        .group_by(RemoteMediaCache.origin_domain)
        .subquery()
    )
    deliveries = (
        select(
            FederationOutbox.destination.label("domain"),
            func.count()
            .filter(FederationOutbox.status.in_(("pending", "retry", "circuit")))
            .label("pending_deliveries"),
            func.count()
            .filter(FederationOutbox.status.in_(("failed", "expired")))
            .label("failed_deliveries"),
        )
        .group_by(FederationOutbox.destination)
        .subquery()
    )
    statement = select(
        Instance.domain,
        Instance.display_name,
        Instance.software_version,
        Instance.last_seen_at,
        Instance.federation_inbox_events.label("retained_events"),
        Instance.federation_inbox_event_bytes.label("event_storage_bytes"),
    ).where(Instance.is_self.is_(False))
    for aggregate in (users, messages, guilds, dms, media, deliveries):
        statement = statement.outerjoin(aggregate, aggregate.c.domain == Instance.domain)
        statement = statement.add_columns(
            *(
                func.coalesce(column, 0).label(column.key)
                for column in aggregate.c
                if column.key != "domain"
            )
        )
    return statement.order_by(Instance.last_seen_at.desc().nulls_last(), Instance.domain)
