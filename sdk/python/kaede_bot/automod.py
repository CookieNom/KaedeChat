from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .models import MISSING, MissingType
from .refs import EntityRef
from .wire import (
    strict_payload_bool,
    strict_payload_datetime,
    strict_payload_int,
    strict_payload_string,
)

if TYPE_CHECKING:
    from .client import Client

AutoModEventType = Literal["message_send", "member_update"]
AutoModTriggerType = Literal[
    "keyword", "spam", "keyword_preset", "mention_spam", "member_profile"
]
AutoModPresetType = Literal["profanity", "sexual_content", "slurs"]
AutoModActionType = Literal[
    "block_message",
    "send_alert_message",
    "timeout",
    "block_member_interaction",
]

MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60
_SAFE_REGEX_FORBIDDEN = re.compile(r"\\[1-9]|\(\?<?[=!]|\(\?>|\(\?P|\{\d+,\d*}\s*[+*?]")


def _unique(values: Sequence[object], *, name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


@dataclass(frozen=True, slots=True)
class AutoModTriggerMetadata:
    keyword_filter: Sequence[str] = ()
    regex_patterns: Sequence[str] = ()
    presets: Sequence[AutoModPresetType] = ()
    allow_list: Sequence[str] = ()
    mention_total_limit: int | None = None
    mention_raid_protection_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "keyword_filter", tuple(self.keyword_filter))
        object.__setattr__(self, "regex_patterns", tuple(self.regex_patterns))
        object.__setattr__(self, "presets", tuple(self.presets))
        object.__setattr__(self, "allow_list", tuple(self.allow_list))
        if len(self.keyword_filter) > 1_000:
            raise ValueError("keyword_filter cannot contain more than 1000 entries")
        if len(self.regex_patterns) > 10:
            raise ValueError("regex_patterns cannot contain more than 10 entries")
        if len(self.presets) > 3:
            raise ValueError("presets cannot contain more than 3 entries")
        if len(self.allow_list) > 1_000:
            raise ValueError("allow_list cannot contain more than 1000 entries")
        _unique(self.keyword_filter, name="keyword_filter entries")
        _unique(self.regex_patterns, name="regex_patterns")
        _unique(self.presets, name="presets")
        _unique(self.allow_list, name="allow_list entries")
        for field_name, values in (
            ("keyword_filter", self.keyword_filter),
            ("allow_list", self.allow_list),
        ):
            for value in values:
                if not value.strip():
                    raise ValueError(f"{field_name} entries must not be blank")
                if not value.strip().strip("*"):
                    raise ValueError(
                        f"{field_name} entries must contain a character other than a wildcard"
                    )
                if len(value) > 60:
                    raise ValueError(
                        f"{field_name} entries cannot exceed 60 characters"
                    )
        for pattern in self.regex_patterns:
            if not pattern or len(pattern) > 260:
                raise ValueError(
                    "regex patterns must contain between 1 and 260 characters"
                )
            if _SAFE_REGEX_FORBIDDEN.search(pattern):
                raise ValueError(
                    "regex patterns cannot use backreferences, lookarounds, atomic "
                    "groups, named groups, or nested counted quantifiers"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern: {exc.msg}") from exc
        allowed_presets = {"profanity", "sexual_content", "slurs"}
        if not set(self.presets) <= allowed_presets:
            raise ValueError("unsupported AutoMod keyword preset")
        if self.mention_total_limit is not None:
            if isinstance(self.mention_total_limit, bool) or not isinstance(
                self.mention_total_limit, int
            ):
                raise TypeError("mention_total_limit must be an integer")
            if not 1 <= self.mention_total_limit <= 50:
                raise ValueError("mention_total_limit must be between 1 and 50")
        if not isinstance(self.mention_raid_protection_enabled, bool):
            raise TypeError("mention_raid_protection_enabled must be a boolean")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutoModTriggerMetadata:
        allowed = {
            "keyword_filter",
            "regex_patterns",
            "presets",
            "allow_list",
            "mention_total_limit",
            "mention_raid_protection_enabled",
        }
        if not set(payload) <= allowed:
            raise ValueError("AutoMod trigger metadata contains unknown fields")

        def string_list(key: str) -> tuple[str, ...]:
            raw = payload.get(key, [])
            if not isinstance(raw, list) or any(
                not isinstance(item, str) for item in raw
            ):
                raise ValueError(f"AutoMod {key} must be a list of strings")
            return tuple(raw)

        raw_mention_limit = payload.get("mention_total_limit")
        mention_limit = (
            strict_payload_int(
                raw_mention_limit,
                "AutoMod mention_total_limit",
                minimum=1,
                maximum=50,
            )
            if raw_mention_limit is not None
            else None
        )
        return cls(
            keyword_filter=string_list("keyword_filter"),
            regex_patterns=string_list("regex_patterns"),
            presets=string_list("presets"),  # type: ignore[arg-type]
            allow_list=string_list("allow_list"),
            mention_total_limit=mention_limit,
            mention_raid_protection_enabled=strict_payload_bool(
                payload,
                "mention_raid_protection_enabled",
                default=False,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "keyword_filter": list(self.keyword_filter),
            "regex_patterns": list(self.regex_patterns),
            "presets": list(self.presets),
            "allow_list": list(self.allow_list),
            "mention_total_limit": self.mention_total_limit,
            "mention_raid_protection_enabled": self.mention_raid_protection_enabled,
        }


@dataclass(frozen=True, slots=True)
class AutoModAction:
    type: AutoModActionType
    custom_message: str | None = None
    channel_ref: EntityRef | None = None
    duration_seconds: int | None = None

    def __post_init__(self) -> None:
        allowed = {
            "block_message",
            "send_alert_message",
            "timeout",
            "block_member_interaction",
        }
        if self.type not in allowed:
            raise ValueError("unsupported AutoMod action type")
        if self.custom_message is not None:
            if not self.custom_message.strip():
                raise ValueError("custom_message must not be blank")
            if len(self.custom_message) > 150:
                raise ValueError("custom_message cannot exceed 150 characters")
        if self.type == "send_alert_message":
            if (
                self.channel_ref is None
                or self.duration_seconds is not None
                or self.custom_message is not None
            ):
                raise ValueError("send_alert_message requires only channel_ref")
        elif self.type == "timeout":
            if self.duration_seconds is None or self.channel_ref is not None:
                raise ValueError("timeout requires only duration_seconds")
            if self.custom_message is not None:
                raise ValueError("timeout does not accept custom_message")
        elif self.type == "block_message":
            if self.channel_ref is not None or self.duration_seconds is not None:
                raise ValueError("block_message accepts only custom_message metadata")
        elif (
            self.channel_ref is not None
            or self.duration_seconds is not None
            or self.custom_message is not None
        ):
            raise ValueError(f"{self.type} does not accept action metadata")
        if self.duration_seconds is not None:
            if isinstance(self.duration_seconds, bool) or not isinstance(
                self.duration_seconds, int
            ):
                raise TypeError("duration_seconds must be an integer")
            if not 1 <= self.duration_seconds <= MAX_TIMEOUT_SECONDS:
                raise ValueError(
                    f"duration_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
                )

    @classmethod
    def block_message(cls, custom_message: str | None = None) -> AutoModAction:
        return cls("block_message", custom_message=custom_message)

    @classmethod
    def send_alert_message(cls, channel: EntityRef) -> AutoModAction:
        return cls("send_alert_message", channel_ref=channel)

    @classmethod
    def timeout(cls, duration_seconds: int) -> AutoModAction:
        return cls("timeout", duration_seconds=duration_seconds)

    @classmethod
    def block_member_interaction(cls) -> AutoModAction:
        return cls("block_member_interaction")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutoModAction:
        if set(payload) != {"type", "metadata"} or not isinstance(
            payload.get("metadata"), dict
        ):
            raise ValueError("AutoMod action response is invalid")
        action_type = payload["type"]
        if action_type not in {
            "block_message",
            "send_alert_message",
            "timeout",
            "block_member_interaction",
        }:
            raise ValueError("unsupported AutoMod action type")
        metadata = payload["metadata"]
        allowed_metadata = {
            "block_message": {"custom_message"},
            "send_alert_message": {"channel_id"},
            "timeout": {"duration_seconds"},
            "block_member_interaction": set(),
        }[action_type]
        if not set(metadata) <= allowed_metadata:
            raise ValueError("AutoMod action metadata is invalid")
        custom_message = metadata.get("custom_message")
        if custom_message is not None and not isinstance(custom_message, str):
            raise ValueError("AutoMod custom message is invalid")
        channel = metadata.get("channel_id")
        if channel is not None and not isinstance(channel, str):
            raise ValueError("AutoMod alert channel is invalid")
        duration = metadata.get("duration_seconds")
        duration_seconds = (
            strict_payload_int(
                duration,
                "AutoMod timeout duration",
                minimum=1,
                maximum=MAX_TIMEOUT_SECONDS,
            )
            if duration is not None
            else None
        )
        return cls(
            type=action_type,
            custom_message=custom_message,
            channel_ref=(EntityRef.parse(channel) if channel is not None else None),
            duration_seconds=duration_seconds,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"type": self.type}
        if self.custom_message is not None:
            payload["custom_message"] = self.custom_message
        if self.channel_ref is not None:
            payload["channel_id"] = str(self.channel_ref)
        if self.duration_seconds is not None:
            payload["duration_seconds"] = self.duration_seconds
        return payload


def validate_actions(
    actions: Sequence[AutoModAction],
    *,
    trigger_type: AutoModTriggerType | None = None,
) -> None:
    """Validate action invariants shared by full rules and partial edits."""

    if not 1 <= len(actions) <= 3:
        raise ValueError("AutoMod rules require between 1 and 3 actions")
    if not all(isinstance(action, AutoModAction) for action in actions):
        raise TypeError("actions must contain AutoModAction instances")
    action_types = [action.type for action in actions]
    if len(action_types) != len(set(action_types)):
        raise ValueError("an AutoMod rule cannot contain duplicate action types")
    if trigger_type is None:
        return
    if "timeout" in action_types and trigger_type not in {"keyword", "mention_spam"}:
        raise ValueError("timeout actions support only keyword and mention-spam rules")
    if "block_member_interaction" in action_types and trigger_type != "member_profile":
        raise ValueError("block_member_interaction requires a member-profile rule")
    if trigger_type == "member_profile" and "block_message" in action_types:
        raise ValueError("member-profile rules cannot use block_message")


def validate_rule_configuration(
    event_type: AutoModEventType,
    trigger_type: AutoModTriggerType,
    trigger_metadata: AutoModTriggerMetadata,
    actions: Sequence[AutoModAction],
    exempt_roles: Sequence[EntityRef],
    exempt_channels: Sequence[EntityRef],
) -> None:
    if event_type not in {"message_send", "member_update"}:
        raise ValueError("unsupported AutoMod event type")
    if trigger_type not in {
        "keyword",
        "spam",
        "keyword_preset",
        "mention_spam",
        "member_profile",
    }:
        raise ValueError("unsupported AutoMod trigger type")
    if trigger_type in {"keyword", "member_profile"} and not (
        trigger_metadata.keyword_filter or trigger_metadata.regex_patterns
    ):
        raise ValueError(
            f"{'keyword' if trigger_type == 'keyword' else 'member-profile'} "
            "rules require keyword_filter or regex_patterns"
        )
    if trigger_type == "keyword_preset" and not trigger_metadata.presets:
        raise ValueError("keyword_preset rules require at least one preset")
    if trigger_type == "mention_spam" and trigger_metadata.mention_total_limit is None:
        raise ValueError("mention_spam rules require mention_total_limit")
    if event_type == "member_update" and trigger_type != "member_profile":
        raise ValueError("member_update supports only the member_profile trigger")
    if event_type == "message_send" and trigger_type == "member_profile":
        raise ValueError("member_profile requires the member_update event")
    if trigger_type in {"keyword", "member_profile"}:
        if len(trigger_metadata.allow_list) > 100:
            raise ValueError(
                "keyword and member-profile allow lists are limited to 100 entries"
            )
        if (
            trigger_metadata.presets
            or trigger_metadata.mention_total_limit is not None
            or trigger_metadata.mention_raid_protection_enabled
        ):
            raise ValueError(
                "keyword and member-profile rules contain incompatible trigger metadata"
            )
    elif trigger_type == "spam":
        if trigger_metadata != AutoModTriggerMetadata():
            raise ValueError("spam rules do not accept trigger metadata")
    elif trigger_type == "keyword_preset":
        if (
            trigger_metadata.keyword_filter
            or trigger_metadata.regex_patterns
            or trigger_metadata.mention_total_limit is not None
            or trigger_metadata.mention_raid_protection_enabled
        ):
            raise ValueError(
                "keyword preset rules contain incompatible trigger metadata"
            )
    elif (
        trigger_metadata.keyword_filter
        or trigger_metadata.regex_patterns
        or trigger_metadata.presets
        or trigger_metadata.allow_list
    ):
        raise ValueError("mention spam rules contain incompatible trigger metadata")
    validate_actions(actions, trigger_type=trigger_type)
    if len(exempt_roles) > 20:
        raise ValueError("an AutoMod rule can exempt at most 20 roles")
    if len(exempt_channels) > 50:
        raise ValueError("an AutoMod rule can exempt at most 50 channels")
    _unique(exempt_roles, name="exempt roles")
    _unique(exempt_channels, name="exempt channels")


@dataclass(slots=True)
class AutoModRule:
    client: Client
    target: str
    ref: EntityRef
    guild_ref: EntityRef
    name: str
    creator_ref: EntityRef
    event_type: AutoModEventType
    trigger_type: AutoModTriggerType
    trigger_metadata: AutoModTriggerMetadata
    actions: tuple[AutoModAction, ...]
    enabled: bool
    exempt_roles: tuple[EntityRef, ...]
    exempt_channels: tuple[EntityRef, ...]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_payload(
        cls, client: Client, target: str, payload: dict[str, Any]
    ) -> AutoModRule:
        required = {
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            "name",
            "creator_id",
            "creator_domain",
            "event_type",
            "trigger_type",
            "trigger_metadata",
            "actions",
            "enabled",
            "exempt_roles",
            "exempt_channels",
            "version",
            "created_at",
            "updated_at",
        }
        if not required <= payload.keys() or not set(payload) <= required | {"ref"}:
            raise ValueError("AutoMod rule response is invalid")
        metadata = payload["trigger_metadata"]
        raw_actions = payload["actions"]
        raw_roles = payload["exempt_roles"]
        raw_channels = payload["exempt_channels"]
        if (
            not isinstance(metadata, dict)
            or not isinstance(raw_actions, list)
            or any(not isinstance(item, dict) for item in raw_actions)
            or not isinstance(raw_roles, list)
            or any(not isinstance(item, str) for item in raw_roles)
            or not isinstance(raw_channels, list)
            or any(not isinstance(item, str) for item in raw_channels)
        ):
            raise ValueError("AutoMod rule collections are invalid")
        name = strict_payload_string(payload["name"], "AutoMod rule name")
        if not 1 <= len(name) <= 100 or name != name.strip():
            raise ValueError("AutoMod rule name is invalid")
        event_type = payload["event_type"]
        trigger_type = payload["trigger_type"]
        if event_type not in {"message_send", "member_update"}:
            raise ValueError("unsupported AutoMod event type")
        if trigger_type not in {
            "keyword",
            "spam",
            "keyword_preset",
            "mention_spam",
            "member_profile",
        }:
            raise ValueError("unsupported AutoMod trigger type")
        raw_version = strict_payload_int(
            payload["version"],
            "AutoMod rule version",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        created_at = strict_payload_datetime(
            payload["created_at"], "AutoMod rule created_at"
        )
        updated_at = strict_payload_datetime(
            payload["updated_at"], "AutoMod rule updated_at"
        )
        if updated_at < created_at:
            raise ValueError("AutoMod rule timestamps are invalid")
        ref = EntityRef.from_wire(payload["id"], payload["origin_domain"])
        if payload.get("ref") is not None and EntityRef.parse(payload["ref"]) != ref:
            raise ValueError("AutoMod rule reference aliases conflict")
        parsed_metadata = AutoModTriggerMetadata.from_payload(metadata)
        actions = tuple(AutoModAction.from_payload(item) for item in raw_actions)
        roles = tuple(EntityRef.parse(item) for item in raw_roles)
        channels = tuple(EntityRef.parse(item) for item in raw_channels)
        validate_rule_configuration(
            event_type,
            trigger_type,
            parsed_metadata,
            actions,
            roles,
            channels,
        )
        return cls(
            client=client,
            target=target,
            ref=ref,
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            name=name,
            creator_ref=EntityRef.from_wire(
                payload["creator_id"], payload["creator_domain"]
            ),
            event_type=event_type,
            trigger_type=trigger_type,
            trigger_metadata=parsed_metadata,
            actions=actions,
            enabled=strict_payload_bool(payload, "enabled", default=True),
            exempt_roles=roles,
            exempt_channels=channels,
            version=raw_version,
            created_at=created_at,
            updated_at=updated_at,
        )

    async def edit(
        self,
        *,
        name: str | MissingType = MISSING,
        event_type: AutoModEventType | MissingType = MISSING,
        trigger_metadata: AutoModTriggerMetadata | MissingType = MISSING,
        actions: Sequence[AutoModAction] | MissingType = MISSING,
        enabled: bool | MissingType = MISSING,
        exempt_roles: Sequence[EntityRef] | MissingType = MISSING,
        exempt_channels: Sequence[EntityRef] | MissingType = MISSING,
        reason: str | None = None,
    ) -> AutoModRule:
        return await self.client.edit_auto_mod_rule(
            self.guild_ref,
            self.ref.id,
            target=self.target,
            name=name,
            event_type=event_type,
            trigger_metadata=trigger_metadata,
            actions=actions,
            enabled=enabled,
            exempt_roles=exempt_roles,
            exempt_channels=exempt_channels,
            reason=reason,
        )

    async def delete(self, *, reason: str | None = None) -> None:
        await self.client.delete_auto_mod_rule(
            self.guild_ref, self.ref.id, target=self.target, reason=reason
        )


@dataclass(frozen=True, slots=True)
class AutoModExecutedAction:
    action: AutoModAction
    outcome: str

    @property
    def type(self) -> AutoModActionType:
        return self.action.type

    @property
    def metadata(self) -> dict[str, object]:
        payload = self.action.to_dict()
        payload.pop("type")
        return payload

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, outcome: object
    ) -> AutoModExecutedAction:
        if outcome not in {"blocked", "alerted", "failed", "timed_out"}:
            raise ValueError("AutoMod execution outcome is invalid")
        try:
            action = AutoModAction.from_payload(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("AutoMod execution action is invalid") from exc
        return cls(action=action, outcome=outcome)


@dataclass(frozen=True, slots=True)
class AutoModExecution:
    guild_ref: EntityRef
    channel_ref: EntityRef | None
    rule_ref: EntityRef
    user_ref: EntityRef
    rule_trigger_type: AutoModTriggerType
    action: AutoModExecutedAction
    content: str
    matched_keyword: str | None
    matched_content: str | None
    alert_system_message_ref: EntityRef | None
    content_digest: str | None

    @property
    def trigger_type(self) -> AutoModTriggerType:
        """Compatibility alias for the pre-parity field spelling."""

        return self.rule_trigger_type

    @property
    def actions(self) -> tuple[AutoModExecutedAction, ...]:
        """Compatibility view; Discord dispatches one executed action per event."""

        return (self.action,)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutoModExecution:
        required = {
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
            "rule_id",
            "rule_domain",
            "rule_trigger_type",
            "user_id",
            "user_domain",
            "action",
            "outcome",
            "content",
            "matched_keyword",
            "matched_content",
            "alert_system_message_id",
            "alert_system_message_domain",
            "content_digest",
        }
        if set(payload) != required:
            raise ValueError("AutoMod execution payload is invalid")

        def reference(prefix: str, *, optional: bool = False) -> EntityRef | None:
            raw_id = payload.get(f"{prefix}_id")
            raw_domain = payload.get(f"{prefix}_domain")
            if raw_id is None and raw_domain is None and optional:
                return None
            if (
                isinstance(raw_id, bool)
                or not isinstance(raw_id, str)
                or not isinstance(raw_domain, str)
            ):
                raise ValueError(f"AutoMod execution {prefix} reference is invalid")
            return EntityRef.parse(f"{raw_id}@{raw_domain}")

        raw_action = payload.get("action")
        if not isinstance(raw_action, dict):
            raise ValueError("AutoMod execution action is invalid")
        trigger_type = payload.get("rule_trigger_type")
        if trigger_type not in {
            "keyword",
            "spam",
            "keyword_preset",
            "mention_spam",
            "member_profile",
        }:
            raise ValueError("AutoMod execution trigger type is invalid")
        content = payload.get("content")
        matched_keyword = payload.get("matched_keyword")
        matched_content = payload.get("matched_content")
        digest = payload.get("content_digest")
        if not isinstance(content, str) or len(content) > 4_000 or "\x00" in content:
            raise ValueError("AutoMod execution content is invalid")
        if matched_keyword is not None and (
            not isinstance(matched_keyword, str)
            or len(matched_keyword) > 260
            or "\x00" in matched_keyword
        ):
            raise ValueError("AutoMod execution matched keyword is invalid")
        if matched_content is not None and (
            not isinstance(matched_content, str)
            or len(matched_content) > 4_000
            or "\x00" in matched_content
        ):
            raise ValueError("AutoMod execution matched content is invalid")
        if digest is not None and (
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("AutoMod execution content digest is invalid")
        guild_ref = reference("guild")
        channel_ref = reference("channel", optional=True)
        rule_ref = reference("rule")
        user_ref = reference("user")
        alert_ref = reference("alert_system_message", optional=True)
        if guild_ref is None or rule_ref is None or user_ref is None:
            raise ValueError("AutoMod execution reference is invalid")
        action = AutoModExecutedAction.from_payload(
            raw_action,
            outcome=payload.get("outcome"),
        )
        if (trigger_type == "member_profile") is not (channel_ref is None):
            raise ValueError("AutoMod execution source channel is invalid")
        if (action.outcome == "alerted") is not (alert_ref is not None):
            raise ValueError("AutoMod execution alert message is invalid")
        allowed_outcomes = {
            "block_message": {"blocked", "failed"},
            "send_alert_message": {"alerted", "failed"},
            "timeout": {"timed_out", "failed"},
            "block_member_interaction": {"blocked", "failed"},
        }[action.type]
        if action.outcome not in allowed_outcomes:
            raise ValueError("AutoMod execution action outcome is invalid")
        return cls(
            guild_ref=guild_ref,
            channel_ref=channel_ref,
            rule_ref=rule_ref,
            user_ref=user_ref,
            rule_trigger_type=trigger_type,
            action=action,
            content=content,
            matched_keyword=matched_keyword,
            matched_content=matched_content,
            alert_system_message_ref=alert_ref,
            content_digest=digest,
        )


__all__ = [
    "AutoModAction",
    "AutoModActionType",
    "AutoModEventType",
    "AutoModExecutedAction",
    "AutoModExecution",
    "AutoModPresetType",
    "AutoModRule",
    "AutoModTriggerMetadata",
    "AutoModTriggerType",
]
