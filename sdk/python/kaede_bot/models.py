from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from ._encoding import encode_base64url
from .polls import PollResult
from .refs import EntityRef, User
from .wire import (
    strict_payload_bool as _strict_payload_bool,
    strict_payload_string as _strict_payload_string,
)

if TYPE_CHECKING:
    from .automod import (
        AutoModAction,
        AutoModEventType,
        AutoModRule,
        AutoModTriggerMetadata,
        AutoModTriggerType,
    )
    from .client import Client
    from .e2ee import (
        E2EEProvider,
        EncryptedRichMessage,
        InteractionE2EEContext,
        WebhookE2EEControlPage,
        WebhookE2EEDevice,
        WebhookE2EEDeviceChallenge,
        WebhookE2EEDeviceInventory,
        WebhookE2EEParticipationStatus,
    )
    from .embeds import Embed
    from .moderation import BulkBanResult, PruneEstimate, PruneResult
    from .polls import Poll
    from .soundboard import SoundboardSound
    from .ui import View
    from .voice import VoiceClient, VoiceE2EEContext, VoiceTransport


class MissingType:
    """Sentinel type used to distinguish omitted fields from explicit nulls."""

    __slots__ = ()


MISSING = MissingType()


@dataclass(frozen=True, slots=True)
class ChannelPositionUpdate:
    """One partial Discord-style guild channel position update."""

    channel: EntityRef
    position: int | None | MissingType = MISSING
    parent_id: int | None | MissingType = MISSING
    lock_permissions: bool | None | MissingType = MISSING
    flags: int | None | MissingType = MISSING


_INTERACTION_LOCALE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_INTERACTION_CONTEXTS = frozenset({"guild", "bot_dm", "private_channel"})
_INTERACTION_INTEGRATION_TYPES = frozenset(
    {"guild_install", "user_install", "dm_capability"}
)
_INTERACTION_TYPES = frozenset({"command", "component", "modal_submit", "autocomplete"})
_AUTHORIZING_INTEGRATION_TYPES = frozenset({"guild_install", "user_install"})
_MAX_PERMISSION_BITS = (1 << 64) - 1


def _canonical_decimal(
    value: object,
    *,
    field_name: str,
    maximum: int = (1 << 63) - 1,
) -> int:
    """Parse an unsigned JSON string without accepting Python coercions."""

    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError(f"{field_name} must be a canonical unsigned decimal string")
    parsed = int(value)
    if parsed > maximum:
        raise ValueError(f"{field_name} is outside the supported range")
    return parsed


def _optional_permission_bits(payload: Mapping[str, object], key: str) -> int | None:
    raw = payload.get(key)
    if raw is None:
        return None
    return _canonical_decimal(
        raw,
        field_name=key,
        maximum=_MAX_PERMISSION_BITS,
    )


def _interaction_locale(value: object, *, field_name: str) -> str:
    # Discord currently emits its finite locale enum. Kaede additionally keeps
    # a federated user's validated BCP-47-style locale intact across authorities.
    if (
        not isinstance(value, str)
        or not 2 <= len(value) <= 16
        or _INTERACTION_LOCALE_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"interaction {field_name} is not a valid locale")
    return value


def _authorizing_integration_owners(
    value: object,
) -> dict[str, EntityRef | Literal["0"]]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 2:
        raise ValueError(
            "interaction authorizing_integration_owners must contain one or two owners"
        )
    owners: dict[str, EntityRef | Literal["0"]] = {}
    for raw_key, raw_owner in value.items():
        if raw_key not in _AUTHORIZING_INTEGRATION_TYPES or not isinstance(
            raw_owner, str
        ):
            raise ValueError(
                "interaction authorizing_integration_owners contains an invalid owner"
            )
        if raw_key == "guild_install" and raw_owner == "0":
            owners[cast(str, raw_key)] = "0"
            continue
        try:
            owner = EntityRef.parse(raw_owner)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interaction authorizing_integration_owners contains an invalid owner"
            ) from exc
        owners[cast(str, raw_key)] = owner
    return owners


def _datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_ref(
    payload: dict[str, Any], id_key: str, domain_key: str
) -> EntityRef | None:
    raw_id = payload.get(id_key)
    domain = payload.get(domain_key)
    if raw_id is None and domain is None:
        return None
    if raw_id is None or domain is None:
        raise ValueError(f"{id_key}/{domain_key} reference is incomplete")
    return EntityRef.from_wire(raw_id, domain)


def _qualified_ref(payload: dict[str, Any], *keys: str) -> EntityRef | None:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, str):
            return EntityRef.parse(raw)
    return None


def _bind_optional_context(
    model: object,
    values: dict[str, object | None],
    *,
    context: str,
    reject_unasserted: bool = False,
) -> None:
    """Merge trusted parent/request context without accepting contradictions."""

    for field_name, asserted_value in values.items():
        current_value = getattr(model, field_name)
        if current_value is not None and (
            (reject_unasserted and asserted_value is None)
            or (asserted_value is not None and current_value != asserted_value)
        ):
            raise ValueError(f"{context} conflicts with its parent {field_name}")
        if current_value is None:
            setattr(model, field_name, asserted_value)


@dataclass(slots=True)
class AuditLogChange:
    key: str
    old_value: Any = MISSING
    new_value: Any = MISSING
    added: Any = MISSING
    removed: Any = MISSING

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuditLogChange:
        return cls(
            key=str(payload["key"]),
            old_value=payload.get("old_value", MISSING),
            new_value=payload.get("new_value", MISSING),
            added=payload.get("added", MISSING),
            removed=payload.get("removed", MISSING),
        )


