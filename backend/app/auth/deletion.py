"""Resumable erasure using the existing message and media deletion paths."""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, or_, select, true, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.core.types import EntityReference
from app.db import models as m

log = structlog.get_logger()


def erase_message_body(message: m.Message) -> None:
    message.content = None
    message.e2ee = None
    message.embeds = []
    message.components = []
    message.sticker_items = []
    message.forward_snapshot = None
    message.poll_result = None
    message.mention_user_refs = []
    message.mention_role_refs = []
    message.mention_everyone = False


async def erase_content_batch(
    session: AsyncSession, redis: Redis, settings: Settings, user: m.User
) -> None:
    # Lazy imports avoid the API/task registration cycle.
    from app.api.channels import (
        commit_local_message_deletion,
        lock_message_delete_access,
        lock_message_delete_target,
        proxy_remote_dm_message_operation,
        proxy_remote_guild_message_operation,
    )
    from app.chat.channel_access import ChannelAccess
    from app.core.task_wake import enqueue_best_effort
    from app.media.service import discard_attachment
    from app.media.tombstones import lock_media_tombstone_ref, queue_terminal_attachment_tombstone
    from app.tasks import federation_deliver, media_local_purge

    state = dict(user.content_deletion or {})
    cutoff = datetime.fromisoformat(state["before"])
    messages = list(
        await session.scalars(
            select(m.Message)
            .where(
                m.Message.author_id == user.id,
                m.Message.author_domain == user.origin_domain,
                true() if user.deleted_at is not None else m.Message.created_at <= cutoff,
                m.Message.deleted_at.is_(None),
            )
            .order_by(m.Message.id, m.Message.origin_domain)
            .limit(25)
        )
    )
    message_refs = [(message.id, message.origin_domain) for message in messages]
    for message_id, message_domain in message_refs:
        message = await session.get(m.Message, (message_id, message_domain))
        if message is None:
            continue
        channel = await session.get(m.Channel, (message.channel_id, message.channel_domain))
        if channel is None:
            raise RuntimeError("Message channel missing")
        guild = (
            await session.get(m.Guild, (channel.guild_id, channel.guild_domain))
            if channel.guild_id
            else None
        )
        participants = (
            list(
                await session.scalars(
                    select(m.User)
                    .join(
                        m.DMParticipant,
                        (m.DMParticipant.user_id == m.User.id)
                        & (m.DMParticipant.user_domain == m.User.origin_domain),
                    )
                    .where(
                        m.DMParticipant.conversation_id == channel.id,
                        m.DMParticipant.conversation_domain == channel.origin_domain,
                    )
                )
            )
            if guild is None
            else []
        )
        access = ChannelAccess(channel, guild, participants)
        message_ref = EntityReference(message.id, message.origin_domain)
        if channel.origin_domain != settings.domain:
            # Remote authorities may reject old messages after a membership ends.
            # We still erase our replica; the status records unsuccessful requests.
            try:
                async with asyncio.timeout(5):
                    if guild is not None:
                        await proxy_remote_guild_message_operation(
                            session,
                            settings,
                            access,
                            user,
                            "message.delete",
                            message_ref=message_ref,
                        )
                    else:
                        await proxy_remote_dm_message_operation(
                            session, settings, access, user, "message.delete", message_ref
                        )
            except Exception:
                await session.rollback()
                state["remote_failures"] = int(state.get("remote_failures", 0)) + 1
                log.warning(
                    "account_content_remote_delete_failed",
                    message_id=message_ref.id,
                    domain=message_ref.domain,
                )
                # Rollback expires all ORM objects, including the user.
                await session.refresh(user)
                message = await session.get(m.Message, (message_ref.id, message_ref.domain))
            if message is None:
                continue
            erase_message_body(message)
            message.deleted_at = cutoff
            await session.execute(
                delete(m.Poll).where(
                    m.Poll.message_id == message.id, m.Poll.message_domain == message.origin_domain
                )
            )
            user.content_deletion = dict(state)
            await session.commit()
        else:
            access = await lock_message_delete_access(session, settings, access)
            message = await lock_message_delete_target(session, settings, access, message_ref)
            erase_message_body(message)
            await session.execute(
                delete(m.Poll).where(
                    m.Poll.message_id == message.id, m.Poll.message_domain == message.origin_domain
                )
            )
            await commit_local_message_deletion(session, redis, settings, access, user, message)
    if messages:
        return

    attachments = list(
        await session.scalars(
            select(m.Attachment)
            .where(
                m.Attachment.uploader_id == user.id,
                m.Attachment.uploader_domain == user.origin_domain,
                true() if user.deleted_at is not None else m.Attachment.created_at <= cutoff,
                m.Attachment.deleted_at.is_(None),
                m.Attachment.origin_domain == settings.domain,
            )
            .order_by(m.Attachment.id)
            .limit(25)
        )
    )
    for attachment in attachments:
        await lock_media_tombstone_ref(session, attachment.id, attachment.origin_domain)
        await session.refresh(attachment, with_for_update=True)
        if attachment.deleted_at is not None:
            continue
        await discard_attachment(session, settings, attachment)
        destinations = await queue_terminal_attachment_tombstone(session, settings, attachment)
        await session.commit()
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)
    if attachments:
        return

    if await erase_authored_tasks_batch(session, redis, settings, user, cutoff):
        return
    if await erase_authored_thread_titles_batch(session, redis, settings, user, cutoff):
        return
    state = dict(user.content_deletion or state)
    for model in (m.Reaction, m.PollVote):
        await session.execute(
            delete(model).where(
                model.user_id == user.id,
                model.user_domain == user.origin_domain,
                model.created_at <= cutoff,
            )
        )
    # Older single-message deletions may have retained ancillary body fields.
    authored = select(m.Message.id, m.Message.origin_domain).where(
        m.Message.author_id == user.id,
        m.Message.author_domain == user.origin_domain,
        true() if user.deleted_at is not None else m.Message.created_at <= cutoff,
    )
    await session.execute(
        delete(m.Poll).where(tuple_(m.Poll.message_id, m.Poll.message_domain).in_(authored))
    )
    await session.execute(
        update(m.Message)
        .where(
            m.Message.author_id == user.id,
            m.Message.author_domain == user.origin_domain,
            true() if user.deleted_at is not None else m.Message.created_at <= cutoff,
        )
        .values(
            content=None,
            e2ee=None,
            embeds=[],
            components=[],
            sticker_items=[],
            forward_snapshot=None,
            poll_result=None,
            mention_user_refs=[],
            mention_role_refs=[],
            mention_everyone=False,
        )
    )
    if user.deleted_at is not None:
        await erase_private_account_state(session, redis, settings, user)
    user.content_deletion = {**(user.content_deletion or state), "status": "complete"}
    await session.commit()


