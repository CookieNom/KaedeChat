from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_stickers import validate_sticker_items
from app.chat.e2ee import interaction_routing_poll, validate_interaction_routing_contract
from app.chat.forwarding import forward_snapshot_matches_attachments, validate_forward_snapshot
from app.chat.interaction_metadata import validate_interaction_metadata
from app.chat.message_flags import MESSAGE_FLAG_IS_VOICE_MESSAGE
from app.chat.poll_results import (
    POLL_RESULT_MESSAGE_TYPE,
    poll_result_embed_has_private_labels,
    validate_poll_result_embed,
    validate_poll_result_projection,
)
from app.chat.rich_content import (
    MESSAGE_LAYOUT_COMPONENT_ADAPTER,
    Embed,
    PollMedia,
    uses_components_v2,
    validate_attachment_url_references,
    validate_embed_collection,
    validate_message_components,
)
from app.core.types import validate_wire_snowflake
from app.db.models import Message, MessageView, Poll, PollAnswer
from app.federation.network import normalize_domain


@dataclass(frozen=True, slots=True)
class PollProjection:
    question: dict[str, object]
    answers: tuple[tuple[int, str | None, dict[str, object] | None], ...]
    allow_multiselect: bool
    layout_type: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReplicatedRichProjection:
    embeds: list[dict[str, Any]]
    components: list[dict[str, Any]]
    sticker_items: list[dict[str, object]]
    application_ref: tuple[int, str] | None
    interaction_metadata: dict[str, object] | None
    view_version: int
    view_persistent: bool
    view_expires_at: datetime | None
    interaction_integration_type: str | None
    interaction_installation_ref: tuple[int, str] | None
    interaction_installation_revision: int | None
    forwarded_ref: tuple[int, str] | None
    forwarded_channel_ref: tuple[int, str] | None
    forward_snapshot: dict[str, object] | None
    poll: PollProjection | None
    poll_result: dict[str, object] | None
    has_encrypted_controls: bool = False
    has_encrypted_forward: bool = False


@dataclass(frozen=True, slots=True)
class WebhookAttribution:
    webhook_ref: tuple[int, str]
    name: str
    avatar_hash: str | None
    avatar_url: str | None


