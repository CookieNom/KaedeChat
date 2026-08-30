from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.auth import worker_runtime_ready
from app.bots.dm_capability import usable_dm_capability
from app.bots.installations import usable_guild_installation
from app.bots.runtime_control import application_runtime_projection_exists
from app.bots.worker_targets import worker_target_allowed_expression
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES
from app.core.settings import Settings
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallation,
    BotWorker,
)
from app.db.models import Channel, DMConversation, User
from app.voice.livekit import LiveKitControl, LiveKitError
from app.voice.rooms import guild_room_name, parse_participant_identity, participant_identity
from app.voice.state import (
    bot_guild_voice_connection_claims,
    bump_generation,
    call_bot_capability_bindings,
    get_active_call,
    occupant_in_room,
    release_voice_connection,
    remove_occupant,
    remove_occupant_connection,
    room_occupants,
    voice_connection_claim,
)


class MediaSessionRotationError(RuntimeError):
    """The old media session could not be safely fenced."""


BotVoiceRuntimeGrant = BotInstallation | BotDMCapability


def bot_voice_lineage_metadata(
    worker: BotWorker,
    grant: BotVoiceRuntimeGrant,
) -> dict[str, object]:
    """Bind a media token to the exact worker and runtime authorization row."""

    common: dict[str, object] = {
        "bot_application_id": str(grant.application_id),
        "bot_application_domain": grant.application_domain,
        "bot_worker_id": worker.id,
    }
    if isinstance(grant, BotDMCapability):
        return {
            **common,
            "bot_dm_capability_grant_id": grant.grant_id,
            "bot_dm_capability_revision": grant.revision,
            "bot_installation_ref": (
                f"{grant.source_installation_id}@{grant.source_installation_domain}"
            ),
            "bot_installation_type": grant.source_kind,
        }
    return {
        **common,
        "bot_installation_id": grant.id,
        "bot_installation_revision": grant.grant_revision,
    }


def _bot_voice_lineage_identity(
    identity: str,
    metadata: dict[str, object],
) -> tuple[int, str, int, int, str] | None:
    application_id = metadata.get("bot_application_id")
    application_domain = metadata.get("bot_application_domain")
    worker_id = metadata.get("bot_worker_id")
    if (
        not isinstance(application_id, str)
        or not application_id.isascii()
        or not application_id.isdecimal()
        or application_id.startswith("0")
        or not isinstance(application_domain, str)
        or type(worker_id) is not int
    ):
        return None
    try:
        bot_user_id, bot_user_domain = parse_participant_identity(identity)
    except ValueError:
        return None
    return (
        int(application_id),
        application_domain,
        worker_id,
        bot_user_id,
        bot_user_domain,
    )