async def erase_private_account_state(
    session: AsyncSession, redis: Redis, settings: Settings, user: m.User
) -> None:
    from app.api.e2ee import queue_device_change_updates
    from app.api.guild_lifecycle import _remove_guild_membership
    from app.auth.service import revoke_user_sessions
    from app.chat.e2ee_membership import (
        pause_local_e2ee_for_device_change,
        publish_e2ee_policy_updates,
    )
    from app.core.task_wake import enqueue_best_effort
    from app.core.types import EntityRef
    from app.db.bot_models import (
        DeveloperTeamMember,
        DeveloperTeamMemberHighwater,
        InstanceAdminGrant,
    )
    from app.tasks import federation_deliver

    active_devices = list(
        await session.scalars(
            select(m.E2EEDevice).where(
                m.E2EEDevice.user_id == user.id,
                m.E2EEDevice.user_domain == user.origin_domain,
                m.E2EEDevice.revoked_at.is_(None),
            )
        )
    )
    if active_devices:
        for device in active_devices:
            device.revoked_at = user.deleted_at
        user.e2ee_device_generation += 1
        paused = await pause_local_e2ee_for_device_change(session, settings, user)
        destinations = await queue_device_change_updates(session, settings, user, paused)
        await session.commit()
        await publish_e2ee_policy_updates(session, redis, settings, paused)
        for destination in destinations:
            await enqueue_best_effort(federation_deliver, destination)

    memberships = list(
        (
            await session.execute(
                select(m.GuildMember.guild_id, m.GuildMember.guild_domain).where(
                    m.GuildMember.user_id == user.id,
                    m.GuildMember.user_domain == user.origin_domain,
                )
            )
        ).tuples()
    )
    for guild_id, domain in memberships:
        # Account cleanup must also leave quarantined/unavailable replicas.
        # Keep the guild lock and normal durable departure side effects.
        guild = await session.scalar(
            select(m.Guild)
            .where(m.Guild.id == guild_id, m.Guild.origin_domain == domain)
            .with_for_update()
        )
        if guild is None:
            continue  # A concurrent guild deletion also removes its memberships.
        member = await session.get(m.GuildMember, (guild_id, domain, user.id, user.origin_domain))
        if member is not None:
            await _remove_guild_membership(session, redis, settings, guild, user, member)
    from fastapi import Response

    from app.api.dependencies import federated_authenticated_user
    from app.api.dms import leave_group_direct_message
    from app.api.interactions import delete_user_installation
    from app.db.bot_models import BotUserInstallation
    from app.tasks import _worker_snowflake

    auth = federated_authenticated_user(user)
    groups = list(
        (
            await session.execute(
                select(m.DMConversation.id, m.DMConversation.origin_domain)
                .join(
                    m.DMParticipant,
                    (m.DMParticipant.conversation_id == m.DMConversation.id)
                    & (m.DMParticipant.conversation_domain == m.DMConversation.origin_domain),
                )
                .where(
                    m.DMParticipant.user_id == user.id,
                    m.DMParticipant.user_domain == user.origin_domain,
                    m.DMConversation.type == "group",
                )
            )
        ).tuples()
    )
    for group_id, domain in groups:
        if _worker_snowflake is None:
            raise RuntimeError("Account deletion requires the worker snowflake lease")
        try:
            async with asyncio.timeout(10):
                await leave_group_direct_message(
                    EntityRef(f"{group_id}@{domain}"),
                    Response(),
                    auth,
                    session,
                    redis,
                    _worker_snowflake,
                    settings,
                )
        except Exception:
            if domain == settings.domain:
                raise
            await session.rollback()
            await session.refresh(user)
            user.content_deletion = {
                **(user.content_deletion or {}),
                "remote_failures": (user.content_deletion or {}).get("remote_failures", 0) + 1,
            }
    installations = list(
        await session.scalars(
            select(BotUserInstallation.id).where(
                BotUserInstallation.user_id == user.id,
                BotUserInstallation.user_domain == user.origin_domain,
                BotUserInstallation.status != "revoked",
            )
        )
    )
    for installation_id in installations:
        if _worker_snowflake is None:
            raise RuntimeError("Account deletion requires the worker snowflake lease")
        await delete_user_installation(
            installation_id, auth, session, redis, _worker_snowflake, settings
        )
    await revoke_user_sessions(session, redis, settings, user)
    for model in (
        m.E2EEKeyPackage,
        m.E2EEAccountVaultDigest,
        m.E2EEAccountVault,
        m.E2EEDevice,
        m.OneTimeToken,
        m.RecoveryCode,
        m.PushDevice,
        m.UserSettings,
        m.GuildNotificationSetting,
        m.ReadState,
        m.InboxDismissal,
        m.AuthEvent,
        m.Session,
        m.DMParticipant,
        m.ThreadMember,
        m.GuildScheduledEventSubscription,
        DeveloperTeamMember,
        DeveloperTeamMemberHighwater,
        InstanceAdminGrant,
    ):
        await session.execute(
            delete(model).where(model.user_id == user.id, model.user_domain == user.origin_domain)
        )
    await session.execute(
        delete(m.Relationship).where(
            or_(
                (m.Relationship.user_id == user.id)
                & (m.Relationship.user_domain == user.origin_domain),
                (m.Relationship.target_id == user.id)
                & (m.Relationship.target_domain == user.origin_domain),
            )
        )
    )


