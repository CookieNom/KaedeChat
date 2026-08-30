from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.chat.custom_emojis import (
    CUSTOM_EMOJI_PATTERN,
    custom_emoji_refs,
    rich_custom_emojis,
)
from app.chat.custom_stickers import validate_sticker_items
from app.chat.rich_content import MessageLayoutComponent, PollCreate
from app.core.federation import canonical_json
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE
from app.core.types import EntityRef

EXPRESSION_USE_AUTHORIZATION_EVENT = "expression.use.authorized"
EXPRESSION_USE_AUTHORIZATION_TTL_SECONDS = 90
ExpressionOperation = Literal[
    "message.create",
    "message.edit",
    "reaction.add",
    "reaction.remove",
]


def expression_actor_intent_resources(
    *,
    source_authority: str,
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: ExpressionOperation,
    operation_id: str,
    emoji_tokens: list[str],
    sticker_refs: list[str],
    authorization_nonce: str,
) -> dict[str, str]:
    projection = canonical_json(
        {
            "emoji_tokens": emoji_tokens,
            "sticker_refs": sticker_refs,
        }
    )
    return {
        "authorization_nonce": authorization_nonce,
        "expression_projection_sha256": hashlib.sha256(projection).hexdigest(),
        "operation": operation,
        "operation_id": operation_id,
        "source_authority": source_authority,
        "target_channel_ref": target_channel_ref,
        "target_guild_ref": target_guild_ref,
        "target_message_ref": target_message_ref or "none",
    }