@dataclass(slots=True)
class AuditLogEntry:
    id: int
    guild_ref: EntityRef
    actor_ref: EntityRef
    action_type: int
    target_type: str | None
    target_ref: dict[str, Any] | None
    reason: str | None
    changes: tuple[AuditLogChange, ...]
    created_at: datetime | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuditLogEntry:
        return cls(
            id=int(payload["id"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            actor_ref=EntityRef.from_wire(payload["actor_id"], payload["actor_domain"]),
            action_type=int(payload["action_type"]),
            target_type=(
                str(payload["target_type"])
                if payload.get("target_type") is not None
                else None
            ),
            target_ref=(
                dict(payload["target_ref"])
                if isinstance(payload.get("target_ref"), dict)
                else None
            ),
            reason=(
                str(payload["reason"]) if payload.get("reason") is not None else None
            ),
            changes=tuple(
                AuditLogChange.from_payload(item)
                for item in payload.get("changes", ())
                if isinstance(item, dict) and "key" in item
            ),
            created_at=_datetime(payload.get("created_at")),
        )


@dataclass(slots=True)
class ChannelOverwrite:
    target_ref: EntityRef
    target_type: str
    allow: int
    deny: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ChannelOverwrite:
        return cls(
            target_ref=EntityRef.from_wire(
                payload["target_id"], payload["target_domain"]
            ),
            target_type=str(payload["target_type"]),
            allow=int(payload.get("allow", 0)),
            deny=int(payload.get("deny", 0)),
        )


@dataclass(slots=True)
class Guild:
    client: Client
    target: str
    ref: EntityRef
    name: str
    description: str | None = None
    icon_hash: str | None = None
    banner_hash: str | None = None
    owner_ref: EntityRef | None = None
    unavailable: bool = False
    sync_status: str | None = None
    permissions: int | None = None
    installation_id: int | None = None
    installation_revision: int | None = None
    granted_scopes: tuple[str, ...] = ()
    granted_intents: tuple[str, ...] = ()
    channel_restrictions: tuple[EntityRef, ...] = ()
    e2ee_mode: str | None = None
    version: str | None = None
    permission_generation: int = 1
    federated_history_policy: str = "disabled"
    history_policy_generation: int = 1
    sync_error_code: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Guild:
        ref = EntityRef.from_wire(payload["id"], payload["origin_domain"])
        owner_domain = payload.get("owner_domain")
        owner_ref = (
            EntityRef.from_wire(payload["owner_id"], owner_domain)
            if payload.get("owner_id") is not None and isinstance(owner_domain, str)
            else None
        )
        raw_installation_id = payload.get("installation_id")
        installation_id = (
            _canonical_decimal(
                raw_installation_id,
                field_name="guild installation ID",
            )
            if raw_installation_id is not None
            else None
        )
        raw_installation_revision = payload.get("capability_revision")
        installation_revision = (
            _canonical_decimal(
                raw_installation_revision,
                field_name="guild installation capability revision",
            )
            if raw_installation_revision is not None
            else None
        )
        if installation_id == 0 or installation_revision == 0:
            raise ValueError("guild installation lineage must be positive")
        if installation_id is not None and installation_revision is None:
            raise ValueError(
                "guild response omitted its installation capability revision"
            )
        raw_restrictions = payload.get("channel_restrictions")
        if raw_restrictions is None:
            if installation_id is not None:
                raise ValueError(
                    "guild response omitted installation channel restrictions"
                )
            channel_restrictions: tuple[EntityRef, ...] = ()
        else:
            if not isinstance(raw_restrictions, list) or len(raw_restrictions) > 500:
                raise ValueError("guild response has invalid channel restrictions")
            try:
                parsed_restrictions = [
                    EntityRef.parse(item)
                    for item in raw_restrictions
                    if isinstance(item, str)
                ]
            except ValueError as exc:
                raise ValueError(
                    "guild response has invalid channel restrictions"
                ) from exc
            channel_restrictions = tuple(parsed_restrictions)
            if (
                len(channel_restrictions) != len(raw_restrictions)
                or any(
                    item.domain is None or item.domain != ref.domain
                    for item in channel_restrictions
                )
                or len(channel_restrictions) != len(set(channel_restrictions))
            ):
                raise ValueError("guild response has invalid channel restrictions")
        return cls(
            client=client,
            target=client._authority_target(ref, target),
            ref=ref,
            name=str(payload["name"]),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            icon_hash=(
                str(payload["icon_hash"])
                if payload.get("icon_hash") is not None
                else None
            ),
            banner_hash=(
                str(payload["banner_hash"])
                if payload.get("banner_hash") is not None
                else None
            ),
            owner_ref=owner_ref,
            unavailable=bool(payload.get("unavailable", False)),
            sync_status=(
                str(payload["sync_status"])
                if payload.get("sync_status") is not None
                else None
            ),
            permissions=(
                int(payload["permissions"])
                if payload.get("permissions") is not None
                else None
            ),
            installation_id=installation_id,
            installation_revision=installation_revision,
            granted_scopes=tuple(
                str(item) for item in payload.get("granted_scopes", [])
            ),
            granted_intents=tuple(
                str(item) for item in payload.get("granted_intents", [])
            ),
            channel_restrictions=channel_restrictions,
            e2ee_mode=(
                str(payload["e2ee_mode"])
                if payload.get("e2ee_mode") is not None
                else None
            ),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            permission_generation=int(str(payload.get("permission_generation", 1))),
            federated_history_policy=str(
                payload.get("federated_history_policy", "disabled")
            ),
            history_policy_generation=int(
                str(payload.get("history_policy_generation", 1))
            ),
            sync_error_code=(
                str(payload["sync_error_code"])
                if payload.get("sync_error_code") is not None
                else None
            ),
        )

    async def channels(self) -> list[Channel]:
        return await self.client.fetch_channels(self.ref, target=self.target)

    async def active_threads(self) -> ThreadPage:
        return await self.client.active_threads(self.ref, target=self.target)

    async def members(
        self,
        *,
        limit: int = 100,
        after: EntityRef | None = None,
        query: str | None = None,
    ) -> list[Member]:
        return await self.client.fetch_members(
            self.ref, target=self.target, limit=limit, after=after, query=query
        )

    async def roles(self) -> list[Role]:
        return await self.client.fetch_roles(self.ref, target=self.target)

    async def edit(
        self,
        *,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
    ) -> Guild:
        return await self.client.edit_guild(
            self.ref,
            target=self.target,
            version=self.version,
            reason=reason,
            name=name,
            description=description,
            federated_history_policy=federated_history_policy,
        )

    async def upload_asset(
        self,
        kind: Literal["icon", "banner"],
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Attachment:
        return await self.client.upload_guild_asset(
            self.ref,
            kind,
            data,
            target=self.target,
            filename=filename,
            content_type=content_type,
        )

    async def commit_asset(
        self,
        kind: Literal["icon", "banner"],
        attachment: EntityRef,
    ) -> Attachment:
        return await self.client.commit_guild_asset(
            self.ref,
            kind,
            attachment,
            target=self.target,
        )

    async def delete_asset(self, kind: Literal["icon", "banner"]) -> Guild:
        return await self.client.delete_guild_asset(
            self.ref,
            kind,
            target=self.target,
        )

    async def create_channel(
        self,
        name: str,
        *,
        reason: str | None = None,
        type: int = 0,
        topic: str | None = None,
        parent_id: int | None = None,
        rate_limit_per_user: int = 0,
        bitrate: int | MissingType = MISSING,
        user_limit: int | MissingType = MISSING,
        rtc_region: str | None | MissingType = MISSING,
        video_quality_mode: int | MissingType = MISSING,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
        tracker_key_prefix: str | MissingType = MISSING,
    ) -> Channel:
        return await self.client.create_channel(
            self.ref,
            name,
            target=self.target,
            reason=reason,
            type=type,
            topic=topic,
            parent_id=parent_id,
            rate_limit_per_user=rate_limit_per_user,
            bitrate=bitrate,
            user_limit=user_limit,
            rtc_region=rtc_region,
            video_quality_mode=video_quality_mode,
            default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
            default_auto_archive_duration=default_auto_archive_duration,
            available_tags=available_tags,
            default_reaction_emoji=default_reaction_emoji,
            default_sort_order=default_sort_order,
            default_forum_layout=default_forum_layout,
            flags=flags,
            e2ee_required=e2ee_required,
            tracker_key_prefix=tracker_key_prefix,
        )

    async def create_role(
        self,
        name: str,
        *,
        reason: str | None = None,
        permissions: int = 0,
        color: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
    ) -> Role:
        return await self.client.create_role(
            self.ref,
            name,
            target=self.target,
            reason=reason,
            permissions=permissions,
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )

    async def invites(self) -> list[Invite]:
        return await self.client.invites(self.ref, target=self.target)

    async def fetch_invite(self, code: str) -> Invite:
        return await self.client.fetch_invite(self.ref, code, target=self.target)

    async def command_permissions(self) -> list[ApplicationCommandPermissions]:
        return await self.client.command_permissions(self.ref, target=self.target)

    async def command_permission(
        self,
        command: EntityRef,
    ) -> ApplicationCommandPermissions:
        return await self.client.command_permission(
            self.ref,
            command,
            target=self.target,
        )

    async def scheduled_events(
        self, *, with_user_count: bool = False
    ) -> list[ScheduledEvent]:
        return await self.client.scheduled_events(
            self.ref,
            target=self.target,
            with_user_count=with_user_count,
        )

    async def fetch_scheduled_event(
        self,
        event: EntityRef,
        *,
        with_user_count: bool = False,
    ) -> ScheduledEvent:
        return await self.client.fetch_scheduled_event(
            self.ref,
            event,
            target=self.target,
            with_user_count=with_user_count,
        )

    async def create_scheduled_event(
        self,
        name: str,
        scheduled_start_time: datetime,
        *,
        entity_type: Literal[1, 2, 3],
        channel: EntityRef | None = None,
        location: str | None = None,
        scheduled_end_time: datetime | None = None,
        description: str | None = None,
        recurrence_rule: ScheduledEventRecurrenceRule | None = None,
        reason: str | None = None,
    ) -> ScheduledEvent:
        return await self.client.create_scheduled_event(
            self.ref,
            name,
            scheduled_start_time,
            entity_type=entity_type,
            target=self.target,
            channel=channel,
            location=location,
            scheduled_end_time=scheduled_end_time,
            description=description,
            recurrence_rule=recurrence_rule,
            reason=reason,
        )

    async def create_invite(
        self,
        *,
        channel_id: int | None = None,
        max_uses: int | None = None,
        max_age_seconds: int | None = 86_400,
        temporary: bool = False,
        unique: bool = False,
        target_type: Literal["stream"] | None = None,
        target_user_id: EntityRef | None = None,
        scheduled_event_id: EntityRef | None = None,
        role_ids: Sequence[EntityRef] = (),
        target_user_ids: Sequence[EntityRef] = (),
    ) -> Invite:
        return await self.client.create_invite(
            self.ref,
            target=self.target,
            channel_id=channel_id,
            max_uses=max_uses,
            max_age_seconds=max_age_seconds,
            temporary=temporary,
            unique=unique,
            target_type=target_type,
            target_user_id=target_user_id,
            scheduled_event_id=scheduled_event_id,
            role_ids=role_ids,
            target_user_ids=target_user_ids,
        )

    async def webhooks(self) -> list[Webhook]:
        return await self.client.webhooks(self.ref, target=self.target)

    async def emojis(self) -> list[Emoji]:
        return await self.client.emojis(self.ref, target=self.target)

    async def fetch_emoji(self, emoji_id: int) -> Emoji:
        return await self.client.fetch_emoji(self.ref, emoji_id, target=self.target)

    async def stickers(self) -> list[Sticker]:
        return await self.client.stickers(self.ref, target=self.target)

    async def fetch_sticker(self, sticker_id: int) -> Sticker:
        return await self.client.fetch_sticker(self.ref, sticker_id, target=self.target)

    async def auto_mod_rules(self) -> list[AutoModRule]:
        return await self.client.auto_mod_rules(self.ref, target=self.target)

    async def fetch_auto_mod_rule(self, rule_id: int) -> AutoModRule:
        return await self.client.fetch_auto_mod_rule(
            self.ref, rule_id, target=self.target
        )

    async def create_auto_mod_rule(
        self,
        name: str,
        trigger_type: AutoModTriggerType,
        actions: Sequence[AutoModAction],
        *,
        event_type: AutoModEventType = "message_send",
        trigger_metadata: AutoModTriggerMetadata | None = None,
        enabled: bool = False,
        exempt_roles: Sequence[EntityRef] = (),
        exempt_channels: Sequence[EntityRef] = (),
        reason: str | None = None,
    ) -> AutoModRule:
        return await self.client.create_auto_mod_rule(
            self.ref,
            name,
            trigger_type,
            actions,
            target=self.target,
            event_type=event_type,
            trigger_metadata=trigger_metadata,
            enabled=enabled,
            exempt_roles=exempt_roles,
            exempt_channels=exempt_channels,
            reason=reason,
        )

    async def estimate_prune(
        self,
        *,
        days: int = 7,
        include_roles: Sequence[EntityRef] = (),
    ) -> PruneEstimate:
        return await self.client.estimate_prune(
            self.ref,
            target=self.target,
            days=days,
            include_roles=include_roles,
        )

    async def prune_members(
        self,
        *,
        days: int = 7,
        include_roles: Sequence[EntityRef] = (),
        compute_prune_count: bool = True,
        reason: str | None = None,
    ) -> PruneResult:
        return await self.client.prune_members(
            self.ref,
            target=self.target,
            days=days,
            include_roles=include_roles,
            compute_prune_count=compute_prune_count,
            reason=reason,
        )

    async def bulk_ban_members(
        self,
        users: Sequence[EntityRef],
        *,
        delete_message_seconds: int = 0,
        reason: str | None = None,
    ) -> BulkBanResult:
        return await self.client.bulk_ban_members(
            self.ref,
            users,
            target=self.target,
            delete_message_seconds=delete_message_seconds,
            reason=reason,
        )

    async def instance_bans(
        self,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[InstanceBan]:
        return await self.client.instance_bans(
            self.ref,
            target=self.target,
            after=after,
            limit=limit,
        )

    async def ban_instance(
        self,
        instance_domain: str,
        *,
        reason: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        await self.client.ban_instance(
            self.ref,
            instance_domain,
            target=self.target,
            reason=reason,
            expires_at=expires_at,
        )

    async def unban_instance(
        self,
        instance_domain: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self.client.unban_instance(
            self.ref,
            instance_domain,
            target=self.target,
            reason=reason,
        )

    def audit_logs(
        self,
        *,
        limit: int | None = None,
        page_size: int = 100,
        before: int | None = None,
        user: EntityRef | None = None,
        action_type: int | None = None,
        target_type: str | None = None,
    ) -> AsyncIterator[AuditLogEntry]:
        return self.client.audit_logs(
            self.ref,
            target=self.target,
            limit=limit,
            page_size=page_size,
            before=before,
            user=user,
            action_type=action_type,
            target_type=target_type,
        )

    async def soundboard_sounds(self) -> list[SoundboardSound]:
        return await self.client.soundboard_sounds(self.ref, target=self.target)

    async def open_dm(self, handle: str) -> Channel:
        if self.installation_id is None:
            raise ValueError("guild payload does not include a bot installation")
        return await self.client.open_dm(
            handle,
            installation_ref=EntityRef(self.installation_id, self.ref.domain),
            installation_type="guild",
            target=self.target,
        )


@dataclass(slots=True)
class ThreadMetadata:
    archived: bool
    auto_archive_duration: int
    archive_timestamp: datetime | None = None
    locked: bool = False
    invitable: bool | None = None
    create_timestamp: datetime | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ThreadMetadata:
        return cls(
            archived=bool(payload.get("archived", False)),
            auto_archive_duration=int(payload.get("auto_archive_duration", 1440)),
            archive_timestamp=_datetime(payload.get("archive_timestamp")),
            locked=bool(payload.get("locked", False)),
            invitable=(
                bool(payload["invitable"])
                if payload.get("invitable") is not None
                else None
            ),
            create_timestamp=_datetime(payload.get("create_timestamp")),
        )


@dataclass(slots=True)
class ThreadMember:
    thread_ref: EntityRef
    user_ref: EntityRef
    guild_ref: EntityRef | None = None
    join_timestamp: datetime | None = None
    flags: int = 0
    notification_level: str = "inherit"
    member: Member | None = None
    presence: dict[str, Any] | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_domain: str,
        default_thread: EntityRef | None = None,
        client: Client | None = None,
        target: str | None = None,
    ) -> ThreadMember:
        thread_id = payload.get("id", payload.get("thread_id"))
        thread_domain = payload.get("thread_domain")
        if thread_id is None and thread_domain is None:
            thread_ref = None
        elif thread_id is None or thread_domain is None:
            raise ValueError("thread member payload has an incomplete thread reference")
        else:
            thread_ref = EntityRef.from_wire(thread_id, thread_domain)
        if thread_ref is None:
            if default_thread is None:
                raise ValueError(
                    "thread member payload is missing its thread reference"
                )
            thread_ref = default_thread
        user_id = payload.get("user_id")
        if user_id is None and isinstance(payload.get("user"), dict):
            user_id = payload["user"].get("id")
        user_domain = payload.get("user_domain")
        if user_domain is None and isinstance(payload.get("user"), dict):
            user_domain = payload["user"].get("origin_domain")
        if user_id is None:
            raise ValueError("thread member payload is missing its user reference")
        return cls(
            thread_ref=thread_ref,
            user_ref=EntityRef.from_wire(
                user_id,
                default_domain if user_domain is None else user_domain,
            ),
            guild_ref=_optional_ref(payload, "guild_id", "guild_domain"),
            join_timestamp=_datetime(payload.get("join_timestamp")),
            flags=int(payload.get("flags", 0)),
            notification_level=str(payload.get("notification_level", "inherit")),
            member=(
                Member.from_payload(client, target, payload["member"])
                if client is not None
                and target is not None
                and isinstance(payload.get("member"), dict)
                else None
            ),
            presence=(
                dict(payload["presence"])
                if isinstance(payload.get("presence"), dict)
                else None
            ),
        )


@dataclass(slots=True)
class ForumTag:
    id: int
    name: str
    moderated: bool = False
    emoji_id: int | None = None
    emoji_domain: str | None = None
    emoji_name: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ForumTag:
        return cls(
            id=int(payload["id"]),
            name=str(payload["name"]),
            moderated=bool(payload.get("moderated", False)),
            emoji_id=(
                int(payload["emoji_id"])
                if payload.get("emoji_id") is not None
                else None
            ),
            emoji_domain=(
                str(payload["emoji_domain"])
                if payload.get("emoji_domain") is not None
                else None
            ),
            emoji_name=(
                str(payload["emoji_name"])
                if payload.get("emoji_name") is not None
                else None
            ),
        )


@dataclass(slots=True)
class Channel:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef | None
    type: int
    name: str | None = None
    topic: str | None = None
    position: int = 0
    parent_ref: EntityRef | None = None
    created_at: datetime | None = None
    permissions: int = 0
    rate_limit_per_user: int = 0
    bitrate: int | None = None
    user_limit: int | None = None
    rtc_region: str | None = None
    video_quality_mode: int | None = None
    flags: int = 0
    owner_ref: EntityRef | None = None
    last_message_ref: EntityRef | None = None
    starter_message_ref: EntityRef | None = None
    starter_message: Message | None = None
    starter_reservation: dict[str, Any] | None = None
    thread_metadata: ThreadMetadata | None = None
    member: ThreadMember | None = None
    message_count: int = 0
    total_message_sent: int = 0
    member_count: int = 0
    applied_tag_ids: tuple[int, ...] = ()
    available_tags: tuple[ForumTag, ...] = ()
    default_reaction_emoji: dict[str, Any] | None = None
    default_thread_rate_limit_per_user: int | None = None
    default_auto_archive_duration: int | None = None
    default_sort_order: int | None = None
    default_forum_layout: int | None = None
    e2ee_required: bool = False
    encryption_mode: str = "plaintext"
    search_available: bool = True
    newly_created: bool = False
    version: str | None = None
    bot_installation_id: int | None = None
    nsfw: bool = False
    permissions_synced: bool = False
    recipients: tuple[User, ...] = ()
    conversation_type: Literal["direct", "group"] | None = None
    federated_history_policy: str = "inherit"
    encryption_state: str = "plaintext"
    encryption_policy_generation: int = 0
    encryption_protocol: str | None = None
    encryption_suite: str | None = None
    encryption_group_id: str | None = None
    encryption_epoch: int | None = None
    encryption_activated_at: datetime | None = None
    encryption_policy: dict[str, Any] | None = None
    history_truncated: bool = False
    history_retention: str | None = None
    history_source: str | None = None
    history_remote_available: bool = False
    oldest_available_message_ref: EntityRef | None = None
    history_degraded_code: str | None = None
    dm_capability_id: str | None = None
    dm_capability_revision: int | None = None
    installation_ref: EntityRef | None = None
    installation_type: Literal["guild", "user"] | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Channel:
        ref = EntityRef.from_wire(payload["id"], payload["origin_domain"])
        authority_target = client._authority_target(ref, target)
        raw_thread_metadata = payload.get("thread_metadata")
        raw_member = payload.get("member")
        raw_tags = payload.get("available_tags")
        raw_applied_tags = payload.get(
            "applied_tags", payload.get("applied_tag_ids", [])
        )
        raw_recipients = payload.get("recipients")
        conversation_type = payload.get("conversation_type")
        if conversation_type is not None and conversation_type not in {
            "direct",
            "group",
        }:
            raise ValueError("channel response has an invalid conversation type")
        installation_type = payload.get(
            "bot_installation_type", payload.get("installation_type")
        )
        if installation_type is not None and installation_type not in {"guild", "user"}:
            raise ValueError("channel response has an invalid installation type")
        bot_installation_id = (
            int(payload["bot_installation_id"])
            if payload.get("bot_installation_id") is not None
            else None
        )
        dm_capability_id = (
            str(payload["bot_dm_capability_id"])
            if payload.get("bot_dm_capability_id") is not None
            else None
        )
        dm_capability_revision = (
            int(str(payload["bot_dm_capability_revision"]))
            if payload.get("bot_dm_capability_revision") is not None
            else None
        )
        installation_ref = _qualified_ref(
            payload, "bot_installation_ref", "installation_ref"
        )
        oldest_available = payload.get("oldest_available_message_ref")
        # Discord calls the atomic forum-create starter `message`; Kaede keeps
        # `starter_message` on ordinary channel projections for clarity.
        raw_starter_message = payload.get("starter_message") or payload.get("message")
        starter_message = (
            Message.from_payload(client, authority_target, raw_starter_message)
            if isinstance(raw_starter_message, dict)
            else None
        )
        if starter_message is not None:
            starter_message.bind_runtime(
                installation_id=bot_installation_id,
                dm_capability_id=dm_capability_id,
                dm_capability_revision=dm_capability_revision,
                installation_ref=installation_ref,
                installation_type=cast(
                    Literal["guild", "user"] | None, installation_type
                ),
                reject_unasserted=True,
            )
        return cls(
            client=client,
            target=authority_target,
            ref=ref,
            guild_ref=_optional_ref(payload, "guild_id", "guild_domain"),
            type=int(payload.get("type", 0)),
            name=str(payload["name"]) if payload.get("name") is not None else None,
            topic=str(payload["topic"]) if payload.get("topic") is not None else None,
            position=int(payload.get("position", 0)),
            parent_ref=_optional_ref(payload, "parent_id", "parent_domain"),
            created_at=_datetime(payload.get("created_at")),
            permissions=int(payload.get("permissions", 0)),
            rate_limit_per_user=int(payload.get("rate_limit_per_user", 0)),
            bitrate=(
                int(payload["bitrate"]) if payload.get("bitrate") is not None else None
            ),
            user_limit=(
                int(payload["user_limit"])
                if payload.get("user_limit") is not None
                else None
            ),
            rtc_region=(
                str(payload["rtc_region"])
                if payload.get("rtc_region") is not None
                else None
            ),
            video_quality_mode=(
                _strict_payload_int(
                    payload["video_quality_mode"], "channel video quality mode"
                )
                if payload.get("video_quality_mode") is not None
                else None
            ),
            flags=int(payload.get("flags") or 0),
            owner_ref=_optional_ref(payload, "owner_id", "owner_domain"),
            last_message_ref=_optional_ref(
                payload, "last_message_id", "last_message_domain"
            ),
            starter_message_ref=(
                _optional_ref(payload, "starter_message_id", "starter_message_domain")
                or _optional_ref(payload, "source_message_id", "source_message_domain")
                or (starter_message.ref if starter_message is not None else None)
            ),
            starter_message=starter_message,
            starter_reservation=(
                dict(payload["starter_reservation"])
                if isinstance(payload.get("starter_reservation"), dict)
                else None
            ),
            thread_metadata=(
                ThreadMetadata.from_payload(
                    raw_thread_metadata
                    if isinstance(raw_thread_metadata, dict)
                    else payload
                )
                if int(payload.get("type", 0)) in {10, 11, 12}
                else None
            ),
            member=(
                ThreadMember.from_payload(
                    raw_member,
                    default_domain=ref.domain,
                    default_thread=ref,
                    client=client,
                    target=target,
                )
                if isinstance(raw_member, dict)
                else None
            ),
            message_count=int(payload.get("message_count") or 0),
            total_message_sent=int(payload.get("total_message_sent") or 0),
            member_count=int(payload.get("member_count") or 0),
            applied_tag_ids=tuple(int(item) for item in raw_applied_tags or ()),
            available_tags=tuple(
                ForumTag.from_payload(item)
                for item in (raw_tags or ())
                if isinstance(item, dict)
            ),
            default_reaction_emoji=(
                dict(payload["default_reaction_emoji"])
                if isinstance(payload.get("default_reaction_emoji"), dict)
                else None
            ),
            default_thread_rate_limit_per_user=(
                int(payload["default_thread_rate_limit_per_user"])
                if payload.get("default_thread_rate_limit_per_user") is not None
                else None
            ),
            default_auto_archive_duration=(
                int(payload["default_auto_archive_duration"])
                if payload.get("default_auto_archive_duration") is not None
                else None
            ),
            default_sort_order=(
                int(payload["default_sort_order"])
                if payload.get("default_sort_order") is not None
                else None
            ),
            default_forum_layout=(
                int(payload["default_forum_layout"])
                if payload.get("default_forum_layout") is not None
                else None
            ),
            e2ee_required=bool(
                payload.get("e2ee_required", False)
                or payload.get("default_thread_encryption_mode") == "e2ee"
            ),
            encryption_mode=str(payload.get("encryption_mode", "plaintext")),
            search_available=bool(payload.get("search_available", True)),
            newly_created=bool(payload.get("newly_created", False)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            bot_installation_id=bot_installation_id,
            nsfw=_strict_payload_bool(payload, "nsfw", default=False),
            permissions_synced=_strict_payload_bool(
                payload, "permissions_synced", default=False
            ),
            recipients=tuple(
                User.from_payload(item)
                for item in (raw_recipients or ())
                if isinstance(item, dict)
            ),
            conversation_type=cast(
                Literal["direct", "group"] | None, conversation_type
            ),
            federated_history_policy=str(
                payload.get("federated_history_policy", "inherit")
            ),
            encryption_state=str(payload.get("encryption_state", "plaintext")),
            encryption_policy_generation=int(
                str(payload.get("encryption_policy_generation", 0))
            ),
            encryption_protocol=(
                str(payload["encryption_protocol"])
                if payload.get("encryption_protocol") is not None
                else None
            ),
            encryption_suite=(
                str(payload["encryption_suite"])
                if payload.get("encryption_suite") is not None
                else None
            ),
            encryption_group_id=(
                str(payload["encryption_group_id"])
                if payload.get("encryption_group_id") is not None
                else None
            ),
            encryption_epoch=(
                int(str(payload["encryption_epoch"]))
                if payload.get("encryption_epoch") is not None
                else None
            ),
            encryption_activated_at=_datetime(payload.get("encryption_activated_at")),
            encryption_policy=(
                dict(payload["encryption_policy"])
                if isinstance(payload.get("encryption_policy"), dict)
                else None
            ),
            history_truncated=_strict_payload_bool(
                payload, "history_truncated", default=False
            ),
            history_retention=(
                str(payload["history_retention"])
                if payload.get("history_retention") is not None
                else None
            ),
            history_source=(
                str(payload["history_source"])
                if payload.get("history_source") is not None
                else None
            ),
            history_remote_available=_strict_payload_bool(
                payload, "history_remote_available", default=False
            ),
            oldest_available_message_ref=(
                _optional_ref(oldest_available, "id", "origin_domain")
                if isinstance(oldest_available, dict)
                else None
            ),
            history_degraded_code=(
                str(payload["history_degraded_code"])
                if payload.get("history_degraded_code") is not None
                else None
            ),
            dm_capability_id=dm_capability_id,
            dm_capability_revision=dm_capability_revision,
            installation_ref=installation_ref,
            installation_type=cast(Literal["guild", "user"] | None, installation_type),
        )

    def bind_runtime(
        self,
        *,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        dm_capability_revision: int | None = None,
        installation_ref: EntityRef | None = None,
        installation_type: Literal["guild", "user"] | None = None,
        reject_unasserted: bool = False,
    ) -> Channel:
        """Pin one exact runtime grant to this channel projection."""

        if installation_id is not None and dm_capability_id is not None:
            raise ValueError(
                "installation and DM capability grants are mutually exclusive"
            )
        _bind_optional_context(
            self,
            {
                "bot_installation_id": installation_id,
                "dm_capability_id": dm_capability_id,
                "dm_capability_revision": dm_capability_revision,
                "installation_ref": installation_ref,
                "installation_type": installation_type,
            },
            context="channel response",
            reject_unasserted=reject_unasserted,
        )
        if self.starter_message is not None:
            self.starter_message.bind_runtime(
                installation_id=self.bot_installation_id,
                dm_capability_id=self.dm_capability_id,
                dm_capability_revision=self.dm_capability_revision,
                installation_ref=self.installation_ref,
                installation_type=self.installation_type,
                reject_unasserted=True,
            )
        return self

    @property
    def is_thread(self) -> bool:
        return self.type in {10, 11, 12}

    @property
    def is_forum(self) -> bool:
        return self.type == 15

    @property
    def is_tracker(self) -> bool:
        return self.type == 17

    @property
    def is_voice(self) -> bool:
        return self.type in {2, 13}

    @property
    def is_stage(self) -> bool:
        return self.type == 13

    @property
    def is_group_dm(self) -> bool:
        return self.type == 3 or self.conversation_type == "group"

    @property
    def archived(self) -> bool:
        return bool(self.thread_metadata and self.thread_metadata.archived)

    @property
    def locked(self) -> bool:
        return bool(self.thread_metadata and self.thread_metadata.locked)

    async def send(
        self,
        content: str | None = None,
        *,
        reply_to: EntityRef | None = None,
        attachment_ids: list[int] | None = None,
        attachment_manifests: Sequence[Mapping[str, object]] = (),
        sticker_ids: Sequence[EntityRef] = (),
        stickers: Sequence[Sticker] = (),
        mention_user_ids: Sequence[EntityRef] = (),
        resolved_mention_user_ids: Sequence[EntityRef] | None = None,
        allowed_mentions: Mapping[str, object] | None = None,
        replied_user_ref: EntityRef | None = None,
        e2ee: dict[str, Any] | None = None,
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        forward: EntityRef | Message | None = None,
        tts: bool = False,
        voice_message: bool = False,
        flags: int = 0,
    ) -> Message:
        return await self.client.send_message(
            self.ref,
            content,
            target=self.target,
            reply_to=reply_to,
            attachment_ids=attachment_ids,
            attachment_manifests=attachment_manifests,
            sticker_ids=list(sticker_ids),
            stickers=stickers,
            mention_user_ids=mention_user_ids,
            resolved_mention_user_ids=resolved_mention_user_ids,
            allowed_mentions=allowed_mentions,
            replied_user_ref=replied_user_ref,
            e2ee=e2ee,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
            embeds=list(embeds),
            view=view,
            poll=poll,
            forward=forward,
            tts=tts,
            voice_message=voice_message,
            flags=flags,
        )

    async def send_sticker(self, sticker: Sticker) -> Message:
        """Send one canonical sticker message in this channel."""
        return await self.client.send_sticker(
            self.ref,
            sticker,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def history(
        self, *, before: EntityRef | None = None, limit: int = 50
    ) -> list[Message]:
        return await self.client.history(
            self.ref,
            target=self.target,
            before=before,
            limit=limit,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def pins(self) -> list[Message]:
        return await self.client.pins(
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def pin_page(
        self, *, before: datetime | None = None, limit: int = 50
    ) -> MessagePinPage:
        return await self.client.pin_page(
            self.ref,
            target=self.target,
            before=before,
            limit=limit,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def trigger_typing(self) -> None:
        await self.client.trigger_typing(
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def voice_occupancy(self) -> VoiceOccupancy:
        return await self.client.voice_occupancy(
            self.ref,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )

    async def start_call(self, *, ring: bool = True) -> Call:
        if self.is_group_dm:
            raise ValueError("applications cannot start calls in group DMs")
        if self.type not in {1, 3} and self.conversation_type is None:
            raise ValueError("calls require a direct or group conversation")
        return await self.client.start_call(
            self.ref,
            target=self.target,
            ring=ring,
            dm_capability_id=self.dm_capability_id,
        )

    async def active_call(self) -> ActiveCall:
        if self.is_group_dm:
            raise ValueError("applications cannot join calls in group DMs")
        if self.type not in {1, 3} and self.conversation_type is None:
            raise ValueError("calls require a direct or group conversation")
        return await self.client.active_call(
            self.ref,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )

    async def connect_voice(
        self,
        *,
        listen: bool = False,
        speak: bool = False,
        stream: bool = False,
        takeover: bool = False,
        transport: VoiceTransport | None = None,
        e2ee_context: VoiceE2EEContext | None = None,
        call: EntityRef | Call | None = None,
    ) -> VoiceClient:
        if self.is_group_dm:
            raise ValueError("applications cannot connect to voice in group DMs")
        if not self.is_voice and self.type not in {1, 3}:
            raise ValueError("voice connections require a voice channel or DM call")
        return await self.client.connect_voice(
            self.ref,
            target=self.target,
            listen=listen,
            speak=speak,
            stream=stream,
            takeover=takeover,
            transport=transport,
            e2ee_context=e2ee_context,
            call=call,
            dm_capability_id=self.dm_capability_id,
        )

    async def tracker(self) -> TrackerBoard:
        if not self.is_tracker:
            raise ValueError("task trackers require a tracker channel")
        return await self.client.fetch_tracker(self.ref, target=self.target)

    async def edit(
        self,
        *,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        topic: str | None | MissingType = MISSING,
        parent_id: int | None | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        bitrate: int | MissingType = MISSING,
        user_limit: int | MissingType = MISSING,
        rtc_region: str | None | MissingType = MISSING,
        video_quality_mode: int | MissingType = MISSING,
        default_thread_rate_limit_per_user: int | MissingType = MISSING,
        default_auto_archive_duration: int | MissingType = MISSING,
        available_tags: list[dict[str, Any]] | MissingType = MISSING,
        default_reaction_emoji: dict[str, Any] | None | MissingType = MISSING,
        default_sort_order: int | None | MissingType = MISSING,
        default_forum_layout: int | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        e2ee_required: bool | MissingType = MISSING,
        federated_history_policy: str | MissingType = MISSING,
        sync_permissions: bool | MissingType = MISSING,
    ) -> Channel:
        if self.guild_ref is None:
            raise ValueError(
                "direct-message channels cannot be managed as guild channels"
            )
        return await self.client.edit_channel(
            self.guild_ref,
            self.ref,
            target=self.target,
            version=self.version,
            reason=reason,
            name=name,
            topic=topic,
            parent_id=parent_id,
            rate_limit_per_user=rate_limit_per_user,
            bitrate=bitrate,
            user_limit=user_limit,
            rtc_region=rtc_region,
            video_quality_mode=video_quality_mode,
            default_thread_rate_limit_per_user=default_thread_rate_limit_per_user,
            default_auto_archive_duration=default_auto_archive_duration,
            available_tags=available_tags,
            default_reaction_emoji=default_reaction_emoji,
            default_sort_order=default_sort_order,
            default_forum_layout=default_forum_layout,
            flags=flags,
            e2ee_required=e2ee_required,
            federated_history_policy=federated_history_policy,
            sync_permissions=sync_permissions,
            channel_type=self.type,
        )

    async def set_voice_status(
        self,
        status: str | None,
        *,
        reason: str | None = None,
    ) -> None:
        if self.guild_ref is None or self.type != 2:
            raise ValueError("voice status is available only on guild voice channels")
        await self.client.set_voice_channel_status(
            self.guild_ref,
            self.ref,
            status,
            target=self.target,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> Channel:
        if self.is_thread:
            return await self.client.delete_thread(
                self.ref,
                target=self.target,
                reason=reason,
                installation_id=self.bot_installation_id,
                dm_capability_id=self.dm_capability_id,
            )
        if self.guild_ref is None:
            raise ValueError(
                "direct-message channels cannot be deleted through guild management"
            )
        return await self.client.delete_channel(
            self.guild_ref,
            self.ref,
            target=self.target,
            reason=reason,
        )

    async def upload(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        encryption_mode: str = "plaintext",
        encryption_protocol: str | None = None,
    ) -> Attachment:
        return await self.client.upload_attachment(
            self.ref,
            data,
            target=self.target,
            filename=filename,
            content_type=content_type,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
            encryption_mode=encryption_mode,
            encryption_protocol=encryption_protocol,
        )

    async def create_webhook(self, name: str) -> Webhook:
        if self.guild_ref is None:
            raise ValueError("webhooks require a guild channel")
        return await self.client.create_webhook(
            self.guild_ref, self.ref, name, target=self.target
        )

    async def overwrites(self) -> list[ChannelOverwrite]:
        if self.guild_ref is None:
            raise ValueError("permission overwrites require a guild channel")
        return await self.client.fetch_channel_overwrites(
            self.guild_ref, self.ref, target=self.target
        )

    async def set_overwrite(
        self,
        target_ref: EntityRef,
        target_type: str,
        *,
        allow: int = 0,
        deny: int = 0,
        reason: str | None = None,
    ) -> ChannelOverwrite:
        if self.guild_ref is None:
            raise ValueError("permission overwrites require a guild channel")
        return await self.client.set_channel_overwrite(
            self.guild_ref,
            self.ref,
            target_ref,
            target_type,
            target=self.target,
            allow=allow,
            deny=deny,
            reason=reason,
        )

    async def delete_overwrite(
        self,
        target_ref: EntityRef,
        target_type: str,
        *,
        reason: str | None = None,
    ) -> None:
        if self.guild_ref is None:
            raise ValueError("permission overwrites require a guild channel")
        await self.client.delete_channel_overwrite(
            self.guild_ref,
            self.ref,
            target_ref,
            target_type,
            target=self.target,
            reason=reason,
        )

    async def sync_permissions(self, *, reason: str | None = None) -> Channel:
        if self.guild_ref is None:
            raise ValueError("permission synchronization requires a guild channel")
        return await self.client.sync_channel_permissions(
            self.guild_ref, self.ref, target=self.target, reason=reason
        )

    async def start_thread(
        self,
        name: str,
        *,
        reason: str | None = None,
        type: int | None = None,
        content: str | None = None,
        e2ee: dict[str, Any] | None = None,
        attachment_ids: Sequence[int] = (),
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        reply_to: EntityRef | None = None,
        mention_user_ids: Sequence[EntityRef] = (),
        forward: EntityRef | None = None,
        tts: bool = False,
        voice_message: bool = False,
        applied_tag_ids: Sequence[int] = (),
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        invitable: bool | None = None,
        client_nonce: str | None = None,
        starter_reservation_nonce: str | None = None,
    ) -> Channel:
        """Create a thread or an atomic forum post beneath this channel."""

        return await self.client.start_thread(
            self.ref,
            name,
            target=self.target,
            reason=reason,
            type=type,
            content=content,
            e2ee=e2ee,
            attachment_ids=attachment_ids,
            embeds=embeds,
            view=view,
            poll=poll,
            reply_to=reply_to,
            mention_user_ids=mention_user_ids,
            forward=forward,
            tts=tts,
            voice_message=voice_message,
            applied_tag_ids=applied_tag_ids,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            invitable=invitable,
            client_nonce=client_nonce,
            starter_reservation_nonce=starter_reservation_nonce,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def create_post(
        self,
        name: str,
        content: str | None = None,
        *,
        reason: str | None = None,
        e2ee: dict[str, Any] | None = None,
        attachment_ids: Sequence[int] = (),
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        reply_to: EntityRef | None = None,
        mention_user_ids: Sequence[EntityRef] = (),
        forward: EntityRef | None = None,
        tts: bool = False,
        voice_message: bool = False,
        applied_tag_ids: Sequence[int] = (),
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
        client_nonce: str | None = None,
        starter_reservation_nonce: str | None = None,
    ) -> Channel:
        if not self.is_forum:
            raise ValueError("forum posts require a forum channel")
        if content is not None and len(content) > 2000:
            raise ValueError("forum post content cannot exceed 2000 characters")
        if (
            not content
            and not attachment_ids
            and e2ee is None
            and not embeds
            and view is None
            and poll is None
            and forward is None
            and not voice_message
            and starter_reservation_nonce is None
        ):
            raise ValueError(
                "a forum post requires content or an attachment, embed, "
                "component, poll, or forwarded message"
            )
        return await self.start_thread(
            name,
            reason=reason,
            type=11,
            content=content,
            e2ee=e2ee,
            attachment_ids=attachment_ids,
            embeds=embeds,
            view=view,
            poll=poll,
            reply_to=reply_to,
            mention_user_ids=mention_user_ids,
            forward=forward,
            tts=tts,
            voice_message=voice_message,
            applied_tag_ids=applied_tag_ids,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            client_nonce=client_nonce,
            starter_reservation_nonce=starter_reservation_nonce,
        )

    async def threads(
        self,
        *,
        archived: bool = False,
        include_archived: bool = False,
        before: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
        tag_id: int | None = None,
        tag_ids: list[int] | None = None,
        query: str | None = None,
        sort_order: int | None = None,
    ) -> ThreadPage:
        return await self.client.fetch_threads(
            self.ref,
            target=self.target,
            archived=archived,
            include_archived=include_archived,
            before=before,
            cursor=cursor,
            limit=limit,
            tag_id=tag_id,
            tag_ids=tag_ids,
            query=query,
            sort_order=sort_order,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def join(
        self, *, flags: int = 0, notification_level: str = "inherit"
    ) -> None:
        if not self.is_thread:
            raise ValueError("only threads can be joined")
        await self.client.join_thread(
            self.ref,
            target=self.target,
            flags=flags,
            notification_level=notification_level,
            dm_capability_id=self.dm_capability_id,
        )

    async def leave(self) -> None:
        if not self.is_thread:
            raise ValueError("only threads can be left")
        await self.client.leave_thread(
            self.ref,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )

    async def add_member(self, user: EntityRef) -> None:
        if not self.is_thread:
            raise ValueError("members can only be added to threads")
        await self.client.add_thread_member(
            self.ref,
            user,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )

    async def remove_member(self, user: EntityRef) -> None:
        if not self.is_thread:
            raise ValueError("members can only be removed from threads")
        await self.client.remove_thread_member(
            self.ref,
            user,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )

    async def members(
        self,
        *,
        after: EntityRef | None = None,
        limit: int = 100,
        with_member: bool = False,
    ) -> list[ThreadMember]:
        if not self.is_thread:
            raise ValueError("only threads have thread members")
        return await self.client.thread_members(
            self.ref,
            target=self.target,
            after=after,
            limit=limit,
            with_member=with_member,
            dm_capability_id=self.dm_capability_id,
        )

    async def fetch_member(
        self, user: EntityRef, *, with_member: bool = False
    ) -> ThreadMember:
        if not self.is_thread:
            raise ValueError("only threads have thread members")
        return await self.client.fetch_thread_member(
            self.ref,
            user,
            target=self.target,
            with_member=with_member,
            dm_capability_id=self.dm_capability_id,
        )

    async def edit_thread(
        self,
        *,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        archived: bool | MissingType = MISSING,
        locked: bool | MissingType = MISSING,
        invitable: bool | MissingType = MISSING,
        auto_archive_duration: int | MissingType = MISSING,
        rate_limit_per_user: int | MissingType = MISSING,
        applied_tag_ids: list[int] | MissingType = MISSING,
        pinned: bool | MissingType = MISSING,
    ) -> Channel:
        if not self.is_thread:
            raise ValueError("thread settings require a thread channel")
        return await self.client.edit_thread(
            self.ref,
            target=self.target,
            reason=reason,
            name=name,
            archived=archived,
            locked=locked,
            invitable=invitable,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            applied_tag_ids=applied_tag_ids,
            pinned=pinned,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )


@dataclass(slots=True)
class ThreadPage:
    threads: list[Channel]
    members: list[ThreadMember]
    has_more: bool = False
    next_cursor: str | None = None


@dataclass(slots=True)
class TrackerLane:
    client: Client
    target: str
    ref: EntityRef
    channel_ref: EntityRef
    name: str
    color: int
    kind: str
    completed: bool
    position: int
    task_count: int = 0
    version: str | None = None
    board_version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> TrackerLane:
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            channel_ref=EntityRef.from_wire(
                payload["channel_id"], payload["channel_domain"]
            ),
            name=str(payload["name"]),
            color=int(payload.get("color", 0)),
            kind=str(payload.get("kind", "custom")),
            completed=bool(payload.get("completed", False)),
            position=int(payload.get("position", 0)),
            task_count=int(payload.get("task_count", 0)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            board_version=(
                str(payload["board_version"])
                if payload.get("board_version") is not None
                else None
            ),
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        color: int | MissingType = MISSING,
        kind: str | MissingType = MISSING,
        completed: bool | MissingType = MISSING,
    ) -> TrackerLane:
        return await self.client.edit_tracker_lane(
            self.channel_ref,
            self.ref,
            target=self.target,
            version=self.version,
            name=name,
            color=color,
            kind=kind,
            completed=completed,
        )

    async def move(self, position: int) -> TrackerLane:
        return await self.client.move_tracker_lane(
            self.channel_ref,
            self.ref,
            position,
            target=self.target,
            version=self.version,
        )

    async def delete(self) -> None:
        await self.client.delete_tracker_lane(
            self.channel_ref,
            self.ref,
            target=self.target,
            version=self.version,
        )


@dataclass(slots=True)
class TrackerTask:
    client: Client
    target: str
    ref: EntityRef
    channel_ref: EntityRef
    lane_ref: EntityRef
    number: int
    key: str
    title: str
    description: str | None
    priority: str
    position: int
    creator: User
    assignee: User | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    version: str | None = None
    board_version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> TrackerTask:
        creator = payload.get("creator")
        if not isinstance(creator, dict):
            raise ValueError("tracker task payload is missing its creator")
        assignee = payload.get("assignee")
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            channel_ref=EntityRef.from_wire(
                payload["channel_id"], payload["channel_domain"]
            ),
            lane_ref=EntityRef.from_wire(
                payload["lane_id"],
                payload.get("lane_domain", payload["channel_domain"]),
            ),
            number=int(payload.get("number", payload.get("task_number", 0))),
            key=str(payload.get("key", payload.get("display_id", ""))),
            title=str(payload["title"]),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            priority=str(payload.get("priority", "none")),
            position=int(payload.get("position", 0)),
            creator=User.from_payload(creator),
            assignee=(
                User.from_payload(assignee) if isinstance(assignee, dict) else None
            ),
            due_at=_datetime(payload.get("due_at")),
            completed_at=_datetime(payload.get("completed_at")),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            board_version=(
                str(payload["board_version"])
                if payload.get("board_version") is not None
                else None
            ),
        )

    async def edit(
        self,
        *,
        title: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        priority: str | MissingType = MISSING,
        due_at: datetime | None | MissingType = MISSING,
        assignee: EntityRef | None | MissingType = MISSING,
    ) -> TrackerTask:
        return await self.client.edit_tracker_task(
            self.channel_ref,
            self.ref,
            target=self.target,
            version=self.version,
            title=title,
            description=description,
            priority=priority,
            due_at=due_at,
            assignee=assignee,
        )

    async def move(self, lane: EntityRef, position: int) -> TrackerTask:
        return await self.client.move_tracker_task(
            self.channel_ref,
            self.ref,
            lane,
            position,
            target=self.target,
            version=self.version,
        )

    async def delete(self) -> None:
        await self.client.delete_tracker_task(
            self.channel_ref,
            self.ref,
            target=self.target,
            version=self.version,
        )


@dataclass(slots=True)
class TrackerBoard:
    client: Client
    target: str
    channel_ref: EntityRef
    key_prefix: str
    next_task_number: int
    permissions: int
    lanes: list[TrackerLane]
    tasks: list[TrackerTask]
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> TrackerBoard:
        channel_ref = EntityRef.from_wire(
            payload["channel_id"], payload["channel_domain"]
        )
        return cls(
            client=client,
            target=target,
            channel_ref=channel_ref,
            key_prefix=str(payload["key_prefix"]),
            next_task_number=int(payload.get("next_task_number", 1)),
            permissions=int(payload.get("permissions", 0)),
            lanes=[
                TrackerLane.from_payload(client, target, item)
                for item in payload.get("lanes", [])
                if isinstance(item, dict)
            ],
            tasks=[
                TrackerTask.from_payload(client, target, item)
                for item in payload.get("tasks", [])
                if isinstance(item, dict)
            ],
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    async def edit(self, *, key_prefix: str) -> TrackerBoard:
        return await self.client.edit_tracker(
            self.channel_ref,
            key_prefix=key_prefix,
            target=self.target,
            version=self.version,
        )

    async def create_lane(
        self,
        name: str,
        *,
        color: int = 0x5865F2,
        kind: str = "custom",
        completed: bool = False,
        position: int | None = None,
    ) -> TrackerLane:
        return await self.client.create_tracker_lane(
            self.channel_ref,
            name,
            target=self.target,
            color=color,
            kind=kind,
            completed=completed,
            position=position,
        )

    async def create_task(
        self,
        lane: EntityRef,
        title: str,
        *,
        description: str | None = None,
        priority: str = "none",
        position: int | None = None,
        due_at: datetime | None = None,
        assignee: EntityRef | None = None,
        client_nonce: str | None = None,
    ) -> TrackerTask:
        return await self.client.create_tracker_task(
            self.channel_ref,
            lane,
            title,
            target=self.target,
            description=description,
            priority=priority,
            position=position,
            due_at=due_at,
            assignee=assignee,
            client_nonce=client_nonce,
        )


@dataclass(slots=True)
class Role:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    color: int
    permissions: int
    position: int
    hoist: bool = False
    mentionable: bool = False
    icon_hash: str | None = None
    version: str | None = None

    @classmethod
    def from_payload(cls, client: Client, target: str, payload: dict[str, Any]) -> Role:
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            name=str(payload["name"]),
            icon_hash=(
                str(payload["icon_hash"])
                if payload.get("icon_hash") is not None
                else None
            ),
            color=int(payload.get("color", 0)),
            permissions=int(payload.get("permissions", 0)),
            position=int(payload.get("position", 0)),
            hoist=bool(payload.get("hoist", False)),
            mentionable=bool(payload.get("mentionable", False)),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    @property
    def mention(self) -> str:
        return f"<@&{self.ref}>"

    async def edit(
        self,
        *,
        reason: str | None = None,
        name: str | MissingType = MISSING,
        permissions: int | MissingType = MISSING,
        color: int | MissingType = MISSING,
        hoist: bool | MissingType = MISSING,
        mentionable: bool | MissingType = MISSING,
    ) -> Role:
        return await self.client.edit_role(
            self.guild_ref,
            self.ref,
            target=self.target,
            version=self.version,
            reason=reason,
            name=name,
            permissions=permissions,
            color=color,
            hoist=hoist,
            mentionable=mentionable,
        )

    async def upload_icon(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Attachment:
        return await self.client.upload_role_icon(
            self.guild_ref,
            self.ref,
            data,
            target=self.target,
            filename=filename,
            content_type=content_type,
        )

    async def commit_icon(self, attachment: EntityRef) -> Role | Attachment:
        return await self.client.commit_role_icon(
            self.guild_ref,
            self.ref,
            attachment,
            target=self.target,
        )

    async def delete_icon(self) -> Role:
        return await self.client.delete_role_icon(
            self.guild_ref,
            self.ref,
            target=self.target,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_role(
            self.guild_ref, self.ref, target=self.target, reason=reason
        )


@dataclass(slots=True)
class Member:
    client: Client
    target: str
    guild_ref: EntityRef
    user: User
    nickname: str | None
    joined_at: datetime
    temporary: bool = False
    timeout_until: datetime | None = None
    timeout_indefinite: bool = False
    role_ids: tuple[int, ...] = ()
    presence: str | None = None
    voice_flags: int = 0
    member_version: int = 0
    permissions: int | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Member:
        return cls(
            client=client,
            target=target,
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            user=User.from_payload(payload["user"]),
            nickname=(
                str(payload["nickname"])
                if payload.get("nickname") is not None
                else None
            ),
            joined_at=datetime.fromisoformat(str(payload["joined_at"])),
            temporary=_strict_payload_bool(payload, "temporary", default=False),
            timeout_until=_datetime(payload.get("timeout_until")),
            timeout_indefinite=bool(payload.get("timeout_indefinite", False)),
            role_ids=tuple(int(item) for item in payload.get("role_ids", [])),
            presence=(
                str(payload["presence"])
                if payload.get("presence") is not None
                else None
            ),
            voice_flags=int(payload.get("voice_flags", 0)),
            member_version=int(payload.get("member_version", 0)),
            permissions=_optional_permission_bits(payload, "permissions"),
        )

    @property
    def name(self) -> str:
        return self.nickname or self.user.name

    async def edit(
        self,
        *,
        nickname: str | None,
        reason: str | None = None,
    ) -> Member:
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            nickname=nickname,
            reason=reason,
        )

    async def timeout(
        self,
        *,
        until: datetime | None = None,
        indefinite: bool = False,
        reason: str | None = None,
    ) -> Member:
        if until is None and not indefinite:
            raise ValueError("a timeout needs an expiry or indefinite=True")
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            timeout_until=until,
            timeout_indefinite=indefinite,
            reason=reason,
        )

    async def remove_timeout(self, *, reason: str | None = None) -> Member:
        return await self.client.edit_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            timeout_until=None,
            timeout_indefinite=False,
            reason=reason,
        )

    async def kick(self, *, reason: str | None = None) -> None:
        await self.client.kick_member(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )

    async def ban(
        self,
        *,
        reason: str | None = None,
        delete_message_seconds: int = 0,
        expires_at: datetime | None = None,
    ) -> None:
        await self.client.ban_member(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            reason=reason,
            delete_message_seconds=delete_message_seconds,
            expires_at=expires_at,
        )

    async def add_role(self, role: EntityRef, *, reason: str | None = None) -> None:
        await self.client.add_member_role(
            self.guild_ref,
            self.user.ref,
            role,
            target=self.target,
            reason=reason,
        )

    async def remove_role(self, role: EntityRef, *, reason: str | None = None) -> None:
        await self.client.remove_member_role(
            self.guild_ref,
            self.user.ref,
            role,
            target=self.target,
            reason=reason,
        )

    async def set_roles(
        self, roles: list[EntityRef], *, reason: str | None = None
    ) -> Member:
        return await self.client.set_member_roles(
            self.guild_ref,
            self.user.ref,
            roles,
            target=self.target,
            reason=reason,
        )

    async def set_voice_moderation(
        self,
        *,
        server_mute: bool | None = None,
        server_deaf: bool | None = None,
        reason: str | None = None,
    ) -> None:
        await self.client.set_voice_moderation(
            self.guild_ref,
            self.user.ref,
            target=self.target,
            server_mute=server_mute,
            server_deaf=server_deaf,
            reason=reason,
        )

    async def disconnect_voice(self, *, reason: str | None = None) -> None:
        await self.client.disconnect_voice(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )

    async def move_voice(
        self, channel: EntityRef, *, reason: str | None = None
    ) -> None:
        await self.client.move_voice(
            self.guild_ref,
            self.user.ref,
            channel,
            target=self.target,
            reason=reason,
        )


@dataclass(slots=True)
class Ban:
    client: Client
    target: str
    guild_ref: EntityRef
    user: User
    reason: str | None
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_payload(cls, client: Client, target: str, payload: dict[str, Any]) -> Ban:
        return cls(
            client=client,
            target=target,
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            user=User.from_payload(payload["user"]),
            reason=str(payload["reason"])
            if payload.get("reason") is not None
            else None,
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            expires_at=_datetime(payload.get("expires_at")),
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.unban_member(
            self.guild_ref, self.user.ref, target=self.target, reason=reason
        )


@dataclass(slots=True)
class InstanceBan:
    client: Client
    target: str
    guild_ref: EntityRef
    instance_domain: str
    reason: str | None
    actor_ref: EntityRef
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> InstanceBan:
        return cls(
            client=client,
            target=target,
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            instance_domain=str(payload["instance_domain"]),
            reason=(
                str(payload["reason"]) if payload.get("reason") is not None else None
            ),
            actor_ref=EntityRef.from_wire(payload["actor_id"], payload["actor_domain"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            expires_at=_datetime(payload.get("expires_at")),
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.unban_instance(
            self.guild_ref,
            self.instance_domain,
            target=self.target,
            reason=reason,
        )


ScheduledEventEntityType = Literal[1, 2, 3]
ScheduledEventStatus = Literal[1, 2, 3, 4]
ScheduledEventRecurrenceFrequency = Literal[0, 1, 2, 3]
_RECURRENCE_YEARLY = 0
_RECURRENCE_MONTHLY = 1
_RECURRENCE_WEEKLY = 2
_RECURRENCE_DAILY = 3
_DAILY_WEEKDAY_SETS = frozenset(
    {
        (0, 1, 2, 3, 4),
        (1, 2, 3, 4, 5),
        (6, 0, 1, 2, 3),
        (4, 5),
        (5, 6),
        (6, 0),
    }
)


def _strict_payload_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _scheduled_event_n_weekday(value: object) -> ScheduledEventNWeekday:
    if not isinstance(value, dict) or set(value) != {"n", "day"}:
        raise ValueError("recurrence n-weekday must contain n and day")
    return ScheduledEventNWeekday(
        n=_strict_payload_int(value["n"], "recurrence n"),
        day=_strict_payload_int(value["day"], "recurrence weekday"),
    )


@dataclass(frozen=True, slots=True)
class ScheduledEventNWeekday:
    n: int
    day: int

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not 1 <= self.n <= 5:
            raise ValueError("recurrence n must be between 1 and 5")
        if isinstance(self.day, bool) or not 0 <= self.day <= 6:
            raise ValueError("recurrence weekday must be between 0 and 6")

    def to_dict(self) -> dict[str, int]:
        return {"n": self.n, "day": self.day}


@dataclass(frozen=True, slots=True)
class ScheduledEventRecurrenceRule:
    start: datetime
    frequency: ScheduledEventRecurrenceFrequency
    interval: int = 1
    by_weekday: tuple[int, ...] | None = None
    by_n_weekday: tuple[ScheduledEventNWeekday, ...] | None = None
    by_month: tuple[int, ...] | None = None
    by_month_day: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("recurrence start must include a timezone")
        if isinstance(self.frequency, bool) or self.frequency not in {0, 1, 2, 3}:
            raise ValueError(
                "recurrence frequency must be daily, weekly, monthly, or yearly"
            )
        if (
            isinstance(self.interval, bool)
            or self.interval not in {1, 2}
            or (self.interval == 2 and self.frequency != _RECURRENCE_WEEKLY)
        ):
            raise ValueError("only weekly recurrence supports an interval of 2")
        selectors = sum(
            (
                self.by_weekday is not None,
                self.by_n_weekday is not None,
                self.by_month is not None or self.by_month_day is not None,
            )
        )
        if selectors > 1:
            raise ValueError("recurrence selectors are mutually exclusive")
        if self.by_weekday is not None:
            weekdays = tuple(self.by_weekday)
            if not weekdays or len(weekdays) > 7 or len(weekdays) != len(set(weekdays)):
                raise ValueError("recurrence weekdays must be unique")
            if any(isinstance(day, bool) or not 0 <= day <= 6 for day in weekdays):
                raise ValueError("recurrence weekdays must be between 0 and 6")
            if (
                self.frequency == _RECURRENCE_DAILY
                and weekdays not in _DAILY_WEEKDAY_SETS
            ):
                raise ValueError("daily recurrence uses a supported weekday set")
            if self.frequency == _RECURRENCE_WEEKLY and len(weekdays) != 1:
                raise ValueError("weekly recurrence accepts exactly one weekday")
            if self.frequency not in {_RECURRENCE_DAILY, _RECURRENCE_WEEKLY}:
                raise ValueError(
                    "weekdays are valid only for daily or weekly recurrence"
                )
        if self.by_n_weekday is not None:
            if len(self.by_n_weekday) != 1 or self.frequency != _RECURRENCE_MONTHLY:
                raise ValueError("monthly recurrence accepts one n-weekday selector")
        if (self.by_month is None) != (self.by_month_day is None):
            raise ValueError("yearly recurrence requires month and month day")
        if self.by_month is not None:
            if (
                self.frequency != _RECURRENCE_YEARLY
                or len(self.by_month) != 1
                or any(
                    isinstance(month, bool) or not 1 <= month <= 12
                    for month in self.by_month
                )
                or self.by_month_day is None
                or len(self.by_month_day) != 1
                or any(
                    isinstance(day, bool) or not 1 <= day <= 31
                    for day in self.by_month_day
                )
            ):
                raise ValueError(
                    "yearly recurrence requires one valid month and month day"
                )

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> ScheduledEventRecurrenceRule | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            start=datetime.fromisoformat(str(payload["start"])),
            frequency=cast(
                ScheduledEventRecurrenceFrequency,
                _strict_payload_int(payload["frequency"], "recurrence frequency"),
            ),
            interval=_strict_payload_int(
                payload.get("interval", 1), "recurrence interval"
            ),
            by_weekday=(
                tuple(
                    _strict_payload_int(item, "recurrence weekday")
                    for item in payload["by_weekday"]
                )
                if isinstance(payload.get("by_weekday"), list)
                else None
            ),
            by_n_weekday=(
                tuple(
                    _scheduled_event_n_weekday(item) for item in payload["by_n_weekday"]
                )
                if isinstance(payload.get("by_n_weekday"), list)
                else None
            ),
            by_month=(
                tuple(
                    _strict_payload_int(item, "recurrence month")
                    for item in payload["by_month"]
                )
                if isinstance(payload.get("by_month"), list)
                else None
            ),
            by_month_day=(
                tuple(
                    _strict_payload_int(item, "recurrence month day")
                    for item in payload["by_month_day"]
                )
                if isinstance(payload.get("by_month_day"), list)
                else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": None,
            "frequency": self.frequency,
            "interval": self.interval,
            "by_weekday": list(self.by_weekday)
            if self.by_weekday is not None
            else None,
            "by_n_weekday": (
                [item.to_dict() for item in self.by_n_weekday]
                if self.by_n_weekday is not None
                else None
            ),
            "by_month": list(self.by_month) if self.by_month is not None else None,
            "by_month_day": (
                list(self.by_month_day) if self.by_month_day is not None else None
            ),
            "by_year_day": None,
            "count": None,
        }


@dataclass(frozen=True, slots=True)
class ScheduledEventEntityMetadata:
    location: str

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> ScheduledEventEntityMetadata | None:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("location"), str
        ):
            return None
        return cls(location=str(payload["location"]))

    def to_dict(self) -> dict[str, str]:
        return {"location": self.location}


@dataclass(slots=True)
class ScheduledEvent:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    creator_ref: EntityRef
    name: str
    scheduled_start_time: datetime
    privacy_level: int
    status: ScheduledEventStatus
    entity_type: ScheduledEventEntityType
    channel_ref: EntityRef | None = None
    creator: User | None = None
    description: str | None = None
    scheduled_end_time: datetime | None = None
    entity_ref: EntityRef | None = None
    entity_metadata: ScheduledEventEntityMetadata | None = None
    recurrence_rule: ScheduledEventRecurrenceRule | None = None
    image_hash: str | None = None
    user_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> ScheduledEvent:
        raw_creator = payload.get("creator")
        status = _strict_payload_int(payload["status"], "scheduled event status")
        entity_type = _strict_payload_int(
            payload["entity_type"], "scheduled event entity type"
        )
        privacy_level = _strict_payload_int(
            payload.get("privacy_level", 2), "scheduled event privacy level"
        )
        if status not in {1, 2, 3, 4}:
            raise ValueError("scheduled event status is invalid")
        if entity_type not in {1, 2, 3}:
            raise ValueError("scheduled event entity type is invalid")
        if privacy_level != 2:
            raise ValueError("scheduled event privacy level is invalid")
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            creator_ref=EntityRef.from_wire(
                payload["creator_id"], payload["creator_domain"]
            ),
            name=str(payload["name"]),
            scheduled_start_time=datetime.fromisoformat(
                str(payload["scheduled_start_time"])
            ),
            privacy_level=privacy_level,
            status=status,  # type: ignore[arg-type]
            entity_type=entity_type,  # type: ignore[arg-type]
            channel_ref=_optional_ref(payload, "channel_id", "channel_domain"),
            creator=(
                User.from_payload(raw_creator)
                if isinstance(raw_creator, dict)
                else None
            ),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            scheduled_end_time=_datetime(payload.get("scheduled_end_time")),
            entity_ref=_optional_ref(payload, "entity_id", "entity_domain"),
            entity_metadata=ScheduledEventEntityMetadata.from_payload(
                payload.get("entity_metadata")
                if isinstance(payload.get("entity_metadata"), dict)
                else None
            ),
            recurrence_rule=ScheduledEventRecurrenceRule.from_payload(
                payload.get("recurrence_rule")
                if isinstance(payload.get("recurrence_rule"), dict)
                else None
            ),
            image_hash=(
                str(payload["image"]) if payload.get("image") is not None else None
            ),
            user_count=(
                int(payload["user_count"])
                if payload.get("user_count") is not None
                else None
            ),
            created_at=_datetime(payload.get("created_at")),
            updated_at=_datetime(payload.get("updated_at")),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        channel: EntityRef | None | MissingType = MISSING,
        location: str | None | MissingType = MISSING,
        scheduled_start_time: datetime | MissingType = MISSING,
        scheduled_end_time: datetime | None | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        entity_type: ScheduledEventEntityType | MissingType = MISSING,
        status: ScheduledEventStatus | MissingType = MISSING,
        recurrence_rule: ScheduledEventRecurrenceRule | None | MissingType = MISSING,
        reason: str | None = None,
    ) -> ScheduledEvent:
        return await self.client.edit_scheduled_event(
            self.guild_ref,
            self.ref,
            target=self.target,
            name=name,
            channel=channel,
            location=location,
            scheduled_start_time=scheduled_start_time,
            scheduled_end_time=scheduled_end_time,
            description=description,
            entity_type=entity_type,
            status=status,
            recurrence_rule=recurrence_rule,
            reason=reason,
        )

    async def upload_image(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        reason: str | None = None,
        scan_attempts: int = 45,
    ) -> ScheduledEvent:
        return await self.client.upload_scheduled_event_image(
            self.guild_ref,
            self.ref,
            data,
            filename=filename,
            content_type=content_type,
            target=self.target,
            reason=reason,
            scan_attempts=scan_attempts,
        )

    async def delete_image(self, *, reason: str | None = None) -> ScheduledEvent:
        return await self.client.delete_scheduled_event_image(
            self.guild_ref,
            self.ref,
            target=self.target,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_scheduled_event(
            self.guild_ref,
            self.ref,
            target=self.target,
            reason=reason,
        )

    async def users(
        self,
        *,
        limit: int = 100,
        before: EntityRef | None = None,
        after: EntityRef | None = None,
        with_member: bool = False,
    ) -> list[ScheduledEventUser]:
        return await self.client.scheduled_event_users(
            self.guild_ref,
            self.ref,
            target=self.target,
            limit=limit,
            before=before,
            after=after,
            with_member=with_member,
        )


@dataclass(frozen=True, slots=True)
class ScheduledEventUser:
    event_ref: EntityRef
    user: User
    member: Member | None
    subscribed_at: datetime

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> ScheduledEventUser:
        raw_user = payload.get("user")
        if not isinstance(raw_user, dict):
            raise ValueError("scheduled event user payload is missing its user")
        raw_member = payload.get("member")
        return cls(
            event_ref=EntityRef.from_wire(
                payload["guild_scheduled_event_id"],
                payload["guild_scheduled_event_domain"],
            ),
            user=User.from_payload(raw_user),
            member=(
                Member.from_payload(client, target, raw_member)
                if isinstance(raw_member, dict)
                else None
            ),
            subscribed_at=datetime.fromisoformat(str(payload["subscribed_at"])),
        )


@dataclass(slots=True)
class Invite:
    client: Client
    target: str
    code: str
    guild_ref: EntityRef
    channel_id: int | None
    uses: int
    max_uses: int | None
    expires_at: datetime | None
    created_at: datetime
    temporary: bool = False
    reusable: bool = False
    target_type: str | None = None
    target_user_ref: EntityRef | None = None
    scheduled_event_ref: EntityRef | None = None
    role_refs: tuple[EntityRef, ...] = ()
    target_user_count: int = 0
    revoked_at: datetime | None = None
    guild_scheduled_event: ScheduledEvent | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Invite:
        guild = payload.get("guild")
        if not isinstance(guild, dict):
            raise ValueError("invite payload is missing its guild")
        guild_ref = EntityRef.from_wire(guild["id"], guild["origin_domain"])
        raw_role_refs = payload.get("role_ids", [])
        target_user_count = payload.get("target_user_count", 0)
        raw_uses = payload.get("uses", 0)
        raw_max_uses = payload.get("max_uses")
        raw_temporary = payload.get("temporary", False)
        raw_reusable = payload.get("reusable", False)
        raw_target_type = payload.get("target_type")
        raw_code = payload.get("code")
        raw_channel_id = payload.get("channel_id")
        raw_scheduled_event = payload.get("guild_scheduled_event")
        if (
            not isinstance(raw_code, str)
            or len(raw_code) != 8
            or not raw_code.isascii()
            or not raw_code.isalnum()
            or raw_channel_id is not None
            and (isinstance(raw_channel_id, bool) or not str(raw_channel_id).isdigit())
            or raw_scheduled_event is not None
            and not isinstance(raw_scheduled_event, dict)
            or not isinstance(raw_role_refs, list)
            or len(raw_role_refs) > 100
            or any(not isinstance(item, str) for item in raw_role_refs)
            or isinstance(target_user_count, bool)
            or not isinstance(target_user_count, int)
            or not 0 <= target_user_count <= 1_000
            or isinstance(raw_uses, bool)
            or not isinstance(raw_uses, int)
            or raw_uses < 0
            or raw_max_uses is not None
            and (
                isinstance(raw_max_uses, bool)
                or not isinstance(raw_max_uses, int)
                or not 1 <= raw_max_uses <= 100
            )
            or raw_max_uses is not None
            and raw_uses > raw_max_uses
            or type(raw_temporary) is not bool
            or type(raw_reusable) is not bool
            or raw_target_type not in {None, "stream"}
            or payload.get("target_application_id") is not None
        ):
            raise ValueError("invite targeting projection is invalid")
        try:
            role_refs = tuple(EntityRef.parse(item) for item in raw_role_refs)
            target_user_ref = (
                EntityRef.parse(payload["target_user_id"])
                if isinstance(payload.get("target_user_id"), str)
                else None
            )
            scheduled_event_ref = (
                EntityRef.parse(payload["scheduled_event_id"])
                if payload.get("scheduled_event_id") is not None
                else None
            )
            guild_scheduled_event = (
                ScheduledEvent.from_payload(client, target, raw_scheduled_event)
                if isinstance(raw_scheduled_event, dict)
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invite targeting projection is invalid") from exc
        if (
            len(role_refs) != len(set(role_refs))
            or any(role_ref.domain != guild_ref.domain for role_ref in role_refs)
            or (raw_target_type == "stream") != (target_user_ref is not None)
            or scheduled_event_ref is not None
            and scheduled_event_ref.domain != guild_ref.domain
            or guild_scheduled_event is not None
            and (
                scheduled_event_ref is None
                or guild_scheduled_event.ref != scheduled_event_ref
                or guild_scheduled_event.guild_ref != guild_ref
            )
        ):
            raise ValueError("invite targeting projection is invalid")
        return cls(
            client=client,
            target=target,
            code=raw_code,
            guild_ref=guild_ref,
            channel_id=(int(raw_channel_id) if raw_channel_id is not None else None),
            uses=raw_uses,
            max_uses=raw_max_uses,
            expires_at=_datetime(payload.get("expires_at")),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            temporary=raw_temporary,
            reusable=raw_reusable,
            target_type=raw_target_type,
            target_user_ref=target_user_ref,
            scheduled_event_ref=scheduled_event_ref,
            role_refs=role_refs,
            target_user_count=target_user_count,
            revoked_at=_datetime(payload.get("revoked_at")),
            guild_scheduled_event=guild_scheduled_event,
        )

    @property
    def unique(self) -> bool:
        return not self.reusable

    async def revoke(self, *, reason: str | None = None) -> Invite:
        return await self.client.revoke_invite(
            self.guild_ref,
            self.code,
            target=self.target,
            reason=reason,
        )

    async def target_users(self) -> InviteTargetUsers:
        return await self.client.fetch_invite_target_users(
            self.guild_ref,
            self.code,
            target=self.target,
        )

    async def update_target_users(
        self,
        users: Sequence[EntityRef],
    ) -> InviteTargetUsersJobStatus:
        return await self.client.update_invite_target_users(
            self.guild_ref,
            self.code,
            users,
            target=self.target,
        )

    async def target_users_job_status(self) -> InviteTargetUsersJobStatus:
        return await self.client.fetch_invite_target_users_job_status(
            self.guild_ref,
            self.code,
            target=self.target,
        )


@dataclass(frozen=True, slots=True)
class InviteTargetUsers:
    users: tuple[EntityRef, ...]

    @classmethod
    def from_payload(cls, payload: object) -> InviteTargetUsers:
        if not isinstance(payload, dict) or set(payload) != {"target_user_ids"}:
            raise ValueError("invite target-user response is invalid")
        raw_users = payload["target_user_ids"]
        if (
            not isinstance(raw_users, list)
            or len(raw_users) > 1_000
            or any(not isinstance(item, str) for item in raw_users)
        ):
            raise ValueError("invite target-user response is invalid")
        try:
            users = tuple(EntityRef.parse(item) for item in raw_users)
        except ValueError as exc:
            raise ValueError("invite target-user response is invalid") from exc
        if len(users) != len(set(users)):
            raise ValueError("invite target-user response is invalid")
        return cls(users=users)


@dataclass(frozen=True, slots=True)
class InviteTargetUsersJobStatus:
    status: Literal[0, 1, 2, 3]
    total_users: int
    processed_users: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None

    @classmethod
    def from_payload(cls, payload: object) -> InviteTargetUsersJobStatus:
        expected = {
            "status",
            "total_users",
            "processed_users",
            "created_at",
            "completed_at",
            "error_message",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invite target-user job response is invalid")
        status = payload["status"]
        total = payload["total_users"]
        processed = payload["processed_users"]
        error = payload["error_message"]
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
            completed_at = (
                datetime.fromisoformat(payload["completed_at"])
                if payload["completed_at"] is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invite target-user job response is invalid") from exc
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or status not in {0, 1, 2, 3}
            or isinstance(total, bool)
            or not isinstance(total, int)
            or not 0 <= total <= 1_000
            or isinstance(processed, bool)
            or not isinstance(processed, int)
            or not 0 <= processed <= total
            or created_at.tzinfo is None
            or completed_at is not None
            and completed_at.tzinfo is None
            or error is not None
            and (not isinstance(error, str) or len(error) > 1_000)
        ):
            raise ValueError("invite target-user job response is invalid")
        return cls(
            status=cast(Literal[0, 1, 2, 3], status),
            total_users=total,
            processed_users=processed,
            created_at=created_at,
            completed_at=completed_at,
            error_message=error,
        )


@dataclass(frozen=True, slots=True)
class WebhookSourceGuild:
    ref: EntityRef
    name: str
    icon_hash: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WebhookSourceGuild:
        return cls(
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            name=str(payload["name"]),
            icon_hash=(
                str(payload["icon_hash"])
                if payload.get("icon_hash") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WebhookSourceChannel:
    ref: EntityRef
    name: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WebhookSourceChannel:
        return cls(
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            name=str(payload["name"]),
        )


@dataclass(slots=True)
class Webhook:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    channel_ref: EntityRef
    name: str
    type: int = 1
    application_ref: EntityRef | None = None
    avatar_hash: str | None = None
    revoked: bool = False
    token: str | None = None
    execution_url: str | None = None
    e2ee_device_id: str | None = None
    e2ee_author_ref: EntityRef | None = None
    creator: User | None = None
    source_guild: WebhookSourceGuild | None = None
    source_channel: WebhookSourceChannel | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Webhook:
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["guild_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            channel_ref=EntityRef.from_wire(
                payload["channel_id"], payload["channel_domain"]
            ),
            name=str(payload["name"]),
            type=int(payload.get("type", 1)),
            application_ref=_optional_ref(
                payload,
                "application_id",
                "application_domain",
            ),
            avatar_hash=(
                str(payload["avatar_hash"])
                if payload.get("avatar_hash") is not None
                else None
            ),
            revoked=_strict_payload_bool(payload, "revoked", default=False),
            token=(str(payload["token"]) if payload.get("token") is not None else None),
            execution_url=(
                str(payload["execution_url"])
                if payload.get("execution_url") is not None
                else None
            ),
            creator=(
                User.from_payload(payload["user"])
                if isinstance(payload.get("user"), dict)
                else None
            ),
            source_guild=(
                WebhookSourceGuild.from_payload(payload["source_guild"])
                if isinstance(payload.get("source_guild"), dict)
                else None
            ),
            source_channel=(
                WebhookSourceChannel.from_payload(payload["source_channel"])
                if isinstance(payload.get("source_channel"), dict)
                else None
            ),
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        avatar_hash: str | None | MissingType = MISSING,
        channel: EntityRef | MissingType = MISSING,
        reason: str | None = None,
    ) -> Webhook:
        return await self.client.edit_webhook(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            name=name,
            avatar_hash=avatar_hash,
            channel=channel,
            reason=reason,
        )

    async def set_avatar(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        reason: str | None = None,
        scan_attempts: int = 45,
    ) -> Webhook:
        return await self.client.upload_webhook_avatar(
            self.guild_ref,
            self.ref.id,
            data,
            filename=filename,
            content_type=content_type,
            target=self.target,
            reason=reason,
            scan_attempts=scan_attempts,
        )

    async def delete_avatar(self, *, reason: str | None = None) -> Webhook:
        return await self.client.delete_webhook_avatar(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            reason=reason,
        )

    async def rotate(self, *, reason: str | None = None) -> Webhook:
        rotated = await self.client.rotate_webhook(
            self.guild_ref, self.ref.id, target=self.target, reason=reason
        )
        self.e2ee_device_id = None
        return rotated

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_webhook(
            self.guild_ref, self.ref.id, target=self.target, reason=reason
        )
        self.e2ee_device_id = None

    async def edit_with_token(
        self,
        *,
        name: str | MissingType = MISSING,
        clear_avatar: bool = False,
    ) -> Webhook:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.edit_webhook_with_token(
            self.ref,
            self.token,
            target=self.target,
            name=name,
            clear_avatar=clear_avatar,
        )

    async def set_avatar_with_token(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        scan_attempts: int = 45,
    ) -> Webhook:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.upload_webhook_avatar_with_token(
            self.ref,
            self.token,
            data,
            filename=filename,
            content_type=content_type,
            target=self.target,
            scan_attempts=scan_attempts,
        )

    async def delete_avatar_with_token(self) -> Webhook:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.delete_webhook_avatar_with_token(
            self.ref,
            self.token,
            target=self.target,
        )

    async def delete_with_token(self) -> None:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        await self.client.delete_webhook_with_token(
            self.ref,
            self.token,
            target=self.target,
        )
        self.e2ee_device_id = None

    def set_e2ee_device(self, device: WebhookE2EEDevice | str | None) -> None:
        protocol_id = (
            device.protocol_id if not isinstance(device, (str, type(None))) else device
        )
        if protocol_id is not None and (
            len(protocol_id) != 47
            or not protocol_id.startswith("kwe_")
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in protocol_id[4:]
            )
        ):
            raise ValueError("webhook E2EE device ID is invalid")
        if not isinstance(device, (str, type(None))) and device.webhook_ref != self.ref:
            raise ValueError("webhook E2EE device belongs to another webhook")
        self.e2ee_device_id = protocol_id
        self.e2ee_author_ref = (
            device.author_ref if not isinstance(device, (str, type(None))) else None
        )

    def encrypt_message(
        self,
        context: InteractionE2EEContext,
        data: Mapping[str, object],
        *,
        message: EntityRef | None = None,
        message_revision: int = 1,
        mention_refs: Sequence[EntityRef | str] = (),
        replied_user_ref: EntityRef | None = None,
        referenced_message_ref: EntityRef | None = None,
    ) -> EncryptedRichMessage:
        """Encrypt one webhook-authored rich create/edit with exact public AAD."""

        if self.e2ee_device_id is None or self.e2ee_author_ref is None:
            raise ValueError("select the registered webhook E2EE device first")
        if context.channel_ref.domain != self.ref.domain:
            raise ValueError("webhook E2EE context belongs to another authority")
        from .e2ee import encrypt_message

        return encrypt_message(
            context,
            data,
            author_ref=self.e2ee_author_ref,
            sender_device_id=self.e2ee_device_id,
            message_ref=message,
            message_revision=message_revision,
            mention_refs=mention_refs,
            replied_user_ref=replied_user_ref,
            referenced_message_ref=referenced_message_ref,
        )

    async def create_e2ee_device_challenge(
        self, provider: E2EEProvider
    ) -> WebhookE2EEDeviceChallenge:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.create_webhook_e2ee_device_challenge(
            self.ref, self.token, provider, target=self.target
        )

    async def register_e2ee_device(
        self,
        provider: E2EEProvider,
        *,
        capabilities: Sequence[str] = ("e2ee-mls/1", "e2ee-media/1"),
    ) -> WebhookE2EEDevice:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        device = await self.client.register_webhook_e2ee_device(
            self.ref,
            self.token,
            provider,
            capabilities=capabilities,
            target=self.target,
        )
        self.set_e2ee_device(device)
        return device

    async def e2ee_devices(self) -> WebhookE2EEDeviceInventory:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.webhook_e2ee_devices(
            self.ref, self.token, target=self.target
        )

    async def replenish_e2ee_key_packages(
        self,
        provider: E2EEProvider,
        *,
        minimum_available: int = 20,
        desired_available: int = 50,
        expires_at: datetime | None = None,
    ) -> WebhookE2EEDevice:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        device = await self.client.replenish_webhook_e2ee_key_packages(
            self.ref,
            self.token,
            provider,
            minimum_available=minimum_available,
            desired_available=desired_available,
            expires_at=expires_at,
            target=self.target,
        )
        self.set_e2ee_device(device)
        return device

    async def e2ee_participation(
        self, channel: EntityRef
    ) -> WebhookE2EEParticipationStatus:
        return await self.client.webhook_e2ee_participation(
            self.guild_ref, self.ref, channel, target=self.target
        )

    async def set_e2ee_participation(
        self,
        channel: EntityRef,
        enabled: bool,
        *,
        reason: str | None = None,
    ) -> WebhookE2EEParticipationStatus:
        return await self.client.set_webhook_e2ee_participation(
            self.guild_ref,
            self.ref,
            channel,
            enabled,
            target=self.target,
            reason=reason,
        )

    async def fetch_e2ee_control_log(
        self,
        channel: EntityRef,
        *,
        after: str | None = None,
        limit: int = 25,
    ) -> WebhookE2EEControlPage:
        if self.token is None or self.e2ee_device_id is None:
            raise ValueError("webhook token and exact E2EE device are required")
        return await self.client.fetch_webhook_e2ee_control_log(
            self.ref,
            self.token,
            channel,
            self.e2ee_device_id,
            after=after,
            limit=limit,
            target=self.target,
        )

    async def sync_e2ee_control_log(
        self,
        context: InteractionE2EEContext,
        *,
        after: str | None = None,
    ) -> str | None:
        if self.token is None or self.e2ee_device_id is None:
            raise ValueError("webhook token and exact E2EE device are required")
        return await self.client.sync_webhook_e2ee_control_log(
            self.ref,
            self.token,
            self.e2ee_device_id,
            context,
            after=after,
            target=self.target,
        )

    async def create_encrypted_forum_post(
        self,
        provider: E2EEProvider,
        name: str,
        data: Mapping[str, object],
        *,
        client_nonce: str | None = None,
        applied_tag_ids: Sequence[int] = (),
        attachment_ids: Sequence[int] = (),
        mention_refs: Sequence[EntityRef | str] = (),
        username: str | None = None,
        avatar_url: str | None = None,
    ) -> tuple[Message, InteractionE2EEContext]:
        """Reserve, activate, and claim one required-E2EE forum post."""

        from .e2ee import E2EEProtocolError, InteractionE2EEContext

        if self.token is None or self.e2ee_device_id is None:
            raise ValueError("webhook token and exact E2EE device are required")
        nonce = client_nonce or f"kwf_{secrets.token_urlsafe(24)}"
        inventory = await self.e2ee_devices()
        matches = [
            item
            for item in inventory.devices
            if item.protocol_id == self.e2ee_device_id
        ]
        if len(matches) != 1:
            raise ValueError("selected webhook E2EE device is unavailable")
        device = matches[0]
        self.set_e2ee_device(device)
        thread = await self.client.create_webhook_encrypted_forum_reservation(
            self.ref,
            self.token,
            name=name,
            client_nonce=nonce,
            device_id=device.protocol_id,
            applied_tag_ids=applied_tag_ids,
            target=self.target,
        )
        if thread.parent_ref != self.channel_ref:
            raise E2EEProtocolError("webhook forum reservation changed its parent")
        digest = hashlib.sha256(
            (
                f"kaede-webhook-forum-operation-v1\0{self.ref}\0{thread.ref}\0"
                f"{device.protocol_id}\0{nonce}"
            ).encode()
        ).digest()
        operation_id = "keo_" + encode_base64url(digest)
        proposal = await self.client.propose_webhook_encrypted_forum_room(
            self.ref,
            self.token,
            thread.ref,
            device,
            provider,
            operation_id,
            target=self.target,
        )
        provider.create_group(proposal.group_id)
        commit, welcome = provider.add_members(
            proposal.group_id,
            [item.key_package for item in proposal.key_packages],
        )
        await self.client.activate_webhook_encrypted_forum_room(
            self.ref,
            self.token,
            thread.ref,
            device,
            proposal,
            commit=commit,
            welcome=welcome,
            target=self.target,
        )
        provider.merge_pending_commit(proposal.group_id)
        context = InteractionE2EEContext(
            provider=provider,
            channel_ref=thread.ref,
            group_id=proposal.group_id,
            policy_generation=proposal.policy_generation,
            epoch=1,
        )
        self.client.set_message_e2ee_context(context)
        encrypted = self.encrypt_message(
            context,
            data,
            mention_refs=mention_refs,
        )
        message = await self.client.claim_webhook_encrypted_forum_starter(
            self.ref,
            self.token,
            thread.ref,
            device.protocol_id,
            client_nonce=nonce,
            e2ee=encrypted.envelope,
            attachment_ids=attachment_ids,
            username=username,
            avatar_url=avatar_url,
            target=self.target,
        )
        return message, context

    async def send(
        self,
        content: str | None = None,
        **kwargs: Any,
    ) -> Message | None:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        if kwargs.get("e2ee") is not None and "e2ee_device_id" not in kwargs:
            kwargs["e2ee_device_id"] = self.e2ee_device_id
        return await self.client.execute_webhook(
            self.ref,
            self.token,
            content,
            target=self.target,
            **kwargs,
        )

    async def upload_attachment(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        channel: EntityRef | None = None,
        encrypted: bool = False,
        duration_secs: float | None = None,
        waveform: str | None = None,
    ) -> Attachment:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.upload_webhook_attachment(
            self.ref,
            self.token,
            data,
            filename=filename,
            content_type=content_type,
            target=self.target,
            channel_ref=channel,
            e2ee_device_id=self.e2ee_device_id if encrypted else None,
            encryption_protocol="kaede-file-v1" if encrypted else None,
            duration_secs=duration_secs,
            waveform=waveform,
        )

    async def fetch_message(self, message: EntityRef) -> Message:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        return await self.client.fetch_webhook_message(
            self.ref,
            self.token,
            message,
            target=self.target,
            e2ee_device_id=self.e2ee_device_id,
        )

    async def edit_message(
        self,
        message: EntityRef,
        **changes: Any,
    ) -> Message:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        if changes.get("e2ee") is not None and "e2ee_device_id" not in changes:
            changes["e2ee_device_id"] = self.e2ee_device_id
        return await self.client.edit_webhook_message(
            self.ref, self.token, message, target=self.target, **changes
        )

    async def delete_message(self, message: EntityRef) -> None:
        if self.token is None:
            raise ValueError("webhook token is unavailable")
        await self.client.delete_webhook_message(
            self.ref, self.token, message, target=self.target
        )


@dataclass(slots=True)
class Emoji:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    animated: bool = False
    available: bool = True
    roles: tuple[EntityRef, ...] = ()
    media_hash: str | None = None
    creator_ref: EntityRef | None = None
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Emoji:
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            name=str(payload["name"]),
            animated=_strict_payload_bool(payload, "animated", default=False),
            available=_strict_payload_bool(payload, "available", default=True),
            roles=tuple(EntityRef.parse(item) for item in payload.get("roles", ())),
            media_hash=(
                str(payload["media_hash"])
                if payload.get("media_hash") is not None
                else None
            ),
            creator_ref=_optional_ref(payload, "creator_id", "creator_domain"),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    @property
    def token(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.ref}>"

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        roles: Sequence[EntityRef] | MissingType = MISSING,
        reason: str | None = None,
    ) -> Emoji:
        return await self.client.edit_emoji(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            name=name,
            roles=roles,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_emoji(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            reason=reason,
        )


@dataclass(slots=True)
class Sticker:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    description: str | None = None
    animated: bool = False
    available: bool = True
    tags: tuple[str, ...] = ()
    media_hash: str | None = None
    creator_ref: EntityRef | None = None
    version: str | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Sticker:
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            name=str(payload["name"]),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            animated=_strict_payload_bool(payload, "animated", default=False),
            available=_strict_payload_bool(payload, "available", default=True),
            tags=tuple(str(item) for item in payload.get("tags", ())),
            media_hash=(
                str(payload["media_hash"])
                if payload.get("media_hash") is not None
                else None
            ),
            creator_ref=_optional_ref(payload, "creator_id", "creator_domain"),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
        )

    @property
    def token(self) -> str:
        return f"<sticker:{self.name}:{self.ref}>"

    @property
    def media_url(self) -> str:
        return f"https://{self.ref.domain}/media/stickers/{self.ref.id}/thumbnail_512"

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        description: str | None | MissingType = MISSING,
        tags: Sequence[str] | MissingType = MISSING,
        reason: str | None = None,
    ) -> Sticker:
        return await self.client.edit_sticker(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            name=name,
            description=description,
            tags=tags,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_sticker(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            reason=reason,
        )


@dataclass(slots=True)
class Attachment:
    client: Client
    target: str
    ref: EntityRef
    filename: str
    content_type: str
    size: int
    scan_status: str
    width: int | None = None
    height: int | None = None
    duration_secs: float | None = None
    waveform: str | None = None
    blurhash: str | None = None
    encryption_mode: str = "plaintext"
    encryption_protocol: str | None = None
    purpose: str = "attachment"
    variants: dict[str, Any] = field(default_factory=dict)
    finalized_at: datetime | None = None
    upload_url: str | None = None
    media_origin: str | None = None
    upload_method: Literal["PUT"] | None = None
    expires_at: datetime | None = None
    installation_id: int | None = None
    channel_ref: EntityRef | None = None
    dm_capability_id: str | None = None
    dm_capability_revision: int | None = None
    installation_ref: EntityRef | None = None
    installation_type: Literal["guild", "user"] | None = None
    encrypted_manifest: dict[str, Any] | None = None

    def bind_runtime(
        self,
        *,
        channel_ref: EntityRef,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        dm_capability_revision: int | None = None,
        installation_ref: EntityRef | None = None,
        installation_type: Literal["guild", "user"] | None = None,
        reject_unasserted: bool = False,
    ) -> Attachment:
        """Fill omitted same-message context and reject conflicting provenance."""

        _bind_optional_context(
            self,
            {
                "channel_ref": channel_ref,
                "installation_id": installation_id,
                "dm_capability_id": dm_capability_id,
                "dm_capability_revision": dm_capability_revision,
                "installation_ref": installation_ref,
                "installation_type": installation_type,
            },
            context="attachment response",
            reject_unasserted=reject_unasserted,
        )
        return self

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Attachment:
        installation_type = payload.get(
            "bot_installation_type", payload.get("installation_type")
        )
        if installation_type is not None and installation_type not in {"guild", "user"}:
            raise ValueError("attachment response has an invalid installation type")
        upload_method = payload.get("upload_method")
        if upload_method is not None and upload_method != "PUT":
            raise ValueError("attachment response has an invalid upload method")
        installation_value = payload.get(
            "bot_installation_id",
            payload.get("installation_id"),
        )
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            filename=str(payload["filename"]),
            content_type=str(payload["content_type"]),
            size=int(payload["size"]),
            scan_status=str(payload.get("scan_status", "pending")),
            width=int(payload["width"]) if payload.get("width") is not None else None,
            height=(
                int(payload["height"]) if payload.get("height") is not None else None
            ),
            duration_secs=(
                float(payload["duration_secs"])
                if payload.get("duration_secs") is not None
                else None
            ),
            waveform=(
                str(payload["waveform"])
                if payload.get("waveform") is not None
                else None
            ),
            blurhash=(
                str(payload["blurhash"])
                if payload.get("blurhash") is not None
                else None
            ),
            encryption_mode=str(payload.get("encryption_mode", "plaintext")),
            encryption_protocol=(
                str(payload["encryption_protocol"])
                if payload.get("encryption_protocol") is not None
                else None
            ),
            purpose=str(payload.get("purpose", "attachment")),
            variants=dict(payload.get("variants") or {}),
            finalized_at=_datetime(payload.get("finalized_at")),
            upload_url=(
                str(payload["upload_url"])
                if payload.get("upload_url") is not None
                else None
            ),
            media_origin=(
                str(payload["media_origin"])
                if payload.get("media_origin") is not None
                else None
            ),
            upload_method=("PUT" if upload_method == "PUT" else None),
            expires_at=_datetime(payload.get("expires_at")),
            installation_id=(
                int(str(installation_value)) if installation_value is not None else None
            ),
            channel_ref=_optional_ref(payload, "channel_id", "channel_domain"),
            dm_capability_id=(
                str(payload["bot_dm_capability_id"])
                if payload.get("bot_dm_capability_id") is not None
                else None
            ),
            dm_capability_revision=(
                int(str(payload["bot_dm_capability_revision"]))
                if payload.get("bot_dm_capability_revision") is not None
                else None
            ),
            installation_ref=_qualified_ref(
                payload, "bot_installation_ref", "installation_ref"
            ),
            installation_type=cast(Literal["guild", "user"] | None, installation_type),
        )

    async def refresh(self) -> Attachment:
        if self.dm_capability_id is not None and self.channel_ref is None:
            raise ValueError("a capability-backed attachment requires its channel")
        return await self.client.fetch_attachment(
            self.ref,
            target=self.target,
            installation_id=self.installation_id,
            channel_ref=self.channel_ref,
            dm_capability_id=self.dm_capability_id,
        )

    async def read(
        self, variant: str = "original", *, max_bytes: int | None = None
    ) -> bytes:
        if self.dm_capability_id is not None and self.channel_ref is None:
            raise ValueError("a capability-backed attachment requires its channel")
        return await self.client.download_attachment(
            self.ref,
            variant=variant,
            target=self.target,
            max_bytes=max_bytes,
            installation_id=self.installation_id,
            channel_ref=self.channel_ref,
            dm_capability_id=self.dm_capability_id,
        )


@dataclass(frozen=True, slots=True)
class _InteractionMessageRuntime:
    interaction_id: int
    kind: Literal["original", "followup"]
    response_id: int | None
    user_installation: bool


@dataclass(frozen=True, slots=True)
class _WebhookMessageRuntime:
    webhook_id: int
    token: str
    thread_id: EntityRef | None
    e2ee_device_id: str | None


@dataclass(frozen=True, slots=True)
class ForwardedMessageReference:
    """Source identity returned when a durable forward uses its saved snapshot."""

    source_channel_ref: EntityRef
    source_message_ref: EntityRef

    @classmethod
    def from_payload(cls, payload: object) -> ForwardedMessageReference:
        if not isinstance(payload, dict) or set(payload) != {
            "source_channel_ref",
            "source_message_ref",
        }:
            raise ValueError("forwarded-message reference response is invalid")
        return cls(
            source_channel_ref=EntityRef.parse(payload["source_channel_ref"]),
            source_message_ref=EntityRef.parse(payload["source_message_ref"]),
        )


@dataclass(slots=True)
class Message:
    client: Client
    target: str
    ref: EntityRef
    channel_ref: EntityRef
    author: User | None
    content: str | None
    created_at: datetime | None
    attachments: list[Attachment]
    message_type: int = 0
    thread: Channel | None = None
    content_unavailable: bool = False
    attachments_unavailable: bool = False
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    referenced_message_ref: EntityRef | None = None
    referenced_message: Message | None = None
    flags: int = 0
    pinned_at: datetime | None = None
    bot_installation_id: int | None = None
    embeds: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    sticker_items: list[dict[str, Any]] = field(default_factory=list)
    message_snapshots: list[dict[str, Any]] = field(default_factory=list)
    poll: dict[str, Any] | None = None
    poll_result: PollResult | None = None
    application_ref: EntityRef | None = None
    interaction_metadata: dict[str, Any] | None = None
    view_version: int = 0
    interaction_id: int | None = None
    interaction_response_id: int | None = None
    forwarded_message_ref: EntityRef | None = None
    reaction_counts: dict[str, int] = field(default_factory=dict)
    reacted_emoji: tuple[str, ...] = ()
    author_ref: EntityRef | None = None
    e2ee: dict[str, Any] | None = None
    encryption_policy_generation: int = 0
    encryption_epoch: int | None = None
    tts: bool = False
    client_nonce: str | None = None
    mention_user_refs: tuple[EntityRef, ...] = ()
    mention_role_refs: tuple[EntityRef, ...] = ()
    mention_everyone: bool = False
    allowed_mentions: dict[str, Any] = field(
        default_factory=lambda: {
            "parse": [],
            "users": [],
            "roles": [],
            "replied_user": False,
        }
    )
    webhook_ref: EntityRef | None = None
    webhook: dict[str, Any] | None = None
    published_at: datetime | None = None
    forwarded_channel_ref: EntityRef | None = None
    forward_snapshot: dict[str, Any] | None = None
    message_reference: dict[str, Any] | None = None
    view_persistent: bool = False
    view_expires_at: datetime | None = None
    interaction_integration_type: (
        Literal["guild_install", "user_install", "dm_capability"] | None
    ) = None
    interaction_installation_ref: EntityRef | None = None
    interaction_installation_revision: int | None = None
    dm_capability_id: str | None = None
    dm_capability_revision: int | None = None
    installation_ref: EntityRef | None = None
    installation_type: Literal["guild", "user"] | None = None
    _interaction_runtime: _InteractionMessageRuntime | None = field(
        default=None,
        repr=False,
    )
    _webhook_runtime: _WebhookMessageRuntime | None = field(default=None, repr=False)

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: dict[str, Any],
        *,
        _reference_depth: int = 0,
    ) -> Message:
        channel = payload.get("channel") or {}
        author = payload.get("author")
        channel_id = channel.get("id", payload.get("channel_id"))
        channel_domain = channel.get("origin_domain", payload.get("channel_domain"))
        if channel_id is None or channel_domain is None:
            raise ValueError(
                "message payload is missing its composite channel reference"
            )
        message_reference = payload.get("message_reference")
        referenced_message_ref = _optional_ref(
            payload, "referenced_message_id", "referenced_message_domain"
        )
        if referenced_message_ref is None and isinstance(message_reference, dict):
            referenced_message_ref = _optional_ref(
                message_reference, "message_id", "message_domain"
            )
        raw_referenced_message = payload.get("referenced_message")
        referenced_message = (
            cls.from_payload(
                client,
                target,
                raw_referenced_message,
                _reference_depth=_reference_depth + 1,
            )
            if _reference_depth == 0 and isinstance(raw_referenced_message, dict)
            else None
        )
        bot_installation_id = (
            int(payload["bot_installation_id"])
            if payload.get("bot_installation_id") is not None
            else None
        )
        dm_capability_id = (
            str(payload["bot_dm_capability_id"])
            if payload.get("bot_dm_capability_id") is not None
            else None
        )
        dm_capability_revision = (
            int(str(payload["bot_dm_capability_revision"]))
            if payload.get("bot_dm_capability_revision") is not None
            else None
        )
        installation_ref = _qualified_ref(
            payload, "bot_installation_ref", "installation_ref"
        )
        installation_type = payload.get(
            "bot_installation_type", payload.get("installation_type")
        )
        if installation_type is not None and installation_type not in {"guild", "user"}:
            raise ValueError("message response has an invalid installation type")
        interaction_integration_type = payload.get("interaction_integration_type")
        if (
            interaction_integration_type is not None
            and interaction_integration_type
            not in {
                "guild_install",
                "user_install",
                "dm_capability",
            }
        ):
            raise ValueError("message response has an invalid interaction lineage type")
        thread = (
            Channel.from_payload(client, target, payload["thread"])
            if isinstance(payload.get("thread"), dict)
            else None
        )
        if thread is not None:
            thread.bind_runtime(
                installation_id=bot_installation_id,
                dm_capability_id=dm_capability_id,
                dm_capability_revision=dm_capability_revision,
                installation_ref=installation_ref,
                installation_type=cast(
                    Literal["guild", "user"] | None, installation_type
                ),
                reject_unasserted=True,
            )
        message_channel_ref = EntityRef.from_wire(channel_id, channel_domain)
        attachments = [
            Attachment.from_payload(client, target, item)
            for item in payload.get("attachments") or []
            if isinstance(item, dict)
        ]
        for attachment in attachments:
            attachment.bind_runtime(
                channel_ref=message_channel_ref,
                installation_id=bot_installation_id,
                dm_capability_id=dm_capability_id,
                dm_capability_revision=dm_capability_revision,
                installation_ref=installation_ref,
                installation_type=cast(
                    Literal["guild", "user"] | None, installation_type
                ),
                reject_unasserted=True,
            )
        message_snapshots = [
            dict(item)
            for item in payload.get("message_snapshots", [])
            if isinstance(item, dict)
        ]
        direct_forward_snapshot = (
            dict(payload["forward_snapshot"])
            if isinstance(payload.get("forward_snapshot"), dict)
            else None
        )
        projected_forward_snapshot = (
            dict(message_snapshots[0]["message"])
            if len(message_snapshots) == 1
            and isinstance(message_snapshots[0].get("message"), dict)
            else None
        )
        if (
            direct_forward_snapshot is not None
            and projected_forward_snapshot is not None
            and direct_forward_snapshot != projected_forward_snapshot
        ):
            raise ValueError("message forward snapshot projections are inconsistent")
        raw_message_type = payload.get("message_type", 0)
        if isinstance(raw_message_type, bool) or not isinstance(raw_message_type, int):
            raise ValueError("message type must be an integer")
        if raw_message_type == 12 and (
            not isinstance(message_reference, dict)
            or _optional_ref(message_reference, "channel_id", "channel_domain") is None
            or _optional_ref(message_reference, "guild_id", "guild_domain") is None
        ):
            raise ValueError(
                "channel follow messages require qualified channel and guild references"
            )
        if raw_message_type == 46:
            poll_result = PollResult.from_payload(
                payload.get("poll_result"),
                referenced_message_ref=referenced_message_ref,
                embeds=payload.get("embeds"),
            )
            if referenced_message is not None:
                if referenced_message.ref != poll_result.poll_message_ref:
                    raise ValueError("poll result referenced message is inconsistent")
                if poll_result.source_encryption_mode == "e2ee" and isinstance(
                    referenced_message.poll, dict
                ):
                    poll_result = poll_result.with_verified_poll(
                        referenced_message.poll
                    )
        else:
            if payload.get("poll_result") is not None:
                raise ValueError("poll result metadata requires message type 46")
            poll_result = None
        raw_mention_users = payload.get("mention_user_refs", [])
        raw_mention_roles = payload.get("mention_role_refs", [])
        raw_mention_everyone = payload.get("mention_everyone", False)
        if (
            not isinstance(raw_mention_users, list)
            or len(raw_mention_users) > 5_000
            or not isinstance(raw_mention_roles, list)
            or len(raw_mention_roles) > 100
            or type(raw_mention_everyone) is not bool
        ):
            raise ValueError("message mention projection is invalid")
        mention_users = tuple(
            ref
            for item in raw_mention_users
            if isinstance(item, dict)
            and (ref := _optional_ref(item, "id", "origin_domain")) is not None
        )
        mention_roles = tuple(
            ref
            for item in raw_mention_roles
            if isinstance(item, dict)
            and (ref := _optional_ref(item, "id", "origin_domain")) is not None
        )
        if (
            len(mention_users) != len(raw_mention_users)
            or len(mention_roles) != len(raw_mention_roles)
            or len(mention_users) != len(set(mention_users))
            or len(mention_roles) != len(set(mention_roles))
        ):
            raise ValueError("message mention projection is invalid")
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            channel_ref=message_channel_ref,
            author=User.from_payload(author) if isinstance(author, dict) else None,
            content=(
                str(payload["content"]) if payload.get("content") is not None else None
            ),
            created_at=_datetime(payload.get("created_at")),
            attachments=attachments,
            message_type=raw_message_type,
            thread=thread,
            content_unavailable=_strict_payload_bool(
                payload, "content_unavailable", default=False
            ),
            attachments_unavailable=_strict_payload_bool(
                payload, "attachments_unavailable", default=False
            ),
            edited_at=_datetime(payload.get("edited_at")),
            deleted_at=_datetime(payload.get("deleted_at")),
            referenced_message_ref=(
                referenced_message_ref
                or (referenced_message.ref if referenced_message is not None else None)
            ),
            referenced_message=referenced_message,
            flags=int(payload.get("flags", 0)),
            pinned_at=_datetime(payload.get("pinned_at")),
            bot_installation_id=bot_installation_id,
            embeds=[
                dict(item)
                for item in payload.get("embeds", [])
                if isinstance(item, dict)
            ],
            components=[
                dict(item)
                for item in payload.get("components", [])
                if isinstance(item, dict)
            ],
            sticker_items=[
                dict(item)
                for item in payload.get("sticker_items", [])
                if isinstance(item, dict)
            ],
            message_snapshots=message_snapshots,
            poll=(
                dict(payload["poll"]) if isinstance(payload.get("poll"), dict) else None
            ),
            poll_result=poll_result,
            application_ref=_optional_ref(
                payload, "application_id", "application_domain"
            ),
            interaction_metadata=(
                dict(payload["interaction_metadata"])
                if isinstance(payload.get("interaction_metadata"), dict)
                else None
            ),
            view_version=int(payload.get("view_version", 0)),
            interaction_id=(
                EntityRef.from_wire(
                    payload["interaction_id"], message_channel_ref.domain
                ).id
                if payload.get("interaction_id") is not None
                else None
            ),
            interaction_response_id=(
                EntityRef.from_wire(
                    payload["response_id"], message_channel_ref.domain
                ).id
                if payload.get("response_id") is not None
                else None
            ),
            forwarded_message_ref=_optional_ref(
                payload, "forwarded_message_id", "forwarded_message_domain"
            )
            or _qualified_ref(payload, "forwarded_message_ref"),
            reaction_counts={
                str(key): int(value)
                for key, value in (payload.get("reaction_counts") or {}).items()
            },
            reacted_emoji=tuple(str(item) for item in payload.get("reacted_emoji", [])),
            author_ref=_optional_ref(payload, "author_id", "author_domain"),
            e2ee=(
                dict(payload["e2ee"]) if isinstance(payload.get("e2ee"), dict) else None
            ),
            encryption_policy_generation=int(
                str(payload.get("encryption_policy_generation", 0))
            ),
            encryption_epoch=(
                int(str(payload["encryption_epoch"]))
                if payload.get("encryption_epoch") is not None
                else None
            ),
            tts=_strict_payload_bool(payload, "tts", default=False),
            client_nonce=(
                str(payload["client_nonce"])
                if payload.get("client_nonce") is not None
                else None
            ),
            mention_user_refs=mention_users,
            mention_role_refs=mention_roles,
            mention_everyone=raw_mention_everyone,
            webhook_ref=(
                _qualified_ref(payload["webhook"], "ref")
                or _optional_ref(payload["webhook"], "id", "origin_domain")
                if isinstance(payload.get("webhook"), dict)
                else _optional_ref(payload, "webhook_id", "webhook_domain")
            ),
            webhook=(
                dict(payload["webhook"])
                if isinstance(payload.get("webhook"), dict)
                else None
            ),
            published_at=_datetime(payload.get("published_at")),
            forwarded_channel_ref=_optional_ref(
                payload, "forwarded_channel_id", "forwarded_channel_domain"
            ),
            forward_snapshot=direct_forward_snapshot or projected_forward_snapshot,
            message_reference=(
                dict(message_reference) if isinstance(message_reference, dict) else None
            ),
            view_persistent=_strict_payload_bool(
                payload, "view_persistent", default=False
            ),
            view_expires_at=_datetime(payload.get("view_expires_at")),
            interaction_integration_type=cast(
                Literal["guild_install", "user_install", "dm_capability"] | None,
                interaction_integration_type,
            ),
            interaction_installation_ref=_qualified_ref(
                payload,
                "interaction_installation_ref",
            ),
            interaction_installation_revision=(
                int(str(payload["interaction_installation_revision"]))
                if payload.get("interaction_installation_revision") is not None
                else None
            ),
            dm_capability_id=dm_capability_id,
            dm_capability_revision=dm_capability_revision,
            installation_ref=installation_ref,
            installation_type=cast(Literal["guild", "user"] | None, installation_type),
        )

    def bind_runtime(
        self,
        *,
        installation_id: int | None = None,
        dm_capability_id: str | None = None,
        dm_capability_revision: int | None = None,
        installation_ref: EntityRef | None = None,
        installation_type: Literal["guild", "user"] | None = None,
        reject_unasserted: bool = False,
    ) -> Message:
        """Pin one exact runtime grant to this message and its owned children."""

        if installation_id is not None and dm_capability_id is not None:
            raise ValueError(
                "installation and DM capability grants are mutually exclusive"
            )
        _bind_optional_context(
            self,
            {
                "bot_installation_id": installation_id,
                "dm_capability_id": dm_capability_id,
                "dm_capability_revision": dm_capability_revision,
                "installation_ref": installation_ref,
                "installation_type": installation_type,
            },
            context="message response",
            reject_unasserted=reject_unasserted,
        )
        for attachment in self.attachments:
            attachment.bind_runtime(
                channel_ref=self.channel_ref,
                installation_id=self.bot_installation_id,
                dm_capability_id=self.dm_capability_id,
                dm_capability_revision=self.dm_capability_revision,
                installation_ref=self.installation_ref,
                installation_type=self.installation_type,
                reject_unasserted=True,
            )
        if self.thread is not None:
            self.thread.bind_runtime(
                installation_id=self.bot_installation_id,
                dm_capability_id=self.dm_capability_id,
                dm_capability_revision=self.dm_capability_revision,
                installation_ref=self.installation_ref,
                installation_type=self.installation_type,
                reject_unasserted=True,
            )
        return self

    def bind_interaction_lifecycle(
        self,
        interaction_id: int,
        *,
        kind: Literal["original", "followup"],
        response_id: int | None = None,
        user_installation: bool = False,
    ) -> Message:
        if self._webhook_runtime is not None:
            raise ValueError("message lifecycle authority cannot be combined")
        if kind == "followup" and response_id is None:
            raise ValueError("a follow-up message requires its response ID")
        runtime = _InteractionMessageRuntime(
            interaction_id=interaction_id,
            kind=kind,
            response_id=response_id,
            user_installation=user_installation,
        )
        if self._interaction_runtime not in {None, runtime}:
            raise ValueError("interaction message lifecycle context conflicts")
        if self.interaction_id not in {None, interaction_id}:
            raise ValueError("interaction message identity conflicts")
        if response_id is not None and self.interaction_response_id not in {
            None,
            response_id,
        }:
            raise ValueError("interaction response identity conflicts")
        self.interaction_id = interaction_id
        if response_id is not None:
            self.interaction_response_id = response_id
        self._interaction_runtime = runtime
        return self

    def bind_webhook_lifecycle(
        self,
        webhook_id: int,
        token: str,
        *,
        thread_id: EntityRef | None = None,
        e2ee_device_id: str | None = None,
    ) -> Message:
        if self._interaction_runtime is not None:
            raise ValueError("message lifecycle authority cannot be combined")
        runtime = _WebhookMessageRuntime(
            webhook_id,
            token,
            thread_id,
            e2ee_device_id,
        )
        if self._webhook_runtime not in {None, runtime}:
            raise ValueError("webhook message lifecycle context conflicts")
        self._webhook_runtime = runtime
        return self

    def _require_generic_message_grant(self) -> None:
        if self._webhook_runtime is not None:
            raise ValueError(
                "this webhook message supports only webhook lifecycle operations"
            )
        runtime = self._interaction_runtime
        if runtime is not None and (
            runtime.user_installation
            or (self.bot_installation_id is None and self.dm_capability_id is None)
        ):
            raise ValueError(
                "this interaction response supports only interaction lifecycle operations"
            )

    @property
    def is_voice_message(self) -> bool:
        """Whether this message carries Discord-compatible voice-message media."""

        return bool(self.flags & (1 << 13))

    @property
    def message_reference_channel_ref(self) -> EntityRef | None:
        """Qualified channel identified by the Discord message-reference object."""

        return (
            _optional_ref(self.message_reference, "channel_id", "channel_domain")
            if isinstance(self.message_reference, dict)
            else None
        )

    @property
    def message_reference_guild_ref(self) -> EntityRef | None:
        """Qualified guild identified by the Discord message-reference object."""

        return (
            _optional_ref(self.message_reference, "guild_id", "guild_domain")
            if isinstance(self.message_reference, dict)
            else None
        )

    @property
    def followed_channel_ref(self) -> EntityRef | None:
        """Announcement channel followed by this type-12 system message."""

        return self.message_reference_channel_ref if self.message_type == 12 else None

    @property
    def followed_guild_ref(self) -> EntityRef | None:
        """Guild owning the announcement channel followed by this message."""

        return self.message_reference_guild_ref if self.message_type == 12 else None

    async def reply(
        self,
        content: str | None = None,
        *,
        attachment_ids: list[int] | None = None,
        sticker_ids: Sequence[EntityRef] = (),
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        allowed_mentions: Mapping[str, object] | None = None,
        mention_author: bool = False,
        tts: bool = False,
        voice_message: bool = False,
    ) -> Message:
        self._require_generic_message_grant()
        options: dict[str, Any] = {}
        if attachment_ids is not None:
            options["attachment_ids"] = attachment_ids
        if sticker_ids:
            options["sticker_ids"] = list(sticker_ids)
        if embeds:
            options["embeds"] = list(embeds)
        if view is not None:
            options["view"] = view
        if poll is not None:
            options["poll"] = poll
        mention_policy = (
            dict(allowed_mentions) if allowed_mentions is not None else None
        )
        if mention_author and mention_policy is not None:
            mention_policy["replied_user"] = True
        if mention_policy is not None:
            options["allowed_mentions"] = mention_policy
        reply_author_requested = mention_author or bool(
            mention_policy is not None and mention_policy.get("replied_user") is True
        )
        replied_user_ref = self.author_ref
        if replied_user_ref is None and self.author is not None:
            replied_user_ref = self.author.ref
        if reply_author_requested:
            if replied_user_ref is None:
                raise ValueError("reply author reference is unavailable")
            options["replied_user_ref"] = replied_user_ref
        if tts:
            options["tts"] = True
        if voice_message:
            options["voice_message"] = True
        return await self.client.send_message(
            self.channel_ref,
            content,
            target=self.target,
            reply_to=self.ref,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
            **options,
        )

    async def edit(
        self,
        content: str | None | MissingType = MISSING,
        *,
        embeds: list[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        view_version: int | None = None,
    ) -> Message:
        webhook_runtime = self._webhook_runtime
        if webhook_runtime is not None:
            if view_version is not None:
                raise ValueError("webhook lifecycle edits do not use view versions")
            return await self.client.edit_webhook_message(
                webhook_runtime.webhook_id,
                webhook_runtime.token,
                self.ref,
                target=self.target,
                content=content,
                embeds=embeds,
                view=view,
                components=components,
                allowed_mentions=(
                    dict(allowed_mentions)
                    if isinstance(allowed_mentions, Mapping)
                    else allowed_mentions
                ),
                e2ee=e2ee,
                attachment_ids=attachment_ids,
                flags=flags,
                thread_id=webhook_runtime.thread_id,
                e2ee_device_id=(
                    webhook_runtime.e2ee_device_id
                    if not isinstance(e2ee, MissingType) and e2ee is not None
                    else None
                ),
            )
        interaction_runtime = self._interaction_runtime
        if interaction_runtime is not None:
            kwargs: dict[str, Any] = {
                "target": self.target,
                "content": content,
                "embeds": embeds,
                "view": view,
                "components": components,
                "allowed_mentions": allowed_mentions,
                "e2ee": e2ee,
                "attachment_ids": attachment_ids,
                "flags": flags,
                "view_version": view_version,
                "installation_id": self.bot_installation_id,
                "user_installation": interaction_runtime.user_installation,
            }
            if interaction_runtime.kind == "original":
                rendered = await self.client.edit_original_interaction_response(
                    interaction_runtime.interaction_id,
                    **kwargs,
                )
            else:
                response_id = interaction_runtime.response_id
                if response_id is None:
                    raise RuntimeError("follow-up lifecycle binding is incomplete")
                rendered = await self.client.edit_interaction_followup(
                    interaction_runtime.interaction_id,
                    response_id,
                    **kwargs,
                )
            if not isinstance(rendered, Message):
                raise RuntimeError(
                    "public interaction edit returned a private response"
                )
            return rendered
        self._require_generic_message_grant()
        return await self.client.edit_message(
            self.channel_ref,
            self.ref,
            content,
            target=self.target,
            embeds=embeds,
            view=view,
            components=components,
            allowed_mentions=allowed_mentions,
            e2ee=e2ee,
            attachment_ids=attachment_ids,
            flags=flags,
            view_version=view_version,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def delete(self) -> None:
        webhook_runtime = self._webhook_runtime
        if webhook_runtime is not None:
            await self.client.delete_webhook_message(
                webhook_runtime.webhook_id,
                webhook_runtime.token,
                self.ref,
                target=self.target,
                thread_id=webhook_runtime.thread_id,
            )
            return
        interaction_runtime = self._interaction_runtime
        if interaction_runtime is not None:
            if interaction_runtime.kind == "original":
                await self.client.delete_original_interaction_response(
                    interaction_runtime.interaction_id,
                    target=self.target,
                )
            else:
                response_id = interaction_runtime.response_id
                if response_id is None:
                    raise RuntimeError("follow-up lifecycle binding is incomplete")
                await self.client.delete_interaction_followup(
                    interaction_runtime.interaction_id,
                    response_id,
                    target=self.target,
                )
            return
        self._require_generic_message_grant()
        await self.client.delete_message(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def add_reaction(self, emoji: str) -> None:
        self._require_generic_message_grant()
        await self.client.add_reaction(
            self.channel_ref,
            self.ref,
            emoji,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def remove_reaction(self, emoji: str) -> None:
        self._require_generic_message_grant()
        await self.client.remove_reaction(
            self.channel_ref,
            self.ref,
            emoji,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def remove_user_reaction(self, user: EntityRef, emoji: str) -> None:
        """Remove one member's reaction when this bot may manage messages."""

        self._require_generic_message_grant()
        await self.client.remove_user_reaction(
            self.channel_ref,
            self.ref,
            user,
            emoji,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def clear_reactions(self) -> None:
        """Remove every reaction from this message."""

        self._require_generic_message_grant()
        await self.client.clear_reactions(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def clear_reaction(self, emoji: str) -> None:
        """Remove every reaction matching one emoji from this message."""

        self._require_generic_message_grant()
        await self.client.clear_reaction(
            self.channel_ref,
            self.ref,
            emoji,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def reaction_users(
        self,
        emoji: str,
        *,
        after: EntityRef | None = None,
        limit: int = 50,
    ) -> tuple[list[User], int, EntityRef | None]:
        """Return a page of users who reacted, its total, and the next cursor."""

        self._require_generic_message_grant()
        return await self.client.reaction_users(
            self.channel_ref,
            self.ref,
            emoji,
            target=self.target,
            after=after,
            limit=limit,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def vote(self, answer_id: int) -> None:
        self._require_generic_message_grant()
        await self.client.add_poll_vote(
            self.channel_ref,
            self.ref,
            answer_id,
            target=self.target,
            installation_id=self.bot_installation_id,
        )

    async def remove_vote(self, answer_id: int) -> None:
        self._require_generic_message_grant()
        await self.client.remove_poll_vote(
            self.channel_ref,
            self.ref,
            answer_id,
            target=self.target,
            installation_id=self.bot_installation_id,
        )

    async def end_poll(self) -> Message | dict[str, Any]:
        interaction_runtime = self._interaction_runtime
        if interaction_runtime is not None:
            return await self.client.finalize_interaction_poll(
                interaction_runtime.interaction_id,
                response_id=(
                    interaction_runtime.response_id
                    if interaction_runtime.kind == "followup"
                    else None
                ),
                target=self.target,
                installation_id=self.bot_installation_id,
                user_installation=interaction_runtime.user_installation,
            )
        self._require_generic_message_grant()
        return await self.client.finalize_poll(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def resolve_forwarded(
        self,
    ) -> Message | ForwardedMessageReference:
        """Resolve this forward while binding the result to its saved source refs."""

        self._require_generic_message_grant()
        return await self.client.resolve_forwarded_message(
            self.channel_ref,
            self,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def publish(self) -> Message:
        self._require_generic_message_grant()
        return await self.client.crosspost_message(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )

    async def pin(self, *, reason: str | None = None) -> None:
        self._require_generic_message_grant()
        await self.client.pin_message(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
            reason=reason,
        )

    async def unpin(self, *, reason: str | None = None) -> None:
        self._require_generic_message_grant()
        await self.client.unpin_message(
            self.channel_ref,
            self.ref,
            target=self.target,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
            reason=reason,
        )

    async def start_thread(
        self,
        name: str,
        *,
        reason: str | None = None,
        auto_archive_duration: int | None = None,
        rate_limit_per_user: int | None = None,
    ) -> Channel:
        self._require_generic_message_grant()
        return await self.client.start_thread_from_message(
            self.channel_ref,
            self.ref,
            name,
            target=self.target,
            reason=reason,
            auto_archive_duration=auto_archive_duration,
            rate_limit_per_user=rate_limit_per_user,
            installation_id=self.bot_installation_id,
            dm_capability_id=self.dm_capability_id,
        )


@dataclass(slots=True)
class MessageSearchResult:
    message: Message
    channel: Channel
    guild: Guild
    snippet: str

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: dict[str, Any],
    ) -> MessageSearchResult:
        raw_message = payload.get("message")
        raw_channel = payload.get("channel")
        raw_guild = payload.get("guild")
        raw_snippet = payload.get("snippet", "")
        if (
            not isinstance(raw_message, dict)
            or not isinstance(raw_channel, dict)
            or not isinstance(raw_guild, dict)
            or not isinstance(raw_snippet, str)
            or len(raw_snippet) > 280
        ):
            raise ValueError("message search result is invalid")
        message = Message.from_payload(client, target, raw_message)
        channel = Channel.from_payload(client, target, raw_channel)
        guild = Guild.from_payload(client, target, raw_guild)
        if message.channel_ref != channel.ref or channel.guild_ref != guild.ref:
            raise ValueError("message search result linkage is invalid")
        return cls(message=message, channel=channel, guild=guild, snippet=raw_snippet)


@dataclass(slots=True)
class MessageSearchPage:
    results: tuple[MessageSearchResult, ...]
    next_cursor: str | None
    encrypted_channel_refs: tuple[EntityRef, ...]
    indexing: bool
    coverage: dict[str, str]

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: object,
    ) -> MessageSearchPage:
        if not isinstance(payload, dict):
            raise ValueError("message search response is invalid")
        raw_results = payload.get("results")
        raw_encrypted = payload.get("encrypted_channel_refs", [])
        raw_cursor = payload.get("next_cursor")
        raw_indexing = payload.get("indexing", False)
        raw_coverage = payload.get("coverage", {})
        if (
            not isinstance(raw_results, list)
            or len(raw_results) > 25
            or not isinstance(raw_encrypted, list)
            or len(raw_encrypted) > 10_000
            or (raw_cursor is not None and not isinstance(raw_cursor, str))
            or (isinstance(raw_cursor, str) and len(raw_cursor) > 512)
            or type(raw_indexing) is not bool
            or not isinstance(raw_coverage, dict)
            or len(raw_coverage) > 8
            or any(
                not isinstance(key, str)
                or len(key) > 64
                or not isinstance(value, str)
                or len(value) > 64
                for key, value in raw_coverage.items()
            )
        ):
            raise ValueError("message search response is invalid")
        if any(not isinstance(item, dict) for item in raw_results):
            raise ValueError("message search response is invalid")
        results = tuple(
            MessageSearchResult.from_payload(client, target, item)
            for item in raw_results
        )
        try:
            if any(not isinstance(item, str) for item in raw_encrypted):
                raise ValueError("encrypted channel references must be strings")
            encrypted = tuple(EntityRef.parse(item) for item in raw_encrypted)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("message search response is invalid") from exc
        return cls(
            results=results,
            next_cursor=raw_cursor,
            encrypted_channel_refs=encrypted,
            indexing=raw_indexing,
            coverage={str(key): str(value) for key, value in raw_coverage.items()},
        )


@dataclass(frozen=True, slots=True)
class MessagePin:
    """One entry from Discord's current channel-pins resource."""

    pinned_at: datetime
    message: Message

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: object,
    ) -> MessagePin:
        if not isinstance(payload, dict):
            raise ValueError("message pin entry is invalid")
        raw_pinned_at = payload.get("pinned_at")
        raw_message = payload.get("message")
        if not isinstance(raw_pinned_at, str) or not isinstance(raw_message, dict):
            raise ValueError("message pin entry is invalid")
        pinned_at = _datetime(raw_pinned_at)
        if pinned_at is None or pinned_at.tzinfo is None:
            raise ValueError("message pin timestamp must include a timezone")
        message = Message.from_payload(client, target, raw_message)
        if message.pinned_at is not None and message.pinned_at != pinned_at:
            raise ValueError("message pin timestamp conflicts with its message")
        message.pinned_at = pinned_at
        return cls(pinned_at=pinned_at, message=message)


@dataclass(frozen=True, slots=True)
class MessagePinPage:
    items: tuple[MessagePin, ...]
    has_more: bool

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: object,
        *,
        channel_ref: EntityRef,
    ) -> MessagePinPage:
        if not isinstance(payload, dict):
            raise ValueError("message pins response is invalid")
        raw_items = payload.get("items")
        raw_has_more = payload.get("has_more")
        if (
            not isinstance(raw_items, list)
            or len(raw_items) > 50
            or type(raw_has_more) is not bool
        ):
            raise ValueError("message pins response is invalid")
        items = tuple(
            MessagePin.from_payload(client, target, item) for item in raw_items
        )
        if any(item.message.channel_ref != channel_ref for item in items):
            raise ValueError("message pin belongs to a different channel")
        refs = tuple(item.message.ref for item in items)
        if len(refs) != len(set(refs)):
            raise ValueError("message pins response contains duplicates")
        return cls(items=items, has_more=raw_has_more)


@dataclass(frozen=True, slots=True)
class InteractionSourceMessage:
    """A safe snapshot of an ephemeral interaction response.

    Unlike :class:`Message`, this object has no channel-message mutation or
    history methods.  Its ``ref`` is the authority-qualified private response
    identity, not a durable channel-message identity.
    """

    client: Client
    target: str
    ref: EntityRef
    response_ref: EntityRef
    interaction_ref: EntityRef
    channel_ref: EntityRef
    application_ref: EntityRef
    author: User
    content: str | None
    e2ee: dict[str, Any] | None
    embeds: tuple[dict[str, Any], ...]
    components: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]
    poll: dict[str, Any] | None
    flags: int
    tts: bool
    message_type: int
    interaction_metadata: dict[str, Any]
    view_version: int
    view_expires_at: datetime | None
    created_at: datetime
    sequence: int
    revision: int
    raw: dict[str, Any]
    ephemeral: Literal[True] = True
    durable: Literal[False] = False

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: dict[str, Any],
    ) -> InteractionSourceMessage:
        if payload.get("ephemeral") is not True or payload.get("durable") is not False:
            raise ValueError(
                "interaction source is not an ephemeral response projection"
            )
        origin = payload.get("origin_domain")
        if not isinstance(origin, str):
            raise ValueError("interaction source has no authority")
        response_id = _canonical_decimal(
            payload.get("response_id"), field_name="interaction source response id"
        )
        message_id = _canonical_decimal(
            payload.get("id"), field_name="interaction source id"
        )
        parent_interaction_id = _canonical_decimal(
            payload.get("interaction_id"),
            field_name="interaction source parent interaction id",
        )
        application_id = _canonical_decimal(
            payload.get("application_id"),
            field_name="interaction source application id",
        )
        if (
            response_id <= 0
            or message_id != response_id
            or parent_interaction_id <= 0
            or application_id <= 0
        ):
            raise ValueError("interaction source response identity is invalid")
        response_ref = EntityRef.parse(payload.get("response_ref"))
        interaction_ref = EntityRef.parse(payload.get("interaction_ref"))
        channel_ref = EntityRef.parse(payload.get("channel_ref"))
        application_ref = EntityRef.parse(payload.get("application_ref"))
        if (
            response_ref != EntityRef(response_id, origin)
            or interaction_ref != EntityRef(parent_interaction_id, origin)
            or channel_ref.domain != origin
            or payload.get("channel_id") != str(channel_ref.id)
            or payload.get("channel_domain") != channel_ref.domain
            or application_ref.id != application_id
            or payload.get("application_domain") != application_ref.domain
        ):
            raise ValueError("interaction source authority binding is invalid")
        raw_author = payload.get("author")
        if not isinstance(raw_author, dict):
            raise ValueError("interaction source author is missing")
        author = User.from_payload(raw_author)
        if (
            payload.get("author_id") != str(author.ref.id)
            or payload.get("author_domain") != author.ref.domain
        ):
            raise ValueError("interaction source author binding is invalid")
        raw_content = payload.get("content")
        raw_e2ee = payload.get("e2ee")
        if raw_content is not None and not isinstance(raw_content, str):
            raise ValueError("interaction source content is invalid")
        if raw_e2ee is not None and not isinstance(raw_e2ee, dict):
            raise ValueError("interaction source encryption envelope is invalid")
        if raw_content is not None and raw_e2ee is not None:
            raise ValueError(
                "interaction source combines plaintext and encrypted content"
            )
        collections: dict[str, tuple[dict[str, Any], ...]] = {}
        for key in ("embeds", "components", "attachments"):
            raw_items = payload.get(key, [])
            if not isinstance(raw_items, list) or any(
                not isinstance(item, dict) for item in raw_items
            ):
                raise ValueError(f"interaction source {key} are invalid")
            collections[key] = tuple(dict(item) for item in raw_items)
        raw_poll = payload.get("poll")
        if raw_poll is not None and not isinstance(raw_poll, dict):
            raise ValueError("interaction source poll is invalid")
        raw_metadata = payload.get("interaction_metadata")
        if not isinstance(raw_metadata, dict):
            raise ValueError("interaction source metadata is missing")
        created_at = _datetime(payload.get("created_at"))
        if created_at is None or created_at.tzinfo is None:
            raise ValueError("interaction source creation time is invalid")
        raw_flags = payload.get("flags")
        raw_type = payload.get("message_type")
        raw_view_version = payload.get("view_version")
        raw_sequence = payload.get("sequence")
        raw_revision = _canonical_decimal(
            payload.get("revision"), field_name="interaction source revision"
        )
        if (
            type(raw_flags) is not int
            or raw_flags < 0
            or not raw_flags & 64
            or type(raw_type) is not int
            or type(raw_view_version) is not int
            or raw_view_version < 0
            or type(raw_sequence) is not int
            or raw_sequence < 0
            or raw_revision <= 0
            or type(payload.get("tts")) is not bool
        ):
            raise ValueError("interaction source message state is invalid")
        view_expires_at = _datetime(payload.get("view_expires_at"))
        if payload.get("view_expires_at") is not None and (
            view_expires_at is None or view_expires_at.tzinfo is None
        ):
            raise ValueError("interaction source view expiry is invalid")
        return cls(
            client=client,
            target=target,
            ref=response_ref,
            response_ref=response_ref,
            interaction_ref=interaction_ref,
            channel_ref=channel_ref,
            application_ref=application_ref,
            author=author,
            content=raw_content,
            e2ee=dict(raw_e2ee) if isinstance(raw_e2ee, dict) else None,
            embeds=collections["embeds"],
            components=collections["components"],
            attachments=collections["attachments"],
            poll=dict(raw_poll) if isinstance(raw_poll, dict) else None,
            flags=raw_flags,
            tts=payload["tts"],
            message_type=raw_type,
            interaction_metadata=dict(raw_metadata),
            view_version=raw_view_version,
            view_expires_at=view_expires_at,
            created_at=created_at,
            sequence=raw_sequence,
            revision=raw_revision,
            raw=dict(payload),
        )


@dataclass(slots=True)
class Interaction:
    client: Client
    target: str
    id: int
    application_ref: EntityRef
    guild_ref: EntityRef | None
    channel_ref: EntityRef
    user: User
    command: dict[str, Any] | None
    options: dict[str, Any] | None
    encrypted_payload: dict[str, Any] | None
    command_id: int | None = None
    expires_at: datetime | None = None
    token: str | None = None
    type: str = "command"
    context: str = "guild"
    integration_type: str = "guild_install"
    version: int = 1
    locale: str | None = None
    guild_locale: str | None = None
    app_permissions: int | None = None
    authorizing_integration_owners: dict[str, EntityRef | Literal["0"]] = field(
        default_factory=dict
    )
    attachment_size_limit: int | None = None
    member: Member | None = None
    message: Message | InteractionSourceMessage | None = None
    installation_id: int | None = None
    user_installation_id: int | None = None
    message_ref: EntityRef | None = None
    response_id: int | None = None
    view_version: int | None = None
    autocomplete_generation: int | None = None
    focused_option: str | None = None
    target_ref: EntityRef | None = None
    target_id: int | None = None
    resolved: dict[str, Any] | None = None
    custom_id: str | None = None
    component_type: int | str | None = None
    values: tuple[str, ...] = ()
    components: tuple[dict[str, Any], ...] = ()
    attachment_manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_component: dict[str, Any] | None = None
    source_modal: dict[str, Any] | None = None

    @property
    def ref(self) -> EntityRef:
        """The authority-qualified interaction identity."""

        return EntityRef(self.id, self.channel_ref.domain)

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> Interaction:
        wire_versioned = "version" in payload
        raw_version = payload.get("version", 1)
        if type(raw_version) is not int or raw_version != 1:
            raise ValueError("interaction version must be 1")

        interaction_type = payload.get("type", "command")
        context = payload.get("context", "guild")
        integration_type = payload.get("integration_type", "guild_install")
        if (
            not isinstance(interaction_type, str)
            or interaction_type not in _INTERACTION_TYPES
        ):
            raise ValueError("interaction type is invalid")
        if not isinstance(context, str) or context not in _INTERACTION_CONTEXTS:
            raise ValueError("interaction context is invalid")
        if (
            not isinstance(integration_type, str)
            or integration_type not in _INTERACTION_INTEGRATION_TYPES
        ):
            raise ValueError("interaction integration type is invalid")
        if wire_versioned:
            if integration_type == "guild_install" and context != "guild":
                raise ValueError("guild-install interactions require a guild context")
            if integration_type == "dm_capability" and context != "bot_dm":
                raise ValueError("DM-capability interactions require a bot DM context")

        application_ref = EntityRef.parse(payload["application_ref"])
        guild_ref = (
            EntityRef.parse(payload["guild_ref"])
            if payload.get("guild_ref") is not None
            else None
        )
        channel_ref = EntityRef.parse(payload["channel_ref"])
        interaction_ref = EntityRef.from_wire(payload.get("id"), channel_ref.domain)
        interaction_id = interaction_ref.id
        raw_interaction_ref = payload.get("interaction_ref")
        if raw_interaction_ref is not None:
            if (
                not isinstance(raw_interaction_ref, str)
                or EntityRef.parse(raw_interaction_ref) != interaction_ref
            ):
                raise ValueError("interaction_ref conflicts with its authority")
        elif wire_versioned:
            raise ValueError("interaction_ref is required")
        if wire_versioned and (context == "guild") != (guild_ref is not None):
            raise ValueError("interaction guild reference conflicts with its context")

        raw_locale = payload.get("locale")
        locale = (
            _interaction_locale(raw_locale, field_name="locale")
            if raw_locale is not None
            else None
        )
        raw_guild_locale = payload.get("guild_locale")
        guild_locale = (
            _interaction_locale(raw_guild_locale, field_name="guild_locale")
            if raw_guild_locale is not None
            else None
        )
        if context != "guild" and guild_locale is not None:
            raise ValueError("private interactions cannot include guild_locale")

        app_permissions = _optional_permission_bits(payload, "app_permissions")
        raw_owners = payload.get("authorizing_integration_owners")
        owners = (
            _authorizing_integration_owners(raw_owners)
            if raw_owners is not None
            else {}
        )
        if owners.get("guild_install") == "0" and not (
            context == "bot_dm" and integration_type == "dm_capability"
        ):
            raise ValueError(
                "interaction guild owner sentinel is only valid for bot-DM capabilities"
            )
        selected_owner_type = (
            integration_type if integration_type != "dm_capability" else None
        )
        if (
            selected_owner_type is not None
            and owners
            and selected_owner_type not in owners
        ):
            raise ValueError(
                "interaction authorizing owners omit the selected installation"
            )

        raw_attachment_size_limit = payload.get("attachment_size_limit")
        if raw_attachment_size_limit is None:
            attachment_size_limit = None
        elif (
            type(raw_attachment_size_limit) is not int
            or not 0 < raw_attachment_size_limit <= (1 << 63) - 1
        ):
            raise ValueError(
                "interaction attachment_size_limit must be a positive integer"
            )
        else:
            attachment_size_limit = raw_attachment_size_limit

        raw_member = payload.get("member")
        if raw_member is not None and not isinstance(raw_member, dict):
            raise ValueError("interaction member must be an object")
        member = (
            Member.from_payload(client, target, raw_member)
            if isinstance(raw_member, dict)
            else None
        )
        raw_user = payload.get("user")
        if raw_user is not None and not isinstance(raw_user, dict):
            raise ValueError("interaction user must be an object")
        top_level_user = (
            User.from_payload(raw_user) if isinstance(raw_user, dict) else None
        )
        if member is not None and guild_ref != member.guild_ref:
            raise ValueError("interaction member belongs to a different guild")

        if wire_versioned:
            if locale is None:
                raise ValueError("interaction locale is required")
            if app_permissions is None:
                raise ValueError("interaction app_permissions is required")
            if not owners:
                raise ValueError(
                    "interaction authorizing_integration_owners is required"
                )
            if attachment_size_limit is None:
                raise ValueError("interaction attachment_size_limit is required")
            if context == "guild":
                if member is None or top_level_user is not None or guild_locale is None:
                    raise ValueError(
                        "guild interactions require member and guild_locale, not user"
                    )
                if member.permissions is None:
                    raise ValueError("interaction member permissions are required")
            elif member is not None or top_level_user is None:
                raise ValueError("private interactions require user, not member")

        user = top_level_user or (member.user if member is not None else None)
        if user is None:
            raise ValueError("interaction actor is missing")
        raw_user_ref = payload.get("user_ref")
        if raw_user_ref is not None and (
            not isinstance(raw_user_ref, str)
            or EntityRef.parse(raw_user_ref) != user.ref
        ):
            raise ValueError("interaction user_ref conflicts with its actor")

        response_id = (
            EntityRef.from_wire(payload["response_id"], channel_ref.domain).id
            if payload.get("response_id") is not None
            else None
        )
        message_ref = (
            EntityRef.parse(payload["message_ref"])
            if payload.get("message_ref") is not None
            else None
        )
        raw_message = payload.get("message")
        if raw_message is not None and not isinstance(raw_message, dict):
            raise ValueError("interaction message must be an object")
        message = None
        if isinstance(raw_message, dict):
            message = (
                InteractionSourceMessage.from_payload(client, target, raw_message)
                if raw_message.get("durable") is False
                else Message.from_payload(client, target, raw_message)
            )
        if message is not None:
            if message.channel_ref != channel_ref:
                raise ValueError("interaction message belongs to a different channel")
            if (
                isinstance(message, Message)
                and message_ref is not None
                and message.ref != message_ref
            ):
                raise ValueError("interaction message conflicts with message_ref")
            if isinstance(message, Message):
                message_ref = message.ref
            elif message_ref is not None:
                raise ValueError(
                    "ephemeral interaction source cannot include a durable message_ref"
                )
            elif (
                response_id is None
                or message.response_ref != EntityRef(response_id, channel_ref.domain)
                or message.application_ref != application_ref
            ):
                raise ValueError(
                    "ephemeral interaction source conflicts with interaction authority"
                )

        return cls(
            client=client,
            target=target,
            id=interaction_id,
            application_ref=application_ref,
            guild_ref=guild_ref,
            channel_ref=channel_ref,
            user=user,
            command=(
                dict(payload["command"])
                if isinstance(payload.get("command"), dict)
                else None
            ),
            options=payload.get("options"),
            encrypted_payload=payload.get("encrypted_payload"),
            command_id=(
                int(payload["command_id"])
                if payload.get("command_id") is not None
                else None
            ),
            expires_at=_datetime(payload.get("expires_at")),
            token=(str(payload["token"]) if payload.get("token") is not None else None),
            type=interaction_type,
            context=context,
            integration_type=integration_type,
            version=raw_version,
            locale=locale,
            guild_locale=guild_locale,
            app_permissions=app_permissions,
            authorizing_integration_owners=owners,
            attachment_size_limit=attachment_size_limit,
            member=member,
            message=message,
            installation_id=(
                int(payload["installation_id"])
                if payload.get("installation_id") is not None
                else None
            ),
            user_installation_id=(
                int(payload["user_installation_id"])
                if payload.get("user_installation_id") is not None
                else None
            ),
            message_ref=message_ref,
            response_id=response_id,
            view_version=(
                int(payload["view_version"])
                if payload.get("view_version") is not None
                else None
            ),
            autocomplete_generation=(
                int(payload["autocomplete_generation"])
                if payload.get("autocomplete_generation") is not None
                else None
            ),
            focused_option=(
                str(payload["focused_option"])
                if payload.get("focused_option") is not None
                else None
            ),
            target_ref=(
                EntityRef.parse(payload["target_ref"])
                if payload.get("target_ref") is not None
                else None
            ),
            target_id=(
                int(payload["target_id"])
                if payload.get("target_id") is not None
                else None
            ),
            resolved=(
                dict(payload["resolved"])
                if isinstance(payload.get("resolved"), dict)
                else None
            ),
            custom_id=(
                str(payload["custom_id"])
                if payload.get("custom_id") is not None
                else None
            ),
            component_type=(
                payload["component_type"]
                if isinstance(payload.get("component_type"), (int, str))
                and not isinstance(payload.get("component_type"), bool)
                else None
            ),
            values=tuple(str(item) for item in payload.get("values", [])),
            components=tuple(
                dict(item)
                for item in payload.get("components", [])
                if isinstance(item, dict)
            ),
            source_component=(
                dict(payload["source_component"])
                if isinstance(payload.get("source_component"), dict)
                else None
            ),
            source_modal=(
                dict(payload["source_modal"])
                if isinstance(payload.get("source_modal"), dict)
                else None
            ),
        )

    def _lifecycle_request_kwargs(self) -> dict[str, Any]:
        """Return only the trusted install context present on the event."""

        result: dict[str, Any] = {"target": self.target}
        if self.installation_id is not None:
            result["installation_id"] = self.installation_id
        elif self.user_installation_id is not None:
            result["user_installation"] = True
        return result

    async def defer(self, *, ephemeral: bool = False) -> None:
        await self.client.request(
            "POST",
            f"/api/v1/bots/interactions/{self.id}/defer",
            target=self.target,
            json={"ephemeral": ephemeral},
        )

    async def defer_update(self) -> None:
        """Acknowledge a component now and update its exact source message later."""

        if self.type not in {"component", "modal_submit"}:
            raise ValueError("deferred message updates require a component interaction")
        if self.message_ref is None and self.response_id is None:
            raise ValueError("this interaction has no source message to update")
        await self.callback(6)

    async def update_message(
        self,
        *,
        content: str | None | MissingType = MISSING,
        embeds: Sequence[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
    ) -> Message | dict[str, Any]:
        """Respond by updating the exact message that created this interaction."""

        if self.type not in {"component", "modal_submit"}:
            raise ValueError("message updates require a component interaction")
        if self.message_ref is None and self.response_id is None:
            raise ValueError("this interaction has no source message to update")
        payload: dict[str, Any] = {}
        if not isinstance(content, MissingType):
            payload["content"] = content
        if not isinstance(embeds, MissingType):
            payload["embeds"] = [embed.to_dict() for embed in embeds]
        if not isinstance(view, MissingType) and not isinstance(
            components, MissingType
        ):
            raise ValueError("view and raw components are mutually exclusive")
        if not isinstance(view, MissingType):
            payload["components"] = view.to_components()
            payload["view_persistent"] = view.is_persistent
            if view.is_components_v2:
                payload["flags"] = (0 if isinstance(flags, MissingType) else flags) | (
                    1 << 15
                )
            if view.timeout is not None:
                payload["view_timeout_seconds"] = max(1, int(view.timeout))
            if view_version is None:
                view_version = self.view_version
        elif not isinstance(components, MissingType):
            rendered_components = list(components)
            payload["components"] = rendered_components
            if any(item.get("type") != 1 for item in rendered_components):
                payload["flags"] = (0 if isinstance(flags, MissingType) else flags) | (
                    1 << 15
                )
        elif view_version is not None:
            raise ValueError("view_version requires a components update")
        if view_version is not None:
            payload["view_version"] = view_version
        if not isinstance(attachment_ids, MissingType):
            payload["attachment_ids"] = [str(item) for item in attachment_ids]
        if not isinstance(flags, MissingType) and "flags" not in payload:
            payload["flags"] = flags
        if not isinstance(allowed_mentions, MissingType):
            payload["allowed_mentions"] = dict(allowed_mentions)
        if not isinstance(e2ee, MissingType):
            if any(
                not isinstance(value, MissingType)
                for value in (
                    content,
                    embeds,
                    view,
                    components,
                    allowed_mentions,
                )
            ):
                raise ValueError(
                    "an encrypted interaction update cannot contain plaintext or rich fields"
                )
            payload["e2ee"] = e2ee
        if not payload:
            raise ValueError("at least one message field is required")
        result = await self.client.interaction_callback(
            self.id,
            7,
            payload,
            **self._lifecycle_request_kwargs(),
        )
        if result is None:
            raise RuntimeError("interaction message update returned no response")
        if not isinstance(view, MissingType):
            if not view.rows:
                if isinstance(result, Message):
                    self.client.remove_view(result.ref)
                elif result.get("id") is not None:
                    self.client.remove_response_view(
                        int(result["id"]), target=self.target
                    )
                return result
            timeout_path = f"/api/v1/bots/interactions/{self.id}/responses/@original"
            if isinstance(result, Message):
                self.client.add_view(
                    view,
                    message_id=result.ref,
                    target=self.target,
                    timeout_editor=self.client._view_timeout_editor(
                        timeout_path,
                        target=self.target,
                        view_version=result.view_version,
                    ),
                )
            elif result.get("id") is not None:
                self.client.add_view(
                    view,
                    response_id=int(result["id"]),
                    target=self.target,
                    timeout_editor=self.client._view_timeout_editor(
                        timeout_path,
                        target=self.target,
                        view_version=int(result.get("view_version", 0) or 0),
                    ),
                )
        return result

    async def edit_message(
        self,
        *,
        content: str | None | MissingType = MISSING,
        embeds: Sequence[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
    ) -> Message | dict[str, Any]:
        """Alias for :meth:`update_message`."""

        return await self.update_message(
            content=content,
            embeds=embeds,
            view=view,
            view_version=view_version,
            attachment_ids=attachment_ids,
            e2ee=e2ee,
            components=components,
            flags=flags,
            allowed_mentions=allowed_mentions,
        )

    async def upload_attachment(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        encryption_mode: Literal["plaintext", "e2ee"] = "plaintext",
        encryption_protocol: str | None = None,
        duration_secs: float | None = None,
        waveform: bytes | str | None = None,
    ) -> Attachment:
        """Stage a policy-matching attachment for one interaction response."""

        if (
            self.attachment_size_limit is not None
            and len(data) > self.attachment_size_limit
        ):
            raise ValueError(
                "attachment exceeds this interaction's attachment_size_limit"
            )
        return await self.client.upload_interaction_attachment(
            self.id,
            data,
            filename=filename,
            content_type=content_type,
            target=self.target,
            encryption_mode=encryption_mode,
            encryption_protocol=encryption_protocol,
            duration_secs=duration_secs,
            waveform=waveform,
        )

    @property
    def input_attachments(self) -> tuple[Attachment, ...]:
        """Authority-resolved files supplied to this command invocation."""

        resolved = self.resolved or {}
        raw_attachments = resolved.get("attachments")
        if not isinstance(raw_attachments, dict):
            return ()
        return tuple(
            Attachment.from_payload(self.client, self.target, raw)
            for raw in raw_attachments.values()
            if isinstance(raw, dict)
        )

    async def fetch_input_attachment(self, attachment: EntityRef) -> Attachment:
        return await self.client.fetch_interaction_input_attachment(
            self.id,
            attachment,
            target=self.target,
        )

    async def read_input_attachment(
        self,
        attachment: EntityRef,
        *,
        variant: str = "original",
        max_bytes: int | None = None,
    ) -> bytes:
        return await self.client.download_interaction_input_attachment(
            self.id,
            attachment,
            variant=variant,
            target=self.target,
            max_bytes=max_bytes,
        )

    async def respond(
        self,
        content: str | None = None,
        *,
        e2ee: dict[str, Any] | None = None,
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        ephemeral: bool = False,
        attachment_ids: Sequence[int] = (),
        tts: bool = False,
        allowed_mentions: Mapping[str, object] | None = None,
        voice_message: bool = False,
        flags: int = 0,
        components: Sequence[dict[str, Any]] | None = None,
    ) -> Message | dict[str, Any]:
        if view is not None and components is not None:
            raise ValueError("view and raw components are mutually exclusive")
        voice_message = voice_message or bool(flags & (1 << 13))
        if voice_message and (
            tts
            or content is not None
            or embeds
            or view is not None
            or poll is not None
            or components
            or len(attachment_ids) != 1
        ):
            raise ValueError(
                "a voice response requires one audio attachment and no text or rich content"
            )
        if components is not None and any(item.get("type") != 1 for item in components):
            flags |= 1 << 15
        if view is not None and view.is_components_v2:
            flags |= 1 << 15
        if voice_message:
            flags |= 1 << 13
        payload: dict[str, Any] = {
            "content": content,
            "embeds": [embed.to_dict() for embed in embeds],
            "attachment_ids": [str(item) for item in attachment_ids],
        }
        if tts:
            payload["tts"] = True
        if voice_message:
            payload["voice_message"] = True
        effective_flags = flags | (64 if ephemeral else 0)
        if effective_flags:
            payload["flags"] = effective_flags
        if allowed_mentions is not None:
            payload["allowed_mentions"] = dict(allowed_mentions)
        if components is not None:
            payload["components"] = list(components)
        if e2ee is not None:
            if (
                content is not None
                or embeds
                or view is not None
                or poll is not None
                or components is not None
                or allowed_mentions is not None
            ):
                raise ValueError(
                    "an encrypted interaction response cannot contain rich plaintext fields"
                )
            payload = {
                "content": None,
                "e2ee": e2ee,
                "attachment_ids": [str(item) for item in attachment_ids],
            }
            if tts:
                payload["tts"] = True
            if voice_message:
                payload["voice_message"] = True
            if effective_flags:
                payload["flags"] = effective_flags
        if view is not None:
            payload["components"] = view.to_components()
            payload["view_persistent"] = view.is_persistent
            if view.timeout is not None:
                payload["view_timeout_seconds"] = max(1, int(view.timeout))
        if poll is not None:
            payload["poll"] = poll.to_dict()
        result = await self.client.interaction_callback(
            self.id,
            4,
            payload,
            **self._lifecycle_request_kwargs(),
        )
        if result is None:
            raise RuntimeError("interaction message callback returned no response")
        if view is not None and view.rows:
            if isinstance(result, Message):
                self.client.add_view(
                    view,
                    message_id=result.ref,
                    target=self.target,
                    timeout_editor=self.client._view_timeout_editor(
                        f"/api/v1/bots/interactions/{self.id}/responses/@original",
                        target=self.target,
                        view_version=result.view_version,
                    ),
                )
            elif result.get("id") is not None:
                self.client.add_view(
                    view,
                    response_id=int(result["id"]),
                    target=self.target,
                    timeout_editor=self.client._view_timeout_editor(
                        f"/api/v1/bots/interactions/{self.id}/responses/@original",
                        target=self.target,
                        view_version=int(result.get("view_version", 0) or 0),
                    ),
                )
        return result

    async def callback(self, type: int, data: dict[str, Any] | None = None) -> Any:
        return await self.client.interaction_callback(
            self.id,
            type,
            data or {},
            **self._lifecycle_request_kwargs(),
        )

    async def autocomplete(self, choices: list[dict[str, Any]]) -> None:
        await self.callback(8, {"choices": choices})

    async def send_modal(self, modal: Any) -> None:
        await self.callback(9, modal.to_dict())

    async def original_response(self) -> Message | dict[str, Any]:
        return await self.client.fetch_original_interaction_response(
            self.id,
            **self._lifecycle_request_kwargs(),
        )

    async def edit_original_response(
        self,
        *,
        content: str | None | MissingType = MISSING,
        embeds: Sequence[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        poll: Poll | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
    ) -> Message | dict[str, Any]:
        rendered_embeds: list[Embed] | MissingType = (
            embeds if isinstance(embeds, MissingType) else list(embeds)
        )
        return await self.client.edit_original_interaction_response(
            self.id,
            content=content,
            embeds=rendered_embeds,
            view=view,
            view_version=view_version,
            attachment_ids=attachment_ids,
            poll=poll,
            e2ee=e2ee,
            components=components,
            flags=flags,
            allowed_mentions=allowed_mentions,
            **self._lifecycle_request_kwargs(),
        )

    async def end_original_poll(self) -> Message | dict[str, Any]:
        """End the private poll attached to this interaction's original response."""

        return await self.client.finalize_interaction_poll(
            self.id,
            **self._lifecycle_request_kwargs(),
        )

    async def end_followup_poll(self, followup_id: int) -> Message | dict[str, Any]:
        """End a private poll attached to one of this interaction's follow-ups."""

        return await self.client.finalize_interaction_poll(
            self.id,
            response_id=followup_id,
            **self._lifecycle_request_kwargs(),
        )

    async def delete_original_response(self) -> None:
        await self.client.delete_original_interaction_response(
            self.id, target=self.target
        )

    async def send_followup(
        self,
        content: str | None = None,
        *,
        embeds: Sequence[Embed] = (),
        view: View | None = None,
        poll: Poll | None = None,
        ephemeral: bool = False,
        attachment_ids: Sequence[int] = (),
        e2ee: dict[str, Any] | None = None,
        tts: bool = False,
        allowed_mentions: Mapping[str, object] | None = None,
        voice_message: bool = False,
        flags: int = 0,
        components: Sequence[dict[str, Any]] | None = None,
    ) -> Message | dict[str, Any]:
        return await self.client.create_interaction_followup(
            self.id,
            content,
            embeds=list(embeds),
            view=view,
            poll=poll,
            ephemeral=ephemeral,
            attachment_ids=attachment_ids,
            e2ee=e2ee,
            tts=tts,
            allowed_mentions=allowed_mentions,
            voice_message=voice_message,
            flags=flags,
            components=components,
            **self._lifecycle_request_kwargs(),
        )

    async def fetch_followup(self, followup_id: int) -> Message | dict[str, Any]:
        return await self.client.fetch_interaction_followup(
            self.id,
            followup_id,
            **self._lifecycle_request_kwargs(),
        )

    async def edit_followup(
        self,
        followup_id: int,
        *,
        content: str | None | MissingType = MISSING,
        embeds: Sequence[Embed] | MissingType = MISSING,
        view: View | MissingType = MISSING,
        view_version: int | None = None,
        attachment_ids: Sequence[int] | MissingType = MISSING,
        e2ee: dict[str, Any] | MissingType = MISSING,
        components: Sequence[dict[str, Any]] | MissingType = MISSING,
        flags: int | MissingType = MISSING,
        allowed_mentions: Mapping[str, object] | MissingType = MISSING,
    ) -> Message | dict[str, Any]:
        rendered_embeds: list[Embed] | MissingType = (
            embeds if isinstance(embeds, MissingType) else list(embeds)
        )
        return await self.client.edit_interaction_followup(
            self.id,
            followup_id,
            content=content,
            embeds=rendered_embeds,
            view=view,
            view_version=view_version,
            attachment_ids=attachment_ids,
            e2ee=e2ee,
            components=components,
            flags=flags,
            allowed_mentions=allowed_mentions,
            **self._lifecycle_request_kwargs(),
        )

    async def delete_followup(self, followup_id: int) -> None:
        await self.client.delete_interaction_followup(
            self.id, followup_id, target=self.target
        )


@dataclass(frozen=True, slots=True)
class ReadyEvent:
    target: str
    application_ref: EntityRef
    worker_id: int
    installations: tuple[dict[str, Any], ...]
    intents: tuple[str, ...] = ()
    user_installations: tuple[dict[str, Any], ...] = ()
    dm_capabilities: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PendingDMOpen:
    """A durable cross-instance DM open accepted for asynchronous delivery."""

    target: str
    operation_id: str
    pair_key: str
    installation_ref: EntityRef
    installation_type: Literal["guild", "user"]
    status: Literal["queued"] = "queued"

    @property
    def installation_id(self) -> int:
        """Compatibility view of the qualified authority-owned identity."""

        return self.installation_ref.id


@dataclass(frozen=True, slots=True)
class DMOpenRejectedEvent:
    """A correlated terminal failure for a previously queued DM open."""

    target: str
    pair_key: str
    code: str
    authority_domain: str


@dataclass(frozen=True, slots=True)
class MessageDeleteEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef


@dataclass(frozen=True, slots=True)
class MessageBulkDeleteEvent:
    target: str
    message_refs: tuple[EntityRef, ...]
    channel_ref: EntityRef
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class ReactionEmoji:
    name: str
    ref: EntityRef | None = None
    animated: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ReactionEmoji:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("reaction emoji name is invalid")
        return cls(
            name=name,
            ref=_optional_ref(payload, "id", "origin_domain"),
            animated=_strict_payload_bool(payload, "animated", default=False),
        )

    @property
    def token(self) -> str:
        if self.ref is None:
            return self.name
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.ref.id}@{self.ref.domain}>"


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    user_ref: EntityRef
    emoji: str
    guild_ref: EntityRef | None = None
    emoji_details: ReactionEmoji | None = None
    message_author_ref: EntityRef | None = None
    burst: bool = False
    burst_colors: tuple[str, ...] = ()
    reaction_type: int = 0


@dataclass(frozen=True, slots=True)
class ReactionClearEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    guild_ref: EntityRef | None = None
    emoji: str | None = None
    emoji_details: ReactionEmoji | None = None


@dataclass(frozen=True, slots=True)
class PollVoteEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    user_ref: EntityRef
    answer_id: int
    added: bool
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class PinEvent:
    target: str
    message_ref: EntityRef
    channel_ref: EntityRef
    pinned: bool


@dataclass(frozen=True, slots=True)
class ChannelPinsUpdateEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef | None
    last_pin_at: datetime | None
    message_ref: EntityRef | None = None
    pinned: bool | None = None


@dataclass(frozen=True, slots=True)
class VoiceChannelInfo:
    channel_ref: EntityRef
    status: str | None = None
    voice_start_time: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelInfoEvent:
    target: str
    guild_ref: EntityRef
    channels: tuple[VoiceChannelInfo, ...]
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApplicationCommandPermission:
    target_ref: EntityRef
    type: Literal["role", "user", "channel"]
    permission: bool


@dataclass(frozen=True, slots=True)
class ApplicationCommandPermissions:
    ref: EntityRef
    application_ref: EntityRef
    guild_ref: EntityRef
    command_ref: EntityRef | None
    application_name: str
    synced: bool
    permissions: tuple[ApplicationCommandPermission, ...]
    raw: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        target: str,
        expected_application_ref: EntityRef,
        expected_guild_ref: EntityRef,
    ) -> ApplicationCommandPermissions:
        if not isinstance(payload, dict):
            raise ValueError("command permission response must be an object")
        try:
            application_ref = EntityRef.parse(payload["application_ref"])
            guild_ref = EntityRef.parse(payload["guild_ref"])
            scope_ref = EntityRef.parse(payload["id"])
            raw_command_ref = payload.get("command_ref")
            command_ref = (
                EntityRef.parse(raw_command_ref)
                if isinstance(raw_command_ref, str)
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "command permission response has invalid identity"
            ) from exc
        application_name = payload.get("application_name")
        synced = payload.get("synced")
        raw_permissions = payload.get("permissions")
        raw_command = payload.get("command")
        if (
            application_ref.domain is None
            or guild_ref.domain is None
            or scope_ref.domain is None
            or application_ref != expected_application_ref
            or guild_ref != expected_guild_ref
            or urlsplit(target).hostname != guild_ref.domain
            or payload.get("application_id") != str(application_ref.id)
            or payload.get("application_domain") != application_ref.domain
            or payload.get("guild_id") != str(guild_ref.id)
            or payload.get("guild_domain") != guild_ref.domain
            or not isinstance(application_name, str)
            or not application_name
            or type(synced) is not bool
            or not isinstance(raw_permissions, list)
            or len(raw_permissions) > 100
        ):
            raise ValueError("command permission response changed resource authority")
        if command_ref is None:
            if (
                raw_command_ref is not None
                or raw_command is not None
                or scope_ref != application_ref
            ):
                raise ValueError("application command permission scope is inconsistent")
        else:
            if (
                command_ref.domain != application_ref.domain
                or scope_ref != command_ref
                or not isinstance(raw_command, dict)
                or raw_command.get("ref") != str(command_ref)
                or raw_command.get("id") != str(command_ref.id)
                or raw_command.get("origin_domain") != command_ref.domain
            ):
                raise ValueError("command permission scope changed command identity")
        permissions: list[ApplicationCommandPermission] = []
        seen: set[tuple[str, EntityRef]] = set()
        for item in raw_permissions:
            if not isinstance(item, dict):
                raise ValueError("command permission response has an invalid entry")
            raw_ref = item.get("id")
            entry_type = item.get("type")
            permission = item.get("permission")
            try:
                target_ref = (
                    EntityRef.parse(raw_ref) if isinstance(raw_ref, str) else None
                )
            except ValueError as exc:
                raise ValueError(
                    "command permission response has an invalid entry"
                ) from exc
            if (
                target_ref is None
                or target_ref.domain is None
                or entry_type not in {"role", "user", "channel"}
                or type(permission) is not bool
                or entry_type in {"role", "channel"}
                and target_ref.domain != guild_ref.domain
            ):
                raise ValueError("command permission response has an invalid entry")
            key = str(entry_type), target_ref
            if key in seen:
                raise ValueError("command permission response repeated an entry")
            seen.add(key)
            permissions.append(
                ApplicationCommandPermission(
                    target_ref=target_ref,
                    type=cast(Literal["role", "user", "channel"], entry_type),
                    permission=permission,
                )
            )
        return cls(
            ref=scope_ref,
            application_ref=application_ref,
            guild_ref=guild_ref,
            command_ref=command_ref,
            application_name=application_name,
            synced=synced,
            permissions=tuple(permissions),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class ApplicationCommandPermissionsUpdateEvent:
    target: str
    application_ref: EntityRef
    guild_ref: EntityRef
    command_ref: EntityRef | None
    permissions: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MemberRemoveEvent:
    target: str
    guild_ref: EntityRef
    user_ref: EntityRef


@dataclass(frozen=True, slots=True)
class GuildDeleteEvent:
    target: str
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class ChannelDeleteEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class TrackerBoardUpdateEvent:
    target: str
    channel_ref: EntityRef
    key_prefix: str
    next_task_number: int
    version: str | None = None
    full_refresh: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TrackerLaneDeleteEvent:
    target: str
    channel_ref: EntityRef
    lane_ref: EntityRef
    board_version: str | None = None


@dataclass(frozen=True, slots=True)
class TrackerTaskDeleteEvent:
    target: str
    channel_ref: EntityRef
    task_ref: EntityRef
    board_version: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadDeleteEvent:
    target: str
    thread_ref: EntityRef
    guild_ref: EntityRef
    parent_ref: EntityRef
    type: int


@dataclass(frozen=True, slots=True)
class ThreadListSyncEvent:
    target: str
    guild_ref: EntityRef | None
    channel_refs: tuple[EntityRef, ...] | None
    threads: tuple[Channel, ...]
    members: tuple[ThreadMember, ...]


@dataclass(frozen=True, slots=True)
class ThreadMemberUpdateEvent:
    target: str
    member: ThreadMember


@dataclass(frozen=True, slots=True)
class ThreadMembersUpdateEvent:
    target: str
    thread_ref: EntityRef
    guild_ref: EntityRef
    member_count: int
    added_members: tuple[ThreadMember, ...]
    removed_member_refs: tuple[EntityRef, ...]


@dataclass(frozen=True, slots=True)
class RoleDeleteEvent:
    target: str
    role_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class EmojiDeleteEvent:
    target: str
    emoji_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class EmojisUpdateEvent:
    target: str
    guild_ref: EntityRef
    emojis: tuple[Emoji, ...]


@dataclass(frozen=True, slots=True)
class StickerDeleteEvent:
    target: str
    sticker_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class StickersUpdateEvent:
    target: str
    guild_ref: EntityRef
    stickers: tuple[Sticker, ...]


@dataclass(frozen=True, slots=True)
class SoundboardSoundDeleteEvent:
    target: str
    sound_ref: EntityRef
    guild_ref: EntityRef


@dataclass(frozen=True, slots=True)
class SoundboardSoundsUpdateEvent:
    target: str
    guild_ref: EntityRef
    sounds: tuple[SoundboardSound, ...]


@dataclass(frozen=True, slots=True)
class ScheduledEventUserEvent:
    target: str
    guild_ref: EntityRef
    event_ref: EntityRef
    user_ref: EntityRef
    added: bool


@dataclass(frozen=True, slots=True)
class TypingEvent:
    target: str
    channel_ref: EntityRef
    user_ref: EntityRef
    timestamp: int


@dataclass(frozen=True, slots=True)
class PresenceEvent:
    target: str
    user_ref: EntityRef
    status: str
    custom_status: str | None
    activities: tuple[dict[str, object], ...]
    since: int | None
    afk: bool
    client_status: dict[str, str]
    raw: dict[str, Any]
    guild_ref: EntityRef | None = None


@dataclass(frozen=True, slots=True)
class GuildMembersChunkEvent:
    target: str
    guild_ref: EntityRef
    members: tuple[Member, ...]
    presences: tuple[dict[str, object], ...]
    chunk_index: int
    chunk_count: int
    not_found: tuple[str, ...]
    nonce: str | None


@dataclass(frozen=True, slots=True)
class VoiceOccupancy:
    channel_ref: EntityRef
    participants: tuple[dict[str, Any], ...]
    generated_at: int | None = None


@dataclass(slots=True)
class Call:
    client: Client
    target: str
    ref: EntityRef
    channel_ref: EntityRef
    room: str
    state: Literal["ringing", "active", "ended"]
    caller_ref: EntityRef
    participant_refs: tuple[EntityRef, ...]
    created_at: int
    ended_at: int | None = None
    dm_capability_id: str | None = None
    dm_capability_revision: int | None = None
    installation_ref: EntityRef | None = None
    installation_type: Literal["guild", "user"] | None = None

    @classmethod
    def from_payload(
        cls,
        client: Client,
        target: str,
        payload: dict[str, Any],
        *,
        fallback_dm_capability_id: str | None = None,
    ) -> Call:
        state = payload.get("state")
        if state not in {"ringing", "active", "ended"}:
            raise ValueError("call response has an invalid state")
        raw_participants = payload.get("participants")
        if (
            not isinstance(raw_participants, list)
            or not 2 <= len(raw_participants) <= 10
            or any(not isinstance(item, str) for item in raw_participants)
        ):
            raise ValueError("call response has invalid participants")
        participants = tuple(EntityRef.parse(item) for item in raw_participants)
        if len(participants) != len(set(participants)):
            raise ValueError("call response repeated a participant")
        caller = EntityRef.parse(payload["caller"])
        if caller not in participants:
            raise ValueError("call response omitted its caller from participants")
        ref = EntityRef.from_wire(payload["id"], payload["authority_domain"])
        channel_ref = EntityRef.from_wire(
            payload["channel_id"], payload["channel_domain"]
        )
        if ref.domain != channel_ref.domain or ref.domain != urlsplit(target).hostname:
            raise ValueError(
                "call response authority does not match its target channel"
            )
        room = payload.get("room")
        if room != f"d.{channel_ref.id}.{ref.id}":
            raise ValueError("call response room does not match its identity")
        created_at = _strict_payload_int(payload.get("created_at"), "call created_at")
        ended_at = (
            _strict_payload_int(payload.get("ended_at"), "call ended_at")
            if payload.get("ended_at") is not None
            else None
        )
        if state == "ended":
            if ended_at is None or ended_at < created_at:
                raise ValueError(
                    "ended call response has an invalid terminal timestamp"
                )
        elif ended_at is not None:
            raise ValueError(
                "active call response unexpectedly has an ended_at timestamp"
            )
        capability_id = payload.get("bot_dm_capability_id", fallback_dm_capability_id)
        capability_revision = payload.get("bot_dm_capability_revision")
        installation_ref = payload.get("bot_installation_ref")
        installation_type = payload.get("bot_installation_type")
        if capability_id is not None and (
            not isinstance(capability_id, str)
            or re.fullmatch(r"kbdg_[A-Za-z0-9_-]{43}", capability_id) is None
        ):
            raise ValueError("call response has an invalid DM capability")
        if (
            fallback_dm_capability_id is not None
            and capability_id != fallback_dm_capability_id
        ):
            raise ValueError("call response changed its requested DM capability")
        if capability_revision is not None and (
            not isinstance(capability_revision, str)
            or not capability_revision.isascii()
            or not capability_revision.isdecimal()
            or int(capability_revision) < 1
        ):
            raise ValueError("call response has an invalid DM capability revision")
        if installation_type is not None and installation_type not in {"guild", "user"}:
            raise ValueError("call response has an invalid installation type")
        capability_fields = (
            capability_id,
            capability_revision,
            installation_ref,
            installation_type,
        )
        if any(item is not None for item in capability_fields) and not all(
            item is not None for item in capability_fields
        ):
            raise ValueError("call response has incomplete DM capability lineage")
        parsed_installation_ref = (
            EntityRef.parse(installation_ref)
            if isinstance(installation_ref, str)
            else None
        )
        return cls(
            client=client,
            target=target,
            ref=ref,
            channel_ref=channel_ref,
            room=cast(str, room),
            state=cast(Literal["ringing", "active", "ended"], state),
            caller_ref=caller,
            participant_refs=participants,
            created_at=created_at,
            ended_at=ended_at,
            dm_capability_id=capability_id,
            dm_capability_revision=(
                int(capability_revision) if capability_revision is not None else None
            ),
            installation_ref=parsed_installation_ref,
            installation_type=cast(
                Literal["guild", "user"] | None,
                installation_type,
            ),
        )

    async def act(self, action: Literal["accept", "decline", "end"]) -> Call:
        updated = await self.client.act_call(
            self.channel_ref,
            self.ref,
            action,
            target=self.target,
            dm_capability_id=self.dm_capability_id,
        )
        self.state = updated.state
        self.ended_at = updated.ended_at
        return self


@dataclass(frozen=True, slots=True)
class ActiveCall:
    call: Call | None
    joined: bool = False


@dataclass(slots=True)
class StageInstance:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    channel_ref: EntityRef
    topic: str
    privacy_level: int = 2
    discoverable_disabled: bool = True
    scheduled_event_ref: EntityRef | None = None

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> StageInstance:
        privacy_level = _strict_payload_int(
            payload.get("privacy_level", 2), "Stage privacy level"
        )
        if privacy_level != 2:
            raise ValueError("Stage privacy level is invalid")
        topic = _strict_payload_string(payload.get("topic"), "Stage topic")
        return cls(
            client=client,
            target=target,
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            channel_ref=EntityRef.from_wire(
                payload["channel_id"], payload["channel_domain"]
            ),
            topic=topic,
            privacy_level=privacy_level,
            discoverable_disabled=_strict_payload_bool(
                payload, "discoverable_disabled", default=True
            ),
            scheduled_event_ref=_optional_ref(
                payload,
                "guild_scheduled_event_id",
                "guild_scheduled_event_domain",
            ),
        )

    async def edit(
        self,
        *,
        topic: str | MissingType = MISSING,
        privacy_level: Literal[2] | MissingType = MISSING,
        reason: str | None = None,
    ) -> StageInstance:
        return await self.client.edit_stage_instance(
            self.channel_ref,
            target=self.target,
            topic=topic,
            privacy_level=privacy_level,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_stage_instance(
            self.channel_ref,
            target=self.target,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class StageVoiceState:
    guild_ref: EntityRef
    channel_ref: EntityRef
    user_ref: EntityRef
    session_id: str
    suppress: bool
    self_mute: bool
    self_deaf: bool
    server_mute: bool
    server_deaf: bool
    request_to_speak_at: datetime | None
    can_speak: bool
    can_stream: bool
    can_priority_speak: bool
    joined_at: int | None
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StageVoiceState:
        return cls(
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            channel_ref=EntityRef.from_wire(
                payload["channel_id"], payload["channel_domain"]
            ),
            user_ref=EntityRef.from_wire(payload["user_id"], payload["user_domain"]),
            session_id=_strict_payload_string(
                payload.get("session_id"), "Stage session ID"
            ),
            suppress=_strict_payload_bool(
                payload,
                "suppress",
                default=False,
                aliases=("suppressed",),
            ),
            self_mute=_strict_payload_bool(payload, "self_mute", default=False),
            self_deaf=_strict_payload_bool(payload, "self_deaf", default=False),
            server_mute=_strict_payload_bool(
                payload,
                "server_mute",
                default=False,
                aliases=("mute",),
            ),
            server_deaf=_strict_payload_bool(
                payload,
                "server_deaf",
                default=False,
                aliases=("deaf",),
            ),
            request_to_speak_at=_datetime(payload.get("request_to_speak_timestamp")),
            can_speak=_strict_payload_bool(payload, "can_speak", default=False),
            can_stream=_strict_payload_bool(payload, "can_stream", default=False),
            can_priority_speak=_strict_payload_bool(
                payload, "can_priority_speak", default=False
            ),
            joined_at=(
                _strict_payload_int(payload["joined_at"], "Stage joined_at")
                if payload.get("joined_at") is not None
                else None
            ),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class VoiceStateEvent:
    target: str
    guild_ref: EntityRef | None
    channel_ref: EntityRef | None
    user_ref: EntityRef | None
    connected: bool | None
    self_mute: bool | None
    self_deaf: bool | None
    server_mute: bool | None
    server_deaf: bool | None
    participants: tuple[dict[str, Any], ...]
    heartbeat: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VoiceChannelEffectEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef
    user_ref: EntityRef
    emoji: str | None
    sound_id: int | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VoiceChannelStatusEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef
    status: str | None


@dataclass(frozen=True, slots=True)
class VoiceChannelStartTimeEvent:
    target: str
    channel_ref: EntityRef
    guild_ref: EntityRef
    voice_start_time: int | None


@dataclass(frozen=True, slots=True)
class RawEvent:
    target: str
    type: str
    data: dict[str, Any]
    topic: str | None = None
    sequence: int = 0