async def erase_authored_tasks_batch(
    session: AsyncSession, redis: Redis, settings: Settings, user: m.User, cutoff: datetime
) -> bool:
    from app.api.dependencies import federated_authenticated_user
    from app.chat.channel_access import ChannelAccess, lock_local_channel_mutation
    from app.tracker.service import TrackerContext, commit_task_deletion, delete_task, ordered_tasks

    refs = list(
        (
            await session.execute(
                select(m.TrackerTask.id, m.TrackerTask.origin_domain)
                .where(
                    m.TrackerTask.creator_id == user.id,
                    m.TrackerTask.creator_domain == user.origin_domain,
                    true() if user.deleted_at is not None else m.TrackerTask.created_at <= cutoff,
                )
                .order_by(m.TrackerTask.id)
                .limit(25)
            )
        ).tuples()
    )
    for task_id, domain in refs:
        task = await session.get(m.TrackerTask, (task_id, domain))
        if task is None:
            continue
        channel = await session.get(m.Channel, (task.channel_id, task.channel_domain))
        guild = await session.get(m.Guild, (task.guild_id, task.guild_domain))
        if channel is None or guild is None:
            raise RuntimeError("Tracker task room missing")
        if domain != settings.domain:
            try:
                async with asyncio.timeout(5):
                    await delete_task(
                        session,
                        redis,
                        settings,
                        federated_authenticated_user(user),
                        EntityReference(channel.id, channel.origin_domain),
                        EntityReference(task_id, domain),
                        task.updated_at.isoformat(),
                    )
            except Exception:
                await session.rollback()
                await session.refresh(user)
                user.content_deletion = {
                    **(user.content_deletion or {}),
                    "remote_failures": (user.content_deletion or {}).get("remote_failures", 0) + 1,
                }
            await session.execute(
                delete(m.TrackerTask).where(
                    m.TrackerTask.id == task_id, m.TrackerTask.origin_domain == domain
                )
            )
            await session.commit()
            continue
        access = await lock_local_channel_mutation(
            session, settings, ChannelAccess(channel, guild, [])
        )
        board = await session.get(
            m.TrackerBoard, (channel.id, channel.origin_domain), with_for_update=True
        )
        if board is None:
            raise RuntimeError("Tracker board missing")
        tasks = await ordered_tasks(session, board, lock=True)
        target = next(
            (item for item in tasks if (item.id, item.origin_domain) == (task_id, domain)), None
        )
        if target is not None:
            await commit_task_deletion(
                session, settings, TrackerContext(access, board, 0), user, target, tasks
            )
    return bool(refs)