def _qualified_ref(value: str, *, label: str) -> str:
    try:
        reference = EntityRef(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a qualified reference") from exc
    if reference.domain is None or str(reference) != value:
        raise ValueError(f"{label} is not a qualified reference")
    return value


def canonical_expression_emoji_tokens(tokens: list[str]) -> list[str]:
    normalized = sorted(set(tokens))
    if normalized != tokens or len(normalized) > 256:
        raise ValueError("expression emoji tokens must be sorted and unique")
    if any(CUSTOM_EMOJI_PATTERN.fullmatch(token) is None for token in normalized):
        raise ValueError("expression emoji token is invalid")
    return normalized


def canonical_expression_authority_map(
    value: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    if len(value) > 16:
        raise ValueError("expression authority map is too large")
    for authority, proof in value.items():
        if authority != authority.rstrip(".").lower() or DOMAIN_RE.fullmatch(authority) is None:
            raise ValueError("expression authority map contains an invalid domain")
        if not isinstance(proof, dict):
            raise ValueError("expression authority map contains an invalid proof")
    return dict(sorted(value.items()))


def expression_custom_emoji_tokens(
    *,
    content: str | None,
    components: list[MessageLayoutComponent] | None,
    poll: PollCreate | None,
    e2ee: dict[str, object] | None,
    default_domain: str,
) -> list[str]:
    tokens = {reference.token for reference in custom_emoji_refs(content)}
    for partial in rich_custom_emojis(components, poll):
        if partial.id is None:
            continue
        emoji_id, emoji_domain = partial.id.resolve(default_domain)
        if partial.name is None:
            raise ValueError("custom rich emoji requires canonical metadata")
        prefix = "a" if partial.animated else ""
        token = f"<{prefix}:{partial.name}:{emoji_id}@{emoji_domain}>"
        if CUSTOM_EMOJI_PATTERN.fullmatch(token) is None:
            raise ValueError("custom rich emoji metadata is invalid")
        tokens.add(token)
    if isinstance(e2ee, dict) and "rich_payload_digest" in e2ee:
        raw = e2ee.get("message_custom_emoji_refs", [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("encrypted expression routing is invalid")
        tokens.update(raw)
    return sorted(tokens)


class ExpressionUseAuthorization(UnambiguousInputModel):
    """Source-authority receipt for one exact destination operation."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source_authority: str = Field(min_length=1, max_length=253)
    requester_ref: str
    requester_type: Literal["human", "bot"]
    application_ref: str | None = None
    target_guild_ref: str
    target_channel_ref: str
    target_message_ref: str | None = None
    operation: ExpressionOperation
    operation_id: str = Field(min_length=1, max_length=128)
    emoji_tokens: list[str] = Field(default_factory=list, max_length=256)
    sticker_items: list[dict[str, object]] = Field(default_factory=list, max_length=9)
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    issued_at: datetime
    expires_at: datetime

    @field_validator("source_authority")
    @classmethod
    def canonical_authority(cls, value: str) -> str:
        if value != value.rstrip(".").lower() or DOMAIN_RE.fullmatch(value) is None:
            raise ValueError("expression source authority is invalid")
        return value

    @field_validator(
        "requester_ref",
        "target_guild_ref",
        "target_channel_ref",
        "target_message_ref",
        "application_ref",
    )
    @classmethod
    def canonical_refs(cls, value: str | None) -> str | None:
        return _qualified_ref(value, label="expression authorization reference") if value else None

    @field_validator("emoji_tokens")
    @classmethod
    def canonical_emojis(cls, value: list[str]) -> list[str]:
        return canonical_expression_emoji_tokens(value)

    @field_validator("sticker_items")
    @classmethod
    def canonical_stickers(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized = validate_sticker_items(value, maximum=9)
        refs = [f"{item['id']}@{item['origin_domain']}" for item in normalized]
        if refs != sorted(set(refs)):
            raise ValueError("expression stickers must be sorted and unique")
        return normalized

    @model_validator(mode="after")
    def valid_binding(self) -> ExpressionUseAuthorization:
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("expression authorization timestamps require a timezone")
        if self.expires_at <= self.issued_at or self.expires_at > self.issued_at + timedelta(
            seconds=EXPRESSION_USE_AUTHORIZATION_TTL_SECONDS
        ):
            raise ValueError("expression authorization lifetime is invalid")
        if not self.emoji_tokens and not self.sticker_items:
            raise ValueError("expression authorization requires an expression")
        if self.requester_type == "bot" and self.application_ref is None:
            raise ValueError("bot expression authorization requires an application")
        if self.requester_type == "human" and self.application_ref is not None:
            raise ValueError("human expression authorization cannot claim an application")
        if self.operation == "message.create" and self.target_message_ref is not None:
            raise ValueError("message creation cannot bind an existing message")
        if self.operation != "message.create" and self.target_message_ref is None:
            raise ValueError("expression operation requires a target message")
        return self


def authority_attested_expression_use(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    """Recognize the one closed S-signed receipt allowed a remote actor."""

    if event_type != EXPRESSION_USE_AUTHORIZATION_EVENT:
        return False
    try:
        authorization = ExpressionUseAuthorization.model_validate(content)
        requester = EntityRef(authorization.requester_ref)
    except (TypeError, ValueError):
        return False
    return bool(
        requester.domain is not None
        and authorization.source_authority == expected_authority
        and (str(requester.id), requester.domain) == actor
        and context
        == {
            "source_authority": expected_authority,
            "target_channel_ref": authorization.target_channel_ref,
        }
    )


def build_expression_use_authorization(
    *,
    source_authority: str,
    requester_ref: str,
    requester_type: Literal["human", "bot"],
    application_ref: str | None,
    target_guild_ref: str,
    target_channel_ref: str,
    target_message_ref: str | None,
    operation: ExpressionOperation,
    operation_id: str,
    emoji_tokens: list[str],
    sticker_items: list[dict[str, object]],
    nonce: str,
    now: datetime | None = None,
) -> dict[str, object]:
    current = now or datetime.now(UTC)
    return ExpressionUseAuthorization(
        source_authority=source_authority,
        requester_ref=requester_ref,
        requester_type=requester_type,
        application_ref=application_ref,
        target_guild_ref=target_guild_ref,
        target_channel_ref=target_channel_ref,
        target_message_ref=target_message_ref,
        operation=operation,
        operation_id=operation_id,
        emoji_tokens=emoji_tokens,
        sticker_items=sticker_items,
        nonce=nonce,
        issued_at=current,
        expires_at=current + timedelta(seconds=EXPRESSION_USE_AUTHORIZATION_TTL_SECONDS),
    ).model_dump(mode="json", exclude_none=False)