def _wire_snowflake(value: object, field: str) -> int:
    try:
        return validate_wire_snowflake(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a decimal snowflake") from exc


def _qualified_ref(value: object, field: str) -> tuple[int, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or "@" not in value:
        raise ValueError(f"{field} is invalid")
    identifier, domain = value.rsplit("@", 1)
    return _wire_snowflake(identifier, field), normalize_domain(domain)


def _timestamp(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def validate_webhook_attribution(
    value: object,
    *,
    message_type: int,
    message_origin: str,
    label: str,
) -> WebhookAttribution | None:
    """Validate Discord-compatible type-0 webhook attribution from an authority."""

    if value is None:
        return None
    if message_type != 0 or not isinstance(value, dict):
        raise ValueError(f"{label} webhook attribution is invalid")
    webhook_ref = (
        _wire_snowflake(value.get("id"), f"{label} webhook id"),
        normalize_domain(str(value.get("origin_domain", ""))),
    )
    if webhook_ref[1] != message_origin:
        raise ValueError(f"{label} webhook authority does not match the message authority")
    supplied_ref = value.get("ref")
    if supplied_ref is not None and supplied_ref != f"{webhook_ref[0]}@{webhook_ref[1]}":
        raise ValueError(f"{label} webhook reference is inconsistent")
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or not name.strip():
        raise ValueError(f"{label} webhook name is invalid")
    avatar_hash = value.get("avatar_hash")
    if avatar_hash is not None and (
        not isinstance(avatar_hash, str)
        or len(avatar_hash) != 64
        or any(character not in "0123456789abcdef" for character in avatar_hash)
    ):
        raise ValueError(f"{label} webhook avatar is invalid")
    avatar_url = value.get("avatar_url")
    if avatar_url is not None:
        if not isinstance(avatar_url, str) or not 1 <= len(avatar_url) <= 2048:
            raise ValueError(f"{label} webhook avatar URL is invalid")
        parsed = urlsplit(avatar_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(f"{label} webhook avatar URL is invalid")
    if avatar_hash is not None and avatar_url is not None:
        raise ValueError(f"{label} webhook has conflicting avatar sources")
    return WebhookAttribution(
        webhook_ref=webhook_ref,
        name=name,
        avatar_hash=avatar_hash,
        avatar_url=avatar_url,
    )


def validate_rendered_poll(
    value: object,
    *,
    message_created_at: datetime,
    label: str,
) -> PollProjection | None:
    """Validate the immutable definition in a rendered poll projection.

    Result counts are intentionally checked but not persisted here. Votes have
    their own authorized mutation path and a message create/history payload may
    not manufacture durable votes.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} poll is invalid")
    try:
        question_model = PollMedia.model_validate(value.get("question"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} poll question is invalid") from exc
    if question_model.text is None or question_model.emoji is not None:
        raise ValueError(f"{label} poll question is invalid")
    raw_answers = value.get("answers")
    if not isinstance(raw_answers, list) or not 2 <= len(raw_answers) <= 10:
        raise ValueError(f"{label} poll answers are invalid")
    answers: list[tuple[int, str | None, dict[str, object] | None]] = []
    for expected_id, raw_answer in enumerate(raw_answers, start=1):
        if not isinstance(raw_answer, dict) or raw_answer.get("answer_id") != expected_id:
            raise ValueError(f"{label} poll answer identity is invalid")
        try:
            media = PollMedia.model_validate(raw_answer.get("poll_media"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} poll answer is invalid") from exc
        if media.text is not None and len(media.text) > 55:
            raise ValueError(f"{label} poll answer text is too long")
        answers.append(
            (
                expected_id,
                media.text,
                (
                    media.emoji.model_dump(mode="json", exclude_none=True)
                    if media.emoji is not None
                    else None
                ),
            )
        )
    allow_multiselect = value.get("allow_multiselect")
    layout_type = value.get("layout_type")
    if not isinstance(allow_multiselect, bool) or layout_type != 1:
        raise ValueError(f"{label} poll options are invalid")
    expires_at = _timestamp(value.get("expiry"), f"{label} poll expiry")
    if (
        expires_at is None
        or expires_at <= message_created_at
        or expires_at > message_created_at + timedelta(hours=768, seconds=2)
    ):
        raise ValueError(f"{label} poll expiry is invalid")
    results = value.get("results")
    if not isinstance(results, dict) or not isinstance(results.get("is_finalized"), bool):
        raise ValueError(f"{label} poll results are invalid")
    finalized_at = _timestamp(
        value.get("finalized_at"),
        f"{label} poll finalization",
        optional=True,
    )
    if finalized_at is not None and (
        finalized_at < message_created_at or results.get("is_finalized") is not True
    ):
        raise ValueError(f"{label} poll finalization is invalid")
    counts = results.get("answer_counts")
    if not isinstance(counts, list) or len(counts) != len(answers):
        raise ValueError(f"{label} poll counts are invalid")
    for expected_id, raw_count in enumerate(counts, start=1):
        if (
            not isinstance(raw_count, dict)
            or raw_count.get("id") != expected_id
            or isinstance(raw_count.get("count"), bool)
            or not isinstance(raw_count.get("count"), int)
            or raw_count["count"] < 0
            or not isinstance(raw_count.get("me_voted"), bool)
        ):
            raise ValueError(f"{label} poll count is invalid")
    return PollProjection(
        question=question_model.model_dump(mode="json", exclude_none=True),
        answers=tuple(answers),
        allow_multiselect=allow_multiselect,
        layout_type=1,
        expires_at=expires_at,
    )


def validate_encrypted_rendered_poll(
    value: object,
    *,
    contract: dict[str, object],
    message_created_at: datetime,
    label: str,
) -> PollProjection:
    """Validate a label-free poll projection against its MLS-bound contract."""

    if not isinstance(value, dict) or set(value) != {
        "encrypted",
        "answer_ids",
        "expiry",
        "allow_multiselect",
        "layout_type",
        "finalized_at",
        "results",
    }:
        raise ValueError(f"{label} encrypted poll is invalid")
    raw_answer_ids = contract.get("answer_ids")
    raw_duration_seconds = contract.get("duration_seconds")
    if (
        not isinstance(raw_answer_ids, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_answer_ids)
        or isinstance(raw_duration_seconds, bool)
        or not isinstance(raw_duration_seconds, int)
    ):
        raise ValueError(f"{label} encrypted poll contract is invalid")
    answer_ids = cast(list[int], raw_answer_ids)
    if (
        value.get("encrypted") is not True
        or value.get("answer_ids") != answer_ids
        or value.get("allow_multiselect") is not contract["allow_multiselect"]
        or value.get("layout_type") != 1
    ):
        raise ValueError(f"{label} encrypted poll routing is invalid")
    expires_at = _timestamp(value.get("expiry"), f"{label} encrypted poll expiry")
    if (
        expires_at is None
        or abs(
            (
                expires_at - message_created_at - timedelta(seconds=raw_duration_seconds)
            ).total_seconds()
        )
        > 2
    ):
        raise ValueError(f"{label} encrypted poll expiry is invalid")
    finalized_at = _timestamp(
        value.get("finalized_at"),
        f"{label} encrypted poll finalization",
        optional=True,
    )
    results = value.get("results")
    counts = results.get("answer_counts") if isinstance(results, dict) else None
    if (
        not isinstance(results, dict)
        or not isinstance(results.get("is_finalized"), bool)
        or not isinstance(counts, list)
        or len(counts) != len(answer_ids)
    ):
        raise ValueError(f"{label} encrypted poll results are invalid")
    for answer_id, count in zip(answer_ids, counts, strict=True):
        if (
            not isinstance(count, dict)
            or count.get("id") != answer_id
            or isinstance(count.get("count"), bool)
            or not isinstance(count.get("count"), int)
            or count["count"] < 0
            or not isinstance(count.get("me_voted"), bool)
        ):
            raise ValueError(f"{label} encrypted poll count is invalid")
    if finalized_at is not None and results.get("is_finalized") is not True:
        raise ValueError(f"{label} encrypted poll finalization is invalid")
    return PollProjection(
        question={"encrypted": True, "version": 1},
        answers=tuple((int(answer_id), f"encrypted:{answer_id}", None) for answer_id in answer_ids),
        allow_multiselect=bool(contract["allow_multiselect"]),
        layout_type=1,
        expires_at=expires_at,
    )


def validate_replicated_rich_projection(
    raw: dict[str, Any],
    *,
    message_id: int,
    message_origin: str,
    message_created_at: datetime,
    e2ee: dict[str, Any] | None,
    message_type: int,
    label: str,
    is_crosspost: bool | None = None,
) -> ReplicatedRichProjection:
    """Strictly sanitize rich message fields received from another instance."""

    if raw.get("webhook") is not None:
        raise ValueError(f"{label} cannot contain webhook attribution")

    raw_embeds = raw.get("embeds", [])
    raw_components = raw.get("components", [])
    raw_sticker_items = raw.get("sticker_items", [])
    raw_attachments = raw.get("attachments", [])
    encrypted_rich = isinstance(e2ee, dict) and "rich_payload_digest" in e2ee
    encrypted_forward_routing = bool(
        encrypted_rich
        and isinstance(e2ee, dict)
        and e2ee.get("forward_snapshot_digest") is not None
    )
    if (
        not isinstance(raw_embeds, list)
        or not isinstance(raw_components, list)
        or not isinstance(raw_attachments, list)
        or len(raw_attachments) > 10
    ):
        raise ValueError(f"{label} rich content is invalid")
    try:
        embed_inputs = (
            [
                {str(key): child for key, child in item.items() if key != "type"}
                if isinstance(item, dict)
                else item
                for item in raw_embeds
            ]
            if message_type == POLL_RESULT_MESSAGE_TYPE
            else raw_embeds
        )
        embeds = [Embed.model_validate(item) for item in embed_inputs]
        components = [
            MESSAGE_LAYOUT_COMPONENT_ADAPTER.validate_python(item) for item in raw_components
        ]
        validate_embed_collection(embeds)
        validate_message_components(components)
        validate_attachment_url_references(
            embeds=embeds,
            components=components,
            attachments=raw_attachments,
        )
        sticker_items = validate_sticker_items(
            raw_sticker_items,
            maximum=9 if encrypted_forward_routing else 3,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} rich content is invalid") from exc
    rendered_embeds = [item.model_dump(mode="json", exclude_none=True) for item in embeds]
    rendered_components = [item.model_dump(mode="json", exclude_none=True) for item in components]
    encrypted_contract: dict[str, object] | None = None
    encrypted_controls = False
    encrypted_poll_contract: dict[str, object] | None = None
    if encrypted_rich:
        if not isinstance(e2ee, dict):
            raise ValueError(f"{label} encrypted rich envelope is invalid")
        raw_contract = e2ee.get("interaction_contract")
        if raw_contract is not None:
            encrypted_contract = validate_interaction_routing_contract(
                raw_contract,
                callback_type=None,
            )
            encrypted_controls = bool(encrypted_contract.get("components"))
            encrypted_poll_contract = interaction_routing_poll(encrypted_contract)

    raw_application_id = raw.get("application_id")
    raw_application_domain = raw.get("application_domain")
    if (raw_application_id is None) != (raw_application_domain is None):
        raise ValueError(f"{label} application identity is incomplete")
    application_ref: tuple[int, str] | None = None
    if raw_application_id is not None:
        application_ref = (
            _wire_snowflake(raw_application_id, f"{label} application id"),
            normalize_domain(str(raw_application_domain)),
        )

    raw_referenced_id = raw.get("referenced_message_id")
    raw_referenced_domain = raw.get("referenced_message_domain")
    if (raw_referenced_id is None) != (raw_referenced_domain is None):
        raise ValueError(f"{label} reply projection is incomplete")
    referenced_ref = (
        (
            _wire_snowflake(raw_referenced_id, f"{label} referenced message id"),
            normalize_domain(str(raw_referenced_domain)),
        )
        if raw_referenced_id is not None
        else None
    )
    raw_poll_result = raw.get("poll_result")
    if message_type == POLL_RESULT_MESSAGE_TYPE:
        if referenced_ref is None or raw_poll_result is None:
            raise ValueError(f"{label} poll result is missing its source reference")
        try:
            poll_result = validate_poll_result_projection(
                raw_poll_result,
                source_ref=referenced_ref,
            )
        except ValueError as exc:
            raise ValueError(f"{label} poll result projection is invalid") from exc
        if len(raw_embeds) != 1:
            raise ValueError(f"{label} poll result embed is missing")
        try:
            rendered_embeds = [validate_poll_result_embed(raw_embeds[0], projection=poll_result)]
            if poll_result["source_encryption_mode"] == "e2ee" and (
                poll_result_embed_has_private_labels(rendered_embeds[0])
            ):
                raise ValueError(f"{label} encrypted poll result leaks private labels")
        except ValueError as exc:
            raise ValueError(f"{label} poll result embed is invalid") from exc
    else:
        if raw_poll_result is not None:
            raise ValueError(f"{label} exposes poll result metadata on the wrong message type")
        poll_result = None
    try:
        interaction_metadata = validate_interaction_metadata(
            raw.get("interaction_metadata"),
            message_type=message_type,
            application_ref=application_ref,
            referenced_message_ref=referenced_ref,
            message_ref=(message_id, message_origin),
        )
    except ValueError as exc:
        raise ValueError(f"{label} interaction metadata is invalid") from exc

    view_version = raw.get("view_version", 0)
    view_persistent = raw.get("view_persistent", False)
    if (
        isinstance(view_version, bool)
        or not isinstance(view_version, int)
        or view_version < 0
        or not isinstance(view_persistent, bool)
    ):
        raise ValueError(f"{label} view metadata is invalid")
    view_expires_at = _timestamp(
        raw.get("view_expires_at"),
        f"{label} view expiry",
        optional=True,
    )
    view_reference_at = (
        _timestamp(
            raw.get("edited_at"),
            f"{label} edit timestamp",
            optional=True,
        )
        or message_created_at
    )
    integration_type = raw.get("interaction_integration_type")
    raw_installation_ref = raw.get("interaction_installation_ref")
    raw_installation_revision = raw.get("interaction_installation_revision")
    installation_ref: tuple[int, str] | None = None
    if raw_installation_ref is not None:
        if not isinstance(raw_installation_ref, str) or "@" not in raw_installation_ref:
            raise ValueError(f"{label} interaction installation reference is invalid")
        raw_installation_id, raw_installation_domain = raw_installation_ref.rsplit("@", 1)
        installation_ref = (
            _wire_snowflake(raw_installation_id, f"{label} interaction installation id"),
            normalize_domain(raw_installation_domain),
        )
    if raw_installation_revision is not None:
        raw_installation_revision = _wire_snowflake(
            raw_installation_revision,
            f"{label} interaction installation revision",
        )

    if encrypted_rich:
        if not isinstance(e2ee, dict):
            raise ValueError(f"{label} encrypted rich envelope is invalid")
        envelope_application_ref = _qualified_ref(
            e2ee.get("application_ref"),
            f"{label} encrypted application reference",
        )
        envelope_installation_ref = _qualified_ref(
            e2ee.get("interaction_installation_ref"),
            f"{label} encrypted installation reference",
        )
        envelope_installation_revision = e2ee.get("interaction_installation_revision")
        parsed_envelope_revision = (
            _wire_snowflake(
                envelope_installation_revision,
                f"{label} encrypted installation revision",
            )
            if envelope_installation_revision is not None
            else None
        )
        if application_ref != envelope_application_ref or view_version != _wire_snowflake(
            e2ee.get("view_version"),
            f"{label} encrypted view version",
        ):
            raise ValueError(f"{label} encrypted application projection is inconsistent")
        if bool(e2ee.get("view_persistent")) is not view_persistent:
            raise ValueError(f"{label} encrypted view persistence is inconsistent")
        if encrypted_controls:
            if (
                integration_type != e2ee.get("interaction_integration_type")
                or installation_ref != envelope_installation_ref
                or raw_installation_revision != parsed_envelope_revision
            ):
                raise ValueError(f"{label} encrypted view lineage is inconsistent")
        elif (
            integration_type is not None
            or installation_ref is not None
            or raw_installation_revision is not None
        ):
            raise ValueError(f"{label} exposes view lineage without encrypted controls")

        expected_author_ref = (
            _wire_snowflake(raw.get("author_id"), f"{label} author id"),
            normalize_domain(str(raw.get("author_domain", ""))),
        )
        if _qualified_ref(e2ee.get("author_ref"), f"{label} encrypted author reference") != (
            expected_author_ref
        ):
            raise ValueError(f"{label} encrypted author projection is inconsistent")
        attachment_refs: list[tuple[int, str]] = []
        for attachment in raw_attachments:
            if not isinstance(attachment, dict):
                raise ValueError(f"{label} encrypted attachment projection is invalid")
            attachment_refs.append(
                (
                    _wire_snowflake(
                        attachment.get("id"),
                        f"{label} encrypted attachment id",
                    ),
                    normalize_domain(str(attachment.get("origin_domain", ""))),
                )
            )
        envelope_attachment_refs = [
            cast(
                tuple[int, str],
                _qualified_ref(item, f"{label} encrypted attachment reference"),
            )
            for item in e2ee.get("message_attachment_refs", [])
        ]
        if sorted(attachment_refs) != sorted(envelope_attachment_refs):
            raise ValueError(f"{label} encrypted attachment projection is inconsistent")
        raw_mentions = raw.get("mention_user_refs", [])
        if not isinstance(raw_mentions, list):
            raise ValueError(f"{label} encrypted mention projection is invalid")
        mention_refs: list[tuple[int, str]] = []
        for mention in raw_mentions:
            if not isinstance(mention, dict):
                raise ValueError(f"{label} encrypted mention projection is invalid")
            mention_refs.append(
                (
                    _wire_snowflake(
                        mention.get("id"),
                        f"{label} encrypted mention id",
                    ),
                    normalize_domain(str(mention.get("origin_domain", ""))),
                )
            )
        envelope_mention_refs = [
            cast(
                tuple[int, str],
                _qualified_ref(item, f"{label} encrypted mention reference"),
            )
            for item in e2ee.get("message_mention_refs", [])
        ]
        if sorted(mention_refs) != sorted(envelope_mention_refs):
            raise ValueError(f"{label} encrypted mention projection is inconsistent")
        envelope_sticker_refs = [
            cast(
                tuple[int, str],
                _qualified_ref(item, f"{label} encrypted sticker reference"),
            )
            for item in e2ee.get("message_sticker_refs", [])
        ]
        projected_sticker_refs = [
            (
                _wire_snowflake(item.get("id"), f"{label} encrypted sticker id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
            for item in sticker_items
        ]
        if sorted(projected_sticker_refs) != sorted(envelope_sticker_refs):
            raise ValueError(f"{label} encrypted sticker projection is inconsistent")
        envelope_referenced_ref = (
            _qualified_ref(
                e2ee.get("referenced_message_ref"),
                f"{label} encrypted referenced message reference",
            )
            if e2ee.get("referenced_message_ref") is not None
            else None
        )
        if referenced_ref != envelope_referenced_ref:
            raise ValueError(f"{label} encrypted reply projection is inconsistent")
        if e2ee.get("tts") is not raw.get("tts", False) or bool(
            int(raw.get("flags", 0)) & MESSAGE_FLAG_IS_VOICE_MESSAGE
        ) is not bool(e2ee.get("voice_message")):
            raise ValueError(f"{label} encrypted delivery markers are inconsistent")
    has_interactive_components = bool(rendered_components) or encrypted_controls
    if has_interactive_components and application_ref is None and message_type != 2:
        raise ValueError(f"interactive {label} is missing its application")
    if has_interactive_components and application_ref is not None:
        if (
            integration_type not in {"guild_install", "user_install", "dm_capability"}
            or installation_ref is None
            or installation_ref[1] != message_origin
            or raw_installation_revision is None
        ):
            raise ValueError(f"interactive {label} installation lineage is invalid")
        if view_version < 1:
            raise ValueError(f"interactive {label} view version is invalid")
        if view_persistent:
            if view_expires_at is not None:
                raise ValueError(f"persistent {label} view cannot expire")
        elif (
            view_expires_at is None
            or view_expires_at <= view_reference_at
            or view_expires_at > view_reference_at + timedelta(days=1, seconds=2)
        ):
            raise ValueError(f"transient {label} view expiry is invalid")
    elif (
        (
            view_version != 0
            and not (
                encrypted_rich
                and e2ee is not None
                and e2ee.get("operation") == "edit"
                and not encrypted_controls
            )
        )
        or view_persistent
        or view_expires_at is not None
        or integration_type is not None
        or installation_ref is not None
        or raw_installation_revision is not None
    ):
        raise ValueError(f"{label} has view metadata without an interactive application")

    if encrypted_controls and not view_persistent:
        if encrypted_contract is None:
            raise ValueError(f"{label} encrypted routing contract is missing")
        expected_expiry = view_reference_at + timedelta(
            seconds=int(str(encrypted_contract["view_timeout_seconds"]))
        )
        if view_expires_at is None or abs((view_expires_at - expected_expiry).total_seconds()) > 2:
            raise ValueError(f"{label} encrypted view expiry is inconsistent")

    raw_forward_id = raw.get("forwarded_message_id")
    raw_forward_domain = raw.get("forwarded_message_domain")
    if (raw_forward_id is None) != (raw_forward_domain is None):
        raise ValueError(f"{label} forward identity is incomplete")
    forwarded_ref: tuple[int, str] | None = None
    if raw_forward_id is not None:
        forwarded_ref = (
            _wire_snowflake(raw_forward_id, f"{label} forwarded message id"),
            normalize_domain(str(raw_forward_domain)),
        )
        if forwarded_ref[1] == message_origin and forwarded_ref >= (
            message_id,
            message_origin,
        ):
            raise ValueError(f"{label} forward identity is invalid")
    raw_forward_channel_id = raw.get("forwarded_channel_id")
    raw_forward_channel_domain = raw.get("forwarded_channel_domain")
    if (raw_forward_channel_id is None) != (raw_forward_channel_domain is None):
        raise ValueError(f"{label} forward channel identity is incomplete")
    forwarded_channel_ref: tuple[int, str] | None = None
    if raw_forward_channel_id is not None:
        forwarded_channel_ref = (
            _wire_snowflake(raw_forward_channel_id, f"{label} forwarded channel id"),
            normalize_domain(str(raw_forward_channel_domain)),
        )
    raw_snapshot = raw.get("forward_snapshot")
    forward_snapshot = validate_forward_snapshot(raw_snapshot) if raw_snapshot is not None else None
    crosspost = False if is_crosspost is None else is_crosspost
    if forward_snapshot is not None and (
        forwarded_ref is None or forwarded_channel_ref is None or crosspost
    ):
        raise ValueError(f"{label} forward snapshot provenance is invalid")
    encrypted_forward = bool(
        encrypted_rich
        and isinstance(e2ee, dict)
        and e2ee.get("forward_snapshot_digest") is not None
    )
    if encrypted_forward and (
        e2ee is None
        or (
            _qualified_ref(
                e2ee.get("forwarded_message_ref"),
                f"{label} encrypted forwarded message reference",
            )
            != forwarded_ref
            or _qualified_ref(
                e2ee.get("forwarded_channel_ref"),
                f"{label} encrypted forwarded channel reference",
            )
            != forwarded_channel_ref
            or forward_snapshot is not None
        )
    ):
        raise ValueError(f"{label} encrypted forward lineage is inconsistent")
    if (
        not crosspost
        and forwarded_ref is not None
        and forward_snapshot is None
        and not encrypted_forward
    ):
        raise ValueError(f"{label} forward is missing its immutable snapshot")
    if forward_snapshot is not None:
        snapshot_created_at = datetime.fromisoformat(str(forward_snapshot["created_at"]))
        if snapshot_created_at > message_created_at + timedelta(seconds=2):
            raise ValueError(f"{label} forward snapshot postdates the forward")

    poll = (
        validate_encrypted_rendered_poll(
            raw.get("poll"),
            contract=encrypted_poll_contract,
            message_created_at=message_created_at,
            label=label,
        )
        if encrypted_poll_contract is not None
        else validate_rendered_poll(
            raw.get("poll"),
            message_created_at=message_created_at,
            label=label,
        )
    )
    if e2ee is not None and (
        rendered_embeds
        or rendered_components
        or sticker_items
        and not encrypted_rich
        or poll is not None
        and encrypted_poll_contract is None
    ):
        raise ValueError(f"encrypted {label} contains rich plaintext")
    if sticker_items and uses_components_v2(components):
        raise ValueError(f"components-v2 {label} contains stickers")
    if (
        not crosspost
        and forwarded_ref is not None
        and not encrypted_forward
        and (
            e2ee is not None
            or rendered_embeds
            or rendered_components
            or sticker_items
            or poll is not None
            or raw_attachments
            and (
                forward_snapshot is None
                or not forward_snapshot_matches_attachments(forward_snapshot, raw_attachments)
            )
            or raw.get("referenced_message_id") is not None
        )
    ):
        raise ValueError(f"{label} forward contains copied rich content")
    return ReplicatedRichProjection(
        embeds=rendered_embeds,
        components=rendered_components,
        sticker_items=sticker_items,
        application_ref=application_ref,
        interaction_metadata=interaction_metadata,
        view_version=view_version,
        view_persistent=view_persistent,
        view_expires_at=view_expires_at,
        interaction_integration_type=(
            str(integration_type) if integration_type is not None else None
        ),
        interaction_installation_ref=installation_ref,
        interaction_installation_revision=raw_installation_revision,
        forwarded_ref=forwarded_ref,
        forwarded_channel_ref=forwarded_channel_ref,
        forward_snapshot=forward_snapshot,
        poll=poll,
        poll_result=poll_result,
        has_encrypted_controls=encrypted_controls,
        has_encrypted_forward=encrypted_forward,
    )


async def stored_poll_matches_projection(
    session: AsyncSession,
    message: Message,
    projection: PollProjection | None,
) -> bool:
    poll = await session.get(Poll, (message.id, message.origin_domain))
    if projection is None:
        return poll is None
    if poll is None or (
        poll.question != projection.question
        or poll.allow_multiselect != projection.allow_multiselect
        or poll.layout_type != projection.layout_type
        or poll.expires_at != projection.expires_at
    ):
        return False
    answers = list(
        await session.scalars(
            select(PollAnswer)
            .where(
                PollAnswer.message_id == message.id,
                PollAnswer.message_domain == message.origin_domain,
            )
            .order_by(PollAnswer.answer_id)
        )
    )
    return [(answer.answer_id, answer.text, answer.emoji) for answer in answers] == list(
        projection.answers
    )


async def stored_view_matches_projection(
    session: AsyncSession,
    message: Message,
    projection: ReplicatedRichProjection,
) -> bool:
    view = await session.get(MessageView, (message.id, message.origin_domain))
    if (
        not projection.components
        and not projection.has_encrypted_controls
        or projection.application_ref is None
    ):
        return view is None
    return bool(
        view is not None
        and (view.application_id, view.application_domain) == projection.application_ref
        and view.version == projection.view_version
        and view.persistent == projection.view_persistent
        and view.expires_at == projection.view_expires_at
        and view.integration_type == projection.interaction_integration_type
        and (view.installation_id, view.installation_domain)
        == projection.interaction_installation_ref
        and view.installation_revision == projection.interaction_installation_revision
    )


def add_poll_projection(
    session: AsyncSession,
    message: Message,
    projection: PollProjection,
    *,
    created_at: datetime,
) -> None:
    session.add(
        Poll(
            message_id=message.id,
            message_domain=message.origin_domain,
            question=projection.question,
            allow_multiselect=projection.allow_multiselect,
            layout_type=projection.layout_type,
            expires_at=projection.expires_at,
            created_at=created_at,
        )
    )
    for answer_id, text, emoji in projection.answers:
        session.add(
            PollAnswer(
                message_id=message.id,
                message_domain=message.origin_domain,
                answer_id=answer_id,
                text=text,
                emoji=emoji,
            )
        )
