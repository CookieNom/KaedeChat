from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import and_, exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ChannelFollow,
    FederatedChannelFollow,
    FederatedMessageCrosspost,
    Message,
    MessageCrosspost,
)


async def announcement_dependencies_exist(
    session: AsyncSession,
    channel_refs: Collection[tuple[int, str]],
) -> bool:
    """Return whether deleting these channels could strand a follow or delivery."""

    refs = list(channel_refs)
    if not refs:
        return False
    local_binding = or_(
        tuple_(
            ChannelFollow.source_channel_id,
            ChannelFollow.source_channel_domain,
        ).in_(refs),
        tuple_(
            ChannelFollow.target_channel_id,
            ChannelFollow.target_channel_domain,
        ).in_(refs),
    )
    federated_binding = or_(
        tuple_(
            FederatedChannelFollow.source_channel_id,
            FederatedChannelFollow.source_channel_domain,
        ).in_(refs),
        tuple_(
            FederatedChannelFollow.target_channel_id,
            FederatedChannelFollow.target_channel_domain,
        ).in_(refs),
    )
    protected = await session.scalar(
        select(
            or_(
                exists().where(ChannelFollow.active.is_(True), local_binding),
                exists().where(
                    FederatedChannelFollow.lifecycle_state.in_({"pending", "accepted", "active"}),
                    federated_binding,
                ),
                select(1)
                .select_from(MessageCrosspost)
                .join(
                    Message,
                    and_(
                        Message.id == MessageCrosspost.source_message_id,
                        Message.origin_domain == MessageCrosspost.source_message_domain,
                    ),
                )
                .where(
                    Message.deleted_at.is_(None),
                    tuple_(Message.channel_id, Message.channel_domain).in_(refs),
                )
                .exists(),
                select(1)
                .select_from(FederatedMessageCrosspost)
                .join(
                    FederatedChannelFollow,
                    and_(
                        FederatedChannelFollow.id == FederatedMessageCrosspost.follow_id,
                        FederatedChannelFollow.target_authority_domain
                        == FederatedMessageCrosspost.follow_authority_domain,
                        FederatedChannelFollow.local_role == FederatedMessageCrosspost.local_role,
                    ),
                )
                .where(
                    FederatedMessageCrosspost.delivery_status.in_({"pending", "retry"}),
                    federated_binding,
                )
                .exists(),
                select(1)
                .select_from(FederatedMessageCrosspost)
                .join(
                    Message,
                    and_(
                        Message.id == FederatedMessageCrosspost.source_message_id,
                        Message.origin_domain == FederatedMessageCrosspost.source_message_domain,
                    ),
                )
                .where(
                    FederatedMessageCrosspost.local_role == "source",
                    FederatedMessageCrosspost.delivery_status == "delivered",
                    Message.deleted_at.is_(None),
                    tuple_(Message.channel_id, Message.channel_domain).in_(refs),
                )
                .exists(),
            )
        )
    )
    return bool(protected)


__all__ = ["announcement_dependencies_exist"]
