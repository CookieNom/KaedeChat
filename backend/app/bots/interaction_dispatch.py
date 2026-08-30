from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.dm_capability import capability_is_active
from app.bots.installations import (
    installation_allows_channel,
    usable_guild_installation,
    user_installation_is_usable,
)
from app.bots.interaction_owners import (
    BOT_DM_GUILD_OWNER,
    GUILD_INSTALL_OWNER,
    installation_authority_lineage,
    installation_authorizing_integration_owners,
    normalize_authorizing_integration_owners,
    stored_installation_authority_lineage,
    stored_interaction_event_snapshot,
)
from app.chat.events import guild_topic, publish_ephemeral_once, user_topic
from app.core.settings import Settings
from app.core.types import validate_entity_reference
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotInteraction,
    BotUserInstallation,
    InteractionCreateDispatchOutbox,
)
from app.db.models import Channel

log = structlog.get_logger()

INTERACTION_CREATE_OUTBOX_VERSION = 1
MAX_INTERACTION_CREATE_EVENT_BYTES = 1_000_000
INTERACTION_LOCALE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


class InteractionCreateDispatchError(ValueError):
    """The sealed dispatch cannot be authenticated against its interaction."""


def _canonical_event(event: dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InteractionCreateDispatchError("interaction event is not canonical JSON") from exc
    if not encoded or len(encoded) > MAX_INTERACTION_CREATE_EVENT_BYTES:
        raise InteractionCreateDispatchError("interaction event is too large")
    return encoded


def interaction_create_event_fingerprint(event: dict[str, object]) -> bytes:
    return hashlib.sha256(_canonical_event(event)).digest()


def _event_context(
    settings: Settings,
    interaction: BotInteraction,
    topic: str,
    audience_user_ref: str,
) -> bytes:
    return (
        f"kaede-interaction-create-outbox:v{INTERACTION_CREATE_OUTBOX_VERSION}:"
        f"{settings.domain}:{interaction.id}:{interaction.channel_domain}:"
        f"{topic}:{audience_user_ref}"
    ).encode()


def _event_token(event: dict[str, object]) -> str:
    token = event.get("token")
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise InteractionCreateDispatchError("interaction event token is malformed")
    return token


def _canonical_permission_string(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 20
    ):
        return False
    parsed = int(value)
    return str(parsed) == value and parsed <= (1 << 64) - 1


def _snapshot_actor_matches(
    interaction: BotInteraction,
    snapshot: dict[str, object],
) -> bool:
    guild_interaction = interaction.guild_id is not None
    raw_member = snapshot.get("member")
    raw_user = snapshot.get("user")
    if guild_interaction:
        if not isinstance(raw_member, dict) or raw_user is not None:
            return False
        raw_user = raw_member.get("user")
        if (
            raw_member.get("guild_id") != str(interaction.guild_id)
            or raw_member.get("guild_domain") != interaction.guild_domain
            or not _canonical_permission_string(raw_member.get("permissions"))
        ):
            return False
    elif raw_member is not None or not isinstance(raw_user, dict):
        return False
    return bool(
        isinstance(raw_user, dict)
        and raw_user.get("id") == str(interaction.user_id)
        and raw_user.get("origin_domain") == interaction.user_domain
    )


def _snapshot_message_matches(
    interaction: BotInteraction,
    snapshot: dict[str, object],
) -> bool:
    raw_message = snapshot.get("message")
    if interaction.message_id is None:
        stored_payload = interaction.payload if isinstance(interaction.payload, dict) else {}
        raw_response_id = stored_payload.get("response_id")
        if raw_response_id is None:
            return "message" not in snapshot
        return bool(
            isinstance(raw_message, dict)
            and raw_message.get("id") == str(raw_response_id)
            and raw_message.get("response_id") == str(raw_response_id)
            and raw_message.get("response_ref") == f"{raw_response_id}@{interaction.channel_domain}"
            and raw_message.get("channel_id") == str(interaction.channel_id)
            and raw_message.get("channel_domain") == interaction.channel_domain
            and raw_message.get("ephemeral") is True
            and raw_message.get("durable") is False
        )
    return bool(
        isinstance(raw_message, dict)
        and raw_message.get("id") == str(interaction.message_id)
        and raw_message.get("origin_domain") == interaction.message_domain
        and raw_message.get("channel_id") == str(interaction.channel_id)
        and raw_message.get("channel_domain") == interaction.channel_domain
    )


def _event_snapshot_matches(
    interaction: BotInteraction,
    event: dict[str, object],
) -> bool:
    """Bind every Discord-compatible delivery field to its durable snapshot."""

    try:
        snapshot = stored_interaction_event_snapshot(interaction)
    except ValueError:
        return False
    if snapshot is None:
        # Deployment compatibility for already-sealed interactions created
        # before snapshots existed. Every newly created row takes the strict path.
        return True
    required = {
        "version",
        "locale",
        "app_permissions",
        "authorizing_integration_owners",
        "attachment_size_limit",
        "entitlements",
    }
    if not required.issubset(snapshot) or any(
        key not in event or event[key] != value for key, value in snapshot.items()
    ):
        return False
    permissions = snapshot.get("app_permissions")
    attachment_limit = snapshot.get("attachment_size_limit")
    locale = snapshot.get("locale")
    guild_locale = snapshot.get("guild_locale")
    try:
        owners = normalize_authorizing_integration_owners(
            snapshot.get("authorizing_integration_owners")
        )
    except ValueError:
        return False
    guild_interaction = interaction.guild_id is not None
    return bool(
        type(snapshot.get("version")) is int
        and snapshot.get("version") == 1
        and isinstance(locale, str)
        and 2 <= len(locale) <= 16
        and INTERACTION_LOCALE_PATTERN.fullmatch(locale) is not None
        and _canonical_permission_string(permissions)
        and not isinstance(attachment_limit, bool)
        and isinstance(attachment_limit, int)
        and attachment_limit > 0
        and snapshot.get("entitlements") == []
        and owners == snapshot.get("authorizing_integration_owners")
        and (
            owners.get(GUILD_INSTALL_OWNER) != BOT_DM_GUILD_OWNER
            or interaction.context == "bot_dm"
            and interaction.integration_type == "dm_capability"
        )
        and ("member" in snapshot) == guild_interaction
        and ("user" in snapshot) != guild_interaction
        and ("guild_locale" in snapshot) == guild_interaction
        and (
            not guild_interaction
            or isinstance(guild_locale, str)
            and 2 <= len(guild_locale) <= 16
            and INTERACTION_LOCALE_PATTERN.fullmatch(guild_locale) is not None
        )
        and ("member" in event) == guild_interaction
        and ("user" in event) != guild_interaction
        and ("guild_locale" in event) == guild_interaction
        and ("message" in event) == ("message" in snapshot)
        and _snapshot_actor_matches(interaction, snapshot)
        and _snapshot_message_matches(interaction, snapshot)
    )


def _event_authorizes_installation(
    interaction: BotInteraction,
    event: dict[str, object],
    installation: BotInstallation | BotUserInstallation | BotDMCapability,
) -> bool:
    try:
        snapshot = stored_interaction_event_snapshot(interaction)
        if snapshot is None:
            return True
        owners = normalize_authorizing_integration_owners(
            event.get("authorizing_integration_owners")
        )
        selected = installation_authorizing_integration_owners(installation)
    except ValueError:
        return False
    return all(owners.get(key) == value for key, value in selected.items())


def _installation_matches_admission_lineage(
    interaction: BotInteraction,
    installation: BotInstallation | BotUserInstallation | BotDMCapability,
) -> bool:
    """Compare only immutable grant identity, never live mutable configuration."""

    try:
        stored = stored_installation_authority_lineage(interaction)
        if stored is None:
            # Compatibility for admitted interactions created before private
            # lineage snapshots were introduced.
            return True
        current = installation_authority_lineage(installation)
    except (TypeError, ValueError):
        return False
    immutable_keys = {
        "integration_type",
        "installation_ref",
        "owner_ref",
        "application_ref",
        "bot_user_ref",
        "source_kind",
        "dm_capability_ref",
        "dm_capability_grant_id",
    }
    return all(stored.get(key) == current.get(key) for key in immutable_keys)


def _event_matches_interaction(
    interaction: BotInteraction,
    event: dict[str, object],
    audience_user_ref: str,
) -> bool:
    try:
        token = _event_token(event)
    except InteractionCreateDispatchError:
        return False
    expected_installation = (
        str(interaction.installation_id) if interaction.installation_id is not None else None
    )
    expected_user_installation = (
        str(interaction.user_installation_id)
        if interaction.user_installation_id is not None
        else None
    )
    expected_guild_ref = (
        f"{interaction.guild_id}@{interaction.guild_domain}"
        if interaction.guild_id is not None
        else None
    )
    expected_message_ref = (
        f"{interaction.message_id}@{interaction.message_domain}"
        if interaction.message_id is not None
        else None
    )
    stored_payload = interaction.payload if isinstance(interaction.payload, dict) else {}
    command = event.get("command")
    if interaction.command_id is not None:
        if (
            not isinstance(command, dict)
            or command.get("name") != interaction.command_name
            or command.get("type") != interaction.command_type
        ):
            return False
    elif command is not None:
        return False
    try:
        bot_ref = validate_entity_reference(str(event.get("bot_user_ref", "")))
    except ValueError:
        return False
    target_ref = stored_payload.get("target_ref")
    try:
        expected_target_id = (
            validate_entity_reference(str(target_ref)).id if target_ref is not None else None
        )
    except ValueError:
        return False
    return bool(
        interaction.token_hash is not None
        and secrets.compare_digest(
            interaction.token_hash,
            hashlib.sha256(token.encode()).digest(),
        )
        and event.get("id") == str(interaction.id)
        and event.get("interaction_ref") == f"{interaction.id}@{interaction.channel_domain}"
        and event.get("application_ref")
        == f"{interaction.application_id}@{interaction.application_domain}"
        and event.get("channel_ref") == f"{interaction.channel_id}@{interaction.channel_domain}"
        and event.get("user_ref") == f"{interaction.user_id}@{interaction.user_domain}"
        and bot_ref.domain is not None
        and str(bot_ref) == event.get("bot_user_ref")
        and str(bot_ref) == audience_user_ref
        and event.get("type") == interaction.interaction_type
        and event.get("context") == interaction.context
        and event.get("integration_type") == interaction.integration_type
        and event.get("installation_id") == expected_installation
        and event.get("user_installation_id") == expected_user_installation
        and event.get("installation_revision") == str(interaction.installation_revision)
        and event.get("guild_ref") == expected_guild_ref
        and event.get("message_ref") == expected_message_ref
        and event.get("command_id")
        == (str(interaction.command_id) if interaction.command_id is not None else None)
        and event.get("custom_id") == interaction.custom_id
        and event.get("autocomplete_generation")
        == (
            str(interaction.autocomplete_generation)
            if interaction.autocomplete_generation is not None
            else None
        )
        and event.get("encrypted_payload") == interaction.encrypted_payload
        and event.get("options")
        == (None if interaction.encrypted_payload is not None else stored_payload.get("options"))
        and event.get("focused_option")
        == (
            None
            if interaction.encrypted_payload is not None
            else stored_payload.get("focused_option")
        )
        and event.get("component_type")
        == (
            None
            if interaction.encrypted_payload is not None
            else stored_payload.get("component_type")
        )
        and event.get("values")
        == ([] if interaction.encrypted_payload is not None else stored_payload.get("values", []))
        and event.get("components")
        == (
            []
            if interaction.encrypted_payload is not None
            else stored_payload.get("components", [])
        )
        and event.get("response_id")
        == (
            None if interaction.encrypted_payload is not None else stored_payload.get("response_id")
        )
        and event.get("view_version")
        == (
            None
            if interaction.encrypted_payload is not None
            else stored_payload.get("view_version")
        )
        and event.get("target_ref")
        == (None if interaction.encrypted_payload is not None else target_ref)
        and event.get("target_id") == (None if expected_target_id is None else expected_target_id)
        and event.get("resolved")
        == (None if interaction.encrypted_payload is not None else stored_payload.get("resolved"))
        and event.get("source_component") == stored_payload.get("source_component")
        and event.get("source_modal") == stored_payload.get("source_modal")
        and _event_snapshot_matches(interaction, event)
        and event.get("expires_at") == interaction.expires_at.isoformat()
        and interaction.created_at is not None
        and event.get("ack_deadline") == (interaction.created_at + timedelta(seconds=3)).isoformat()
    )


def seal_interaction_create_event(
    settings: Settings,
    interaction: BotInteraction,
    topic: str,
    audience_user_ref: str,
    event: dict[str, object],
) -> tuple[bytes, bytes]:
    """Authenticate and encrypt the only copy of the raw callback token."""

    if not _event_matches_interaction(interaction, event, audience_user_ref):
        raise InteractionCreateDispatchError("interaction event does not match its durable row")
    plaintext = _canonical_event(event)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(settings.secret_key_bytes).encrypt(
        nonce,
        plaintext,
        _event_context(settings, interaction, topic, audience_user_ref),
    )
    return nonce + ciphertext, hashlib.sha256(plaintext).digest()


def unseal_interaction_create_event(
    settings: Settings,
    interaction: BotInteraction,
    row: InteractionCreateDispatchOutbox,
) -> dict[str, object]:
    encrypted = row.event_ciphertext
    if not 29 <= len(encrypted) <= MAX_INTERACTION_CREATE_EVENT_BYTES + 28:
        raise InteractionCreateDispatchError("sealed interaction event is malformed")
    try:
        plaintext = AESGCM(settings.secret_key_bytes).decrypt(
            encrypted[:12],
            encrypted[12:],
            _event_context(
                settings,
                interaction,
                row.topic,
                row.audience_user_ref,
            ),
        )
        decoded: Any = json.loads(plaintext)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise InteractionCreateDispatchError("sealed interaction event is invalid") from exc
    if (
        not isinstance(decoded, dict)
        or hashlib.sha256(plaintext).digest() != row.event_fingerprint
        or _canonical_event(decoded) != plaintext
        or not _event_matches_interaction(
            interaction,
            decoded,
            row.audience_user_ref,
        )
    ):
        raise InteractionCreateDispatchError("sealed interaction event binding is invalid")
    return {str(key): value for key, value in decoded.items()}


async def durable_interaction_create_binding_matches(
    session: AsyncSession,
    interaction: BotInteraction,
    row: InteractionCreateDispatchOutbox,
    event: dict[str, object],
    *,
    authority_domain: str,
) -> bool:
    """Rejoin the sealed event to its exact durable bot/install lineage."""

    application = await session.get(
        BotApplication,
        (interaction.application_id, interaction.application_domain),
        populate_existing=True,
    )
    if (
        application is None
        or application.status != "active"
        or event.get("bot_user_ref") != f"{application.bot_user_id}@{application.bot_user_domain}"
        or row.audience_user_ref != f"{application.bot_user_id}@{application.bot_user_domain}"
    ):
        return False
    channel = await session.get(
        Channel,
        (interaction.channel_id, interaction.channel_domain),
        populate_existing=True,
    )
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain)
        != (interaction.guild_id, interaction.guild_domain)
    ):
        return False
    if interaction.integration_type == "guild_install":
        guild_installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.id == interaction.installation_id,
                usable_guild_installation(),
            )
        )
        return bool(
            guild_installation is not None
            and (guild_installation.application_id, guild_installation.application_domain)
            == (interaction.application_id, interaction.application_domain)
            and (guild_installation.bot_user_id, guild_installation.bot_user_domain)
            == (application.bot_user_id, application.bot_user_domain)
            and (guild_installation.guild_id, guild_installation.guild_domain)
            == (interaction.guild_id, interaction.guild_domain)
            and await installation_allows_channel(session, guild_installation, channel)
            and _installation_matches_admission_lineage(interaction, guild_installation)
            and _event_authorizes_installation(interaction, event, guild_installation)
            and row.topic
            == guild_topic(guild_installation.guild_domain, guild_installation.guild_id)
        )
    expected_topic = user_topic(application.bot_user_domain, application.bot_user_id)
    if row.topic != expected_topic:
        return False
    if interaction.integration_type == "user_install":
        user_installation = await session.get(
            BotUserInstallation,
            interaction.user_installation_id,
            populate_existing=True,
        )
        return bool(
            user_installation is not None
            and user_installation_is_usable(
                user_installation,
                current_instance_domain=authority_domain,
            )
            and (user_installation.application_id, user_installation.application_domain)
            == (interaction.application_id, interaction.application_domain)
            and _installation_matches_admission_lineage(interaction, user_installation)
            and _event_authorizes_installation(interaction, event, user_installation)
        )
    if interaction.integration_type != "dm_capability":
        return False
    capability = await session.get(
        BotDMCapability,
        interaction.dm_capability_id,
        populate_existing=True,
    )
    return bool(
        capability is not None
        and capability_is_active(capability)
        and capability.authority_domain == interaction.channel_domain
        and (capability.application_id, capability.application_domain)
        == (interaction.application_id, interaction.application_domain)
        and (capability.bot_user_id, capability.bot_user_domain)
        == (application.bot_user_id, application.bot_user_domain)
        and (capability.target_user_id, capability.target_user_domain)
        == (interaction.user_id, interaction.user_domain)
        and (capability.conversation_id, capability.conversation_domain)
        == (interaction.channel_id, interaction.channel_domain)
        and event.get("bot_dm_capability_id") == capability.grant_id
        and event.get("bot_dm_capability_revision") == str(interaction.installation_revision)
        and event.get("installation_ref")
        == f"{capability.source_installation_id}@{capability.source_installation_domain}"
        and event.get("installation_type") == capability.source_kind
        and _installation_matches_admission_lineage(interaction, capability)
        and _event_authorizes_installation(interaction, event, capability)
    )


