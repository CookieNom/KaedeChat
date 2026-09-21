"""Operator-only delivery of local update notices; no HTTP or federation endpoint."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import user_topic
from app.chat.payloads import dm_channel_payload, message_payload
from app.chat.postcommit import queue_postcommit_dispatch
from app.core.dm import dm_pair_key
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import InstanceAdminGrant
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    Message,
    MessageProjection,
    SystemUpdateNotice,
    User,
)


class UpdateNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    current_revision: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,109}$")
    maintenance: Literal["required", "not_required", "unknown"]
    reasons: list[Literal["database", "infrastructure", "deployment", "unverified"]] = Field(
        default_factory=list, max_length=4
    )

    def message(self) -> str:
        status = {
            "required": "Maintenance required. Back up your instance and schedule downtime.",
            "not_required": "No maintenance requirement detected. A rolling update is available.",
            "unknown": "Maintenance requirements could not be verified. Review before updating.",
        }[self.maintenance]
        reasons = {
            "database": "Database migrations changed.",
            "infrastructure": "Infrastructure configuration changed.",
            "deployment": "Deployment code changed; review the update instructions.",
            "unverified": "The deployed revision or configuration could not be compared reliably.",
        }
        instructions = (
            "After backing up and updating the source checkout to this revision, "
            "run `make deploy MAINTENANCE=1`."
            if self.maintenance == "required"
            else "Review the changes and follow your instance's update instructions."
        )
        return "\n\n".join(
            [
                "**Kaede update available**",
                f"Installed: `{self.current_revision[:12]}` → Available: `{self.revision[:12]}`",
                status,
                " ".join(reasons[reason] for reason in self.reasons),
                instructions,
                "This is an automated notice from your instance. Replies are disabled.",
            ]
        )


async def deliver_update_notice(
    session: AsyncSession, settings: Settings, snowflake: SnowflakeGenerator, notice: UpdateNotice
) -> int:
    # Serialize the rare operator check so account creation and delivery receipts
    # are atomic even when manual checks overlap the host timer.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('kaede-update-notices'))"))
    now = datetime.now(UTC)
    owners = list(
        await session.scalars(
            select(User)
            .join(
                InstanceAdminGrant,
                (InstanceAdminGrant.user_id == User.id)
                & (InstanceAdminGrant.user_domain == User.origin_domain),
            )
            .where(
                InstanceAdminGrant.role == "owner",
                InstanceAdminGrant.revoked_at.is_(None),
                (InstanceAdminGrant.expires_at.is_(None) | (InstanceAdminGrant.expires_at > now)),
                User.is_local.is_(True),
                User.origin_domain == settings.domain,
                User.account_type == "human",
                User.disabled_at.is_(None),
                User.deleted_at.is_(None),
            )
            .with_for_update(of=InstanceAdminGrant)
        )
    )
    if not owners:
        return 0
    system = await session.scalar(select(User).where(User.account_type == "system"))
    if system is None:
        identifier = await snowflake.mint()
        system = User(
            id=identifier,
            origin_domain=settings.domain,
            is_local=True,
            account_type="system",
            username=f"system_{identifier}",
            display_name="Kaede System",
            bio="Official notices from this instance. This account cannot receive replies.",
        )
        session.add(system)
        await session.flush()
    count = 0
    for owner in owners:
        receipt_key = (notice.revision, owner.id, owner.origin_domain)
        if await session.get(SystemUpdateNotice, receipt_key) is not None:
            continue
        pair_key = dm_pair_key(
            f"{system.username}@{settings.domain}", f"{owner.username}@{settings.domain}"
        )
        conversation = await session.scalar(
            select(DMConversation).where(DMConversation.pair_key == pair_key)
        )
        channel: Channel | None
        if conversation is None:
            identifier = await snowflake.mint()
            channel = Channel(
                id=identifier, origin_domain=settings.domain, type=1, created_floor_id=identifier
            )
            session.add(channel)
            await session.flush()
            conversation = DMConversation(
                id=identifier,
                origin_domain=settings.domain,
                pair_key=pair_key,
                authority_domain=settings.domain,
                type="direct",
            )
            session.add(conversation)
            await session.flush()
            session.add_all(
                [
                    DMParticipant(
                        conversation_id=identifier,
                        conversation_domain=settings.domain,
                        user_id=user.id,
                        user_domain=settings.domain,
                    )
                    for user in (system, owner)
                ]
            )
            await session.flush()
        else:
            channel = await session.get(Channel, (conversation.id, settings.domain))
            if channel is None or channel.encryption_mode != "plaintext":
                raise ValueError("system notices require their dedicated plaintext conversation")
        channel.unavailable = False
        message = Message(
            id=await snowflake.mint(),
            origin_domain=settings.domain,
            channel_id=channel.id,
            channel_domain=settings.domain,
            author_id=system.id,
            author_domain=settings.domain,
            content=notice.message(),
            created_at=now,
        )
        session.add(message)
        await session.flush()
        channel.last_message_id = message.id
        channel.last_message_domain = settings.domain
        channel.updated_at = now
        session.add(
            MessageProjection(
                message_id=message.id,
                message_domain=settings.domain,
                channel_id=channel.id,
                channel_domain=settings.domain,
                mention_user_refs=[{"id": str(owner.id), "origin_domain": settings.domain}],
            )
        )
        session.add(
            SystemUpdateNotice(
                revision=notice.revision,
                user_id=owner.id,
                user_domain=settings.domain,
            )
        )
        topic = user_topic(settings.domain, owner.id)
        queue_postcommit_dispatch(
            session,
            topic,
            "CHANNEL_CREATE",
            dm_channel_payload(channel, [system], conversation=conversation),
        )
        queue_postcommit_dispatch(
            session, topic, "MESSAGE_CREATE", message_payload(message, system)
        )
        await session.flush()
        count += 1
    return count