async def erase_authored_thread_titles_batch(
    session: AsyncSession, redis: Redis, settings: Settings, user: m.User, cutoff: datetime
) -> bool:
    from app.api.threads import proxy_remote_thread_mutation
    from app.chat.channel_access import (
        ChannelAccess,
        lock_local_channel_mutation,
        publish_channel_dispatch,
    )
    from app.chat.guild_revision import (
        federation_channel_state,
        queue_guild_mutation,
        wake_queued_guild_federation,
    )
    from app.chat.payloads import channel_payload
    from app.db.materialization import materialize_updated_at

    refs = list(
        (
            await session.execute(
                select(m.Channel.id, m.Channel.origin_domain)
                .where(
                    m.Channel.owner_id == user.id,
                    m.Channel.owner_domain == user.origin_domain,
                    m.Channel.type.in_((10, 11, 12)),
                    true() if user.deleted_at is not None else m.Channel.created_at <= cutoff,
                    or_(m.Channel.name != "Deleted post", m.Channel.topic.is_not(None)),
                )
                .order_by(m.Channel.id)
                .limit(25)
            )
        ).tuples()
    )
    for channel_id, domain in refs:
        channel = await session.get(m.Channel, (channel_id, domain))
        if channel is None:
            continue
        guild = await session.get(m.Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
            raise RuntimeError("Thread guild missing")
        access = ChannelAccess(channel, guild, [])
        if domain != settings.domain:
            try:
                async with asyncio.timeout(5):
                    await proxy_remote_thread_mutation(
                        session,
                        settings,
                        access,
                        user,
                        "thread.update",
                        payload={"name": "Deleted post"},
                    )
            except Exception:
                await session.rollback()
                await session.refresh(user)
                user.content_deletion = {
                    **(user.content_deletion or {}),
                    "remote_failures": (user.content_deletion or {}).get("remote_failures", 0) + 1,
                }
                channel = await session.get(m.Channel, (channel_id, domain))
        else:
            access = await lock_local_channel_mutation(session, settings, access)
            channel = access.channel
        if channel is None:
            continue
        # Keep other people's replies; remove the author's title and description.
        channel.name = "Deleted post"
        channel.topic = None
        if domain == settings.domain:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                user,
                "guild.channel.update",
                {"channel": federation_channel_state(channel)},
                channel=channel,
            )
        await materialize_updated_at(session, channel)
        rendered = channel_payload(channel)
        await session.commit()
        if domain == settings.domain:
            await wake_queued_guild_federation(guild)
            await publish_channel_dispatch(redis, access, "THREAD_UPDATE", rendered)
    return bool(refs)