async def active_bot_guild_voice_installation(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    identity: str,
    metadata: dict[str, object],
) -> BotInstallation | None:
    """Revalidate the exact guild installation and worker at media admission."""

    installation_id = metadata.get("bot_installation_id")
    revision = metadata.get("bot_installation_revision")
    lineage = _bot_voice_lineage_identity(identity, metadata)
    if type(installation_id) is not int or type(revision) is not int or lineage is None:
        return None
    application_id, application_domain, worker_id, bot_user_id, bot_user_domain = lineage
    return cast(
        BotInstallation | None,
        await session.scalar(
            select(BotInstallation)
            .join(
                BotWorker,
                (BotWorker.id == worker_id)
                & (BotWorker.application_id == BotInstallation.application_id)
                & (BotWorker.application_domain == BotInstallation.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == BotInstallation.application_id)
                & (BotApplication.origin_domain == BotInstallation.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotInstallation.id == installation_id,
                BotInstallation.grant_revision == revision,
                BotInstallation.application_id == application_id,
                BotInstallation.application_domain == application_domain,
                BotInstallation.bot_user_id == bot_user_id,
                BotInstallation.bot_user_domain == bot_user_domain,
                BotInstallation.guild_id == guild_id,
                BotInstallation.guild_domain == settings.domain,
                usable_guild_installation(),
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                worker_target_allowed_expression(settings.domain),
                BotApplication.status == "active",
                or_(
                    BotApplication.origin_domain == settings.domain,
                    application_runtime_projection_exists(settings.domain),
                ),
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
        ),
    )


async def active_bot_dm_voice_capability(
    session: AsyncSession,
    settings: Settings,
    call: dict[str, object],
    identity: str,
    metadata: dict[str, object],
) -> BotDMCapability | None:
    """Resolve the exact live proof embedded in a bot's minted media token."""

    grant_id = metadata.get("bot_dm_capability_grant_id")
    revision = metadata.get("bot_dm_capability_revision")
    lineage = _bot_voice_lineage_identity(identity, metadata)
    if not isinstance(grant_id, str) or type(revision) is not int or lineage is None:
        return None
    binding = call_bot_capability_bindings(call).get(identity)
    if binding != {"grant_id": grant_id, "revision": revision}:
        return None
    application_id, application_domain, worker_id, user_id, user_domain = lineage
    row = (
        await session.execute(
            select(BotDMCapability, BotWorker, BotApplication)
            .join(
                BotWorker,
                (BotWorker.id == worker_id)
                & (BotWorker.application_id == BotDMCapability.application_id)
                & (BotWorker.application_domain == BotDMCapability.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == BotDMCapability.application_id)
                & (BotApplication.origin_domain == BotDMCapability.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotDMCapability.grant_id == grant_id,
                BotDMCapability.revision == revision,
                BotDMCapability.bot_user_id == user_id,
                BotDMCapability.bot_user_domain == user_domain,
                BotDMCapability.application_id == application_id,
                BotDMCapability.application_domain == application_domain,
                BotDMCapability.authority_domain == settings.domain,
                BotDMCapability.conversation_id == int(str(call["channel_id"])),
                BotDMCapability.conversation_domain == str(call["channel_domain"]),
                usable_dm_capability(at=datetime.now(UTC)),
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
                worker_target_allowed_expression(settings.domain),
                BotApplication.status == "active",
                or_(
                    BotApplication.origin_domain == settings.domain,
                    application_runtime_projection_exists(settings.domain),
                ),
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    capability, worker, application = row
    runtime_target = await session.scalar(
        select(BotApplicationTarget).where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == settings.domain,
        )
    )
    if not worker_runtime_ready(
        application,
        worker,
        runtime_target,
        target_domain=settings.domain,
        dm_capability=capability,
    ):
        return None
    return cast(BotDMCapability, capability)


def _voice_lineage_matches(
    lineage: dict[str, object],
    *,
    application_ref: tuple[int, str] | None,
    worker_ids: set[int],
    installation_ids: set[int],
    capability_grant_ids: set[str],
    device_protocol_ids: set[str],
) -> bool:
    if application_ref is not None and (
        lineage.get("bot_application_id") != str(application_ref[0])
        or lineage.get("bot_application_domain") != application_ref[1]
    ):
        return False
    selected = bool(worker_ids or installation_ids or capability_grant_ids or device_protocol_ids)
    if not selected:
        return application_ref is not None
    return bool(
        (type(lineage.get("bot_worker_id")) is int and lineage["bot_worker_id"] in worker_ids)
        or (
            type(lineage.get("bot_installation_id")) is int
            and lineage["bot_installation_id"] in installation_ids
        )
        or lineage.get("bot_dm_capability_grant_id") in capability_grant_ids
        or lineage.get("bot_e2ee_device_id") in device_protocol_ids
    )


async def evict_bot_voice_runtime_sessions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    application_ref: tuple[int, str] | None = None,
    worker_ids: Iterable[int] = (),
    installation_ids: Iterable[int] = (),
    capability_grant_ids: Iterable[str] = (),
    device_protocol_ids: Iterable[str] = (),
) -> set[str]:
    """Fence exact bot media claims, including tokens not joined to LiveKit yet."""

    worker_id_set = set(worker_ids)
    installation_id_set = set(installation_ids)
    capability_id_set = set(capability_grant_ids)
    device_id_set = set(device_protocol_ids)
    if application_ref is None and not (
        worker_id_set or installation_id_set or capability_id_set or device_id_set
    ):
        return set()

    identity_refs: set[tuple[int, str]] = set()
    if application_ref is not None:
        application = await session.get(BotApplication, application_ref)
        if application is not None:
            identity_refs.add((application.bot_user_id, application.bot_user_domain))
    if installation_id_set:
        identity_refs.update(
            (user_id, user_domain)
            for user_id, user_domain in await session.execute(
                select(BotInstallation.bot_user_id, BotInstallation.bot_user_domain).where(
                    BotInstallation.id.in_(installation_id_set)
                )
            )
        )
    if capability_id_set:
        identity_refs.update(
            (user_id, user_domain)
            for user_id, user_domain in await session.execute(
                select(BotDMCapability.bot_user_id, BotDMCapability.bot_user_domain).where(
                    BotDMCapability.grant_id.in_(capability_id_set)
                )
            )
        )
    if not identity_refs:
        return set()

    control = LiveKitControl(settings) if settings.voice_enabled else None
    changed_rooms: set[str] = set()
    control_failed = False
    for user_id, user_domain in sorted(identity_refs, key=lambda item: (item[1], item[0])):
        identity = participant_identity(user_id, user_domain)
        claims = await bot_guild_voice_connection_claims(redis, settings.domain, identity)
        legacy_claim = await voice_connection_claim(redis, settings.domain, identity)
        if legacy_claim is not None and legacy_claim.get("client_kind") == "bot":
            claims.append(legacy_claim)
        for claim in claims:
            raw_lineage = claim.get("bot_lineage")
            if not isinstance(raw_lineage, str):
                continue
            try:
                parsed_lineage = json.loads(raw_lineage)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(parsed_lineage, dict) or not _voice_lineage_matches(
                parsed_lineage,
                application_ref=application_ref,
                worker_ids=worker_id_set,
                installation_ids=installation_id_set,
                capability_grant_ids=capability_id_set,
                device_protocol_ids=device_id_set,
            ):
                continue
            room = claim.get("room")
            connection_id = claim.get("connection_id")
            generation = claim.get("generation")
            if (
                not isinstance(room, str)
                or not isinstance(connection_id, str)
                or type(generation) is not int
            ):
                continue
            await bump_generation(redis, settings.domain, room, identity)
            occupant = await occupant_in_room(redis, settings.domain, room, identity)
            if (
                control is not None
                and occupant is not None
                and occupant.connection_id == connection_id
            ):
                try:
                    await control.remove_participant(room, identity)
                except LiveKitError:
                    control_failed = True
            await remove_occupant_connection(
                redis,
                settings.domain,
                room,
                identity,
                connection_id,
                generation=generation,
            )
            await release_voice_connection(
                redis,
                settings.domain,
                identity,
                connection_id,
                room=room,
                generation=generation,
                client_kind="bot",
            )
            changed_rooms.add(room)
    if changed_rooms:
        from app.core.task_wake import enqueue_best_effort
        from app.tasks import voice_replicate_room

        for room in sorted(changed_rooms):
            await enqueue_best_effort(voice_replicate_room, room)
    if control_failed:
        raise MediaSessionRotationError("could not evict a revoked bot media grant")
    return changed_rooms


async def evict_channel_media_sessions(
    redis: Redis,
    settings: Settings,
    channel: Channel,
    *,
    conversation: DMConversation | None = None,
) -> None:
    """Fence every grant and connection that can carry the channel's old key."""

    if not settings.voice_enabled:
        return
    rooms: set[str] = set()
    if channel.type in GUILD_VOICE_CHANNEL_TYPES and channel.guild_id is not None:
        rooms.add(guild_room_name(channel.guild_id, channel.id))
    elif conversation is not None and conversation.authority_domain == settings.domain:
        record = await get_active_call(redis, settings.domain, channel.id)
        if record is not None and record.get("state") != "ended":
            rooms.add(str(record["room"]))
    if not rooms:
        return

    try:
        control = LiveKitControl(settings)
        existing = {str(room.name) for room in await control.list_rooms()}
        for room in rooms:
            if room not in existing:
                continue
            occupants = await room_occupants(redis, settings.domain, room)
            # Fence the short-lived JWT before deleting the room. A delayed or
            # replayed join is then rejected by the admission webhook.
            for occupant in occupants:
                await bump_generation(redis, settings.domain, room, occupant.identity)
            await control.delete_room(room)
            for occupant in occupants:
                await remove_occupant(redis, settings.domain, room, occupant.identity)
                if occupant.connection_id:
                    await release_voice_connection(
                        redis,
                        settings.domain,
                        occupant.identity,
                        occupant.connection_id,
                        room=room,
                        client_kind=occupant.client_kind,
                    )
    except LiveKitError as exc:
        raise MediaSessionRotationError("could not fence the old media session") from exc