def queue_interaction_create_dispatch(
    session: AsyncSession,
    settings: Settings,
    interaction: BotInteraction,
    *,
    topic: str,
    audience_user_ref: str,
    event: dict[str, object],
) -> InteractionCreateDispatchOutbox:
    """Queue an exact event in the same transaction as its interaction."""

    ciphertext, fingerprint = seal_interaction_create_event(
        settings,
        interaction,
        topic,
        audience_user_ref,
        event,
    )
    interaction.dispatch_fingerprint = fingerprint
    row = InteractionCreateDispatchOutbox(
        interaction_id=interaction.id,
        topic=topic,
        audience_user_ref=audience_user_ref,
        event_ciphertext=ciphertext,
        event_fingerprint=fingerprint,
        expires_at=interaction.expires_at,
    )
    session.add(row)
    return row


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(60.0, 0.5 * (2 ** min(max(0, attempts - 1), 7))))


async def drain_interaction_create_dispatch_outbox(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    interaction_id: int | None = None,
    limit: int = 100,
) -> int:
    """Publish committed creates at least once with their original token."""

    if not 1 <= limit <= 500:
        raise ValueError("interaction create dispatch limit must be between 1 and 500")
    now = datetime.now(UTC)
    conditions: list[Any] = [InteractionCreateDispatchOutbox.dispatched_at.is_(None)]
    conditions.append(
        InteractionCreateDispatchOutbox.next_attempt_at <= now
        if interaction_id is None
        else InteractionCreateDispatchOutbox.interaction_id == interaction_id
    )
    statement = (
        select(InteractionCreateDispatchOutbox)
        .where(*conditions)
        .order_by(InteractionCreateDispatchOutbox.interaction_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(await session.scalars(statement))
    delivered = 0
    for row in rows:
        interaction = await session.get(
            BotInteraction,
            row.interaction_id,
            populate_existing=True,
        )
        if (
            interaction is None
            or row.expires_at <= now
            or interaction.expires_at <= now
            or interaction.status not in {"pending", "deferred"}
        ):
            await session.delete(row)
            continue
        try:
            event = unseal_interaction_create_event(settings, interaction, row)
            if not await durable_interaction_create_binding_matches(
                session,
                interaction,
                row,
                event,
                authority_domain=settings.domain,
            ):
                raise InteractionCreateDispatchError("interaction event durable lineage is invalid")
        except InteractionCreateDispatchError:
            interaction.status = "failed" if interaction.status == "pending" else interaction.status
            await session.delete(row)
            log.exception(
                "interaction_create_dispatch_invalid",
                interaction_id=interaction.id,
            )
            continue
        try:
            published = await publish_ephemeral_once(
                redis,
                row.topic,
                "INTERACTION_CREATE",
                event,
                idempotency_key=(
                    f"interaction-create:{interaction.id}:{row.event_fingerprint.hex()}"
                ),
                ttl_seconds=max(
                    1,
                    min(86_400, int((interaction.expires_at - now).total_seconds()) + 60),
                ),
                audience_user_refs=(row.audience_user_ref,),
            )
        except Exception:
            published = None
            log.exception(
                "interaction_create_dispatch_failed",
                interaction_id=interaction.id,
                attempts=row.attempts,
            )
        if published:
            row.dispatched_at = now
            delivered += 1
        else:
            row.attempts += 1
            row.next_attempt_at = now + _retry_delay(row.attempts)
    await session.commit()
    return delivered


async def wake_interaction_create_dispatch_outbox() -> bool:
    """Wake the durable drain; its minute sweep is the final fallback."""

    try:
        from app.tasks import interaction_create_dispatch_outbox_drain
    except Exception:
        log.exception("interaction_create_dispatch_wake_unavailable")
        return False
    from app.core.task_wake import enqueue_best_effort

    return await enqueue_best_effort(interaction_create_dispatch_outbox_drain)
