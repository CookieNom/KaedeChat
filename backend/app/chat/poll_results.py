from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from app.core.model_validation import UnambiguousInputModel

POLL_RESULT_MESSAGE_TYPE = 46
POLL_RESULT_VERSION = 1
DM_POLL_MUTATION_EVENTS = frozenset({"dm.poll.vote.add", "dm.poll.vote.remove", "dm.poll.finalize"})
QualifiedRef = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=273,
        pattern=r"^[1-9][0-9]{0,18}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$",
    ),
]


class PollResultAnswerCount(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1, le=10)
    count: int = Field(ge=0)


class PollResultProjection(UnambiguousInputModel):
    """Authority-visible, label-free result metadata for message type 46."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=POLL_RESULT_VERSION, le=POLL_RESULT_VERSION)
    poll_message_ref: QualifiedRef
    source_encryption_mode: Literal["plaintext", "e2ee"]
    answer_counts: list[PollResultAnswerCount] = Field(min_length=1, max_length=10)
    total_votes: int = Field(ge=0)
    victor_answer_id: int | None = Field(default=None, ge=1, le=10)
    victor_answer_votes: int = Field(ge=0)

    @model_validator(mode="after")
    def internally_consistent(self) -> PollResultProjection:
        identifier = int(self.poll_message_ref.split("@", 1)[0])
        if identifier > (1 << 63) - 1:
            raise ValueError("poll result source snowflake is outside the database range")
        answer_ids = [item.id for item in self.answer_counts]
        if answer_ids != sorted(set(answer_ids)):
            raise ValueError("poll result answer counts must be sorted and unique")
        if self.total_votes != sum(item.count for item in self.answer_counts):
            raise ValueError("poll result total does not match its answer counts")
        highest = max(item.count for item in self.answer_counts)
        winners = [item.id for item in self.answer_counts if item.count == highest]
        expected_victor = winners[0] if highest > 0 and len(winners) == 1 else None
        if self.victor_answer_id != expected_victor or self.victor_answer_votes != highest:
            raise ValueError("poll result victor does not match its answer counts")
        return self


def validate_poll_result_projection(
    value: object,
    *,
    source_ref: tuple[int, str] | None = None,
) -> dict[str, object]:
    projection = PollResultProjection.model_validate(value)
    if source_ref is not None and projection.poll_message_ref != f"{source_ref[0]}@{source_ref[1]}":
        raise ValueError("poll result source reference is inconsistent")
    return projection.model_dump(mode="json")


def build_poll_result_projection(
    *,
    source_ref: tuple[int, str],
    answer_counts: Sequence[tuple[int, int]],
    source_encryption_mode: Literal["plaintext", "e2ee"],
) -> dict[str, object]:
    victor_id, victor_votes = _victor(answer_counts)
    return validate_poll_result_projection(
        {
            "version": POLL_RESULT_VERSION,
            "poll_message_ref": f"{source_ref[0]}@{source_ref[1]}",
            "source_encryption_mode": source_encryption_mode,
            "answer_counts": [
                {"id": answer_id, "count": count} for answer_id, count in sorted(answer_counts)
            ],
            "total_votes": sum(count for _, count in answer_counts),
            "victor_answer_id": victor_id,
            "victor_answer_votes": victor_votes,
        },
        source_ref=source_ref,
    )


def _victor(answer_counts: Sequence[tuple[int, int]]) -> tuple[int | None, int]:
    highest = max((count for _, count in answer_counts), default=0)
    winners = [answer_id for answer_id, count in answer_counts if count == highest]
    return (winners[0] if highest > 0 and len(winners) == 1 else None, highest)


def poll_result_embed(
    projection: Mapping[str, object],
    *,
    question: Mapping[str, object] | None,
    answers: Sequence[tuple[int, str | None, Mapping[str, object] | None]],
) -> dict[str, object]:
    """Build the Discord-style visual embed without leaking encrypted labels."""

    encrypted = question == {"encrypted": True, "version": 1}
    winner_id = projection.get("victor_answer_id")
    fields: list[dict[str, object]] = []
    if not encrypted:
        raw_question = question.get("text") if question is not None else None
        if isinstance(raw_question, str) and raw_question:
            fields.append({"name": "poll_question_text", "value": raw_question, "inline": False})
    fields.extend(
        (
            {
                "name": "victor_answer_votes",
                "value": str(projection["victor_answer_votes"]),
                "inline": False,
            },
            {
                "name": "total_votes",
                "value": str(projection["total_votes"]),
                "inline": False,
            },
        )
    )
    if isinstance(winner_id, int):
        fields.append({"name": "victor_answer_id", "value": str(winner_id), "inline": False})
        if not encrypted:
            answer = next((item for item in answers if item[0] == winner_id), None)
            if answer is not None:
                if answer[1]:
                    fields.append(
                        {
                            "name": "victor_answer_text",
                            "value": answer[1],
                            "inline": False,
                        }
                    )
                emoji = answer[2]
                if isinstance(emoji, Mapping):
                    if emoji.get("id") is not None:
                        fields.append(
                            {
                                "name": "victor_answer_emoji_id",
                                "value": str(emoji["id"]),
                                "inline": False,
                            }
                        )
                    if emoji.get("name") is not None:
                        fields.append(
                            {
                                "name": "victor_answer_emoji_name",
                                "value": str(emoji["name"]),
                                "inline": False,
                            }
                        )
                    if emoji.get("animated") is not None:
                        fields.append(
                            {
                                "name": "victor_answer_emoji_animated",
                                "value": "true" if emoji["animated"] is True else "false",
                                "inline": False,
                            }
                        )
    return {"type": "poll_result", "fields": fields}


def validate_poll_result_embed(
    value: object,
    *,
    projection: Mapping[str, object],
) -> dict[str, object]:
    """Validate Discord's output-only poll_result embed fields exactly."""

    if not isinstance(value, dict) or set(value) != {"type", "fields"}:
        raise ValueError("poll result embed shape is invalid")
    if value.get("type") != "poll_result" or not isinstance(value.get("fields"), list):
        raise ValueError("poll result embed type is invalid")
    raw_fields = value["fields"]
    if not 2 <= len(raw_fields) <= 8:
        raise ValueError("poll result embed field count is invalid")
    fields: dict[str, str] = {}
    allowed = {
        "poll_question_text",
        "victor_answer_votes",
        "total_votes",
        "victor_answer_id",
        "victor_answer_text",
        "victor_answer_emoji_id",
        "victor_answer_emoji_name",
        "victor_answer_emoji_animated",
    }
    for item in raw_fields:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "value", "inline"}
            or not isinstance(item.get("name"), str)
            or item["name"] not in allowed
            or item["name"] in fields
            or not isinstance(item.get("value"), str)
            or not 1 <= len(item["value"]) <= 1_024
            or item.get("inline") is not False
        ):
            raise ValueError("poll result embed field is invalid")
        fields[item["name"]] = item["value"]
    if fields.get("victor_answer_votes") != str(projection["victor_answer_votes"]):
        raise ValueError("poll result embed victor count is inconsistent")
    if fields.get("total_votes") != str(projection["total_votes"]):
        raise ValueError("poll result embed total is inconsistent")
    victor_id = projection.get("victor_answer_id")
    if fields.get("victor_answer_id") != (str(victor_id) if isinstance(victor_id, int) else None):
        raise ValueError("poll result embed victor is inconsistent")
    victor_label_fields = {
        "victor_answer_text",
        "victor_answer_emoji_id",
        "victor_answer_emoji_name",
        "victor_answer_emoji_animated",
    }
    if victor_id is None and victor_label_fields & fields.keys():
        raise ValueError("poll result embed labels require a victor")
    if "victor_answer_emoji_animated" in fields and fields["victor_answer_emoji_animated"] not in {
        "true",
        "false",
    }:
        raise ValueError("poll result embed animated marker is invalid")
    return {"type": "poll_result", "fields": list(raw_fields)}


def validate_poll_result_wire_body(
    raw: Mapping[str, object],
    *,
    author_ref: tuple[int, str],
    channel_ref: tuple[int, str],
    source_ref: tuple[int, str] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate the complete immutable type-46 body received over federation.

    Poll-result messages are minted by the conversation authority on behalf of
    the poll author.  That narrow exception must not become a generic way for
    an authority to inject content as a remote user.
    """

    referenced_id = raw.get("referenced_message_id")
    referenced_domain = raw.get("referenced_message_domain")
    if (
        isinstance(referenced_id, bool)
        or not isinstance(referenced_id, (str, int))
        or not str(referenced_id).isdigit()
        or str(int(str(referenced_id))) != str(referenced_id)
        or int(str(referenced_id)) <= 0
        or not isinstance(referenced_domain, str)
    ):
        raise ValueError("poll result source reference is invalid")
    actual_source_ref = (int(str(referenced_id)), referenced_domain)
    if source_ref is not None and actual_source_ref != source_ref:
        raise ValueError("poll result source reference is inconsistent")
    projection = validate_poll_result_projection(
        raw.get("poll_result"),
        source_ref=actual_source_ref,
    )

    if (
        raw.get("message_type") != POLL_RESULT_MESSAGE_TYPE
        or (str(raw.get("author_id")), raw.get("author_domain"))
        != (str(author_ref[0]), author_ref[1])
        or (str(raw.get("channel_id")), raw.get("channel_domain"))
        != (str(channel_ref[0]), channel_ref[1])
        or raw.get("content") is not None
        or raw.get("e2ee") is not None
        or raw.get("attachments") != []
        or raw.get("components") != []
        or raw.get("sticker_items") != []
        or raw.get("poll") is not None
        or raw.get("forwarded_message_id") is not None
        or raw.get("forwarded_message_domain") is not None
        or raw.get("forwarded_channel_id") is not None
        or raw.get("forwarded_channel_domain") is not None
        or raw.get("forward_snapshot") is not None
        or raw.get("application_id") is not None
        or raw.get("application_domain") is not None
        or raw.get("interaction_metadata") is not None
        or raw.get("webhook") is not None
        or raw.get("webhook_id") is not None
        or raw.get("tts") is not False
        or raw.get("flags") != 0
        or raw.get("view_version", 0) != 0
        or raw.get("view_persistent", False) is not False
        or raw.get("view_expires_at") is not None
        or raw.get("interaction_integration_type") is not None
        or raw.get("interaction_installation_ref") is not None
        or raw.get("interaction_installation_revision") is not None
        or raw.get("mention_user_refs")
        != [{"id": str(author_ref[0]), "origin_domain": author_ref[1]}]
    ):
        raise ValueError("poll result body is not the canonical system projection")

    embeds = raw.get("embeds")
    if not isinstance(embeds, list) or len(embeds) != 1:
        raise ValueError("poll result embed is missing")
    embed = validate_poll_result_embed(embeds[0], projection=projection)
    if projection["source_encryption_mode"] == "e2ee" and (
        poll_result_embed_has_private_labels(embed)
    ):
        raise ValueError("encrypted poll result leaks private labels")
    return projection, embed


def poll_result_embed_has_private_labels(value: Mapping[str, object]) -> bool:
    fields = value.get("fields")
    if not isinstance(fields, list):
        return True
    private_names = {
        "poll_question_text",
        "victor_answer_text",
        "victor_answer_emoji_id",
        "victor_answer_emoji_name",
        "victor_answer_emoji_animated",
    }
    return any(isinstance(item, dict) and item.get("name") in private_names for item in fields)


def authority_attested_direct_poll_result(
    event_type: object,
    content: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    """Recognize the exact direct-DM result an authority signs for its author."""

    if event_type != "dm.message.create" or not isinstance(content, dict):
        return False
    raw = content.get("message")
    author = content.get("author")
    if not isinstance(raw, dict) or not isinstance(author, dict):
        return False
    author_id, author_domain = actor
    channel_id = raw.get("channel_id")
    if (
        not author_id.isdigit()
        or str(int(author_id)) != author_id
        or int(author_id) <= 0
        or not isinstance(channel_id, str)
        or not channel_id.isdigit()
        or str(int(channel_id)) != channel_id
        or int(channel_id) <= 0
    ):
        return False
    try:
        validate_poll_result_wire_body(
            raw,
            author_ref=(int(author_id), author_domain),
            channel_ref=(int(channel_id), expected_authority),
        )
    except (TypeError, ValueError):
        return False
    return bool(
        raw.get("origin_domain") == expected_authority
        and raw.get("channel_domain") == expected_authority
        and (str(author.get("id")), author.get("origin_domain")) == actor
    )


def authority_attested_dm_poll_mutation(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
) -> bool:
    """Recognize one narrow authority-committed DM poll lifecycle event."""

    if (
        event_type not in DM_POLL_MUTATION_EVENTS
        or not isinstance(content, dict)
        or not isinstance(context, dict)
        or set(context) != {"conversation_id", "conversation_domain"}
        or context.get("conversation_domain") != expected_authority
    ):
        return False
    conversation_id = context.get("conversation_id")
    message_id = content.get("message_id")
    message_domain = content.get("message_domain")
    channel_id = content.get("channel_id")
    channel_domain = content.get("channel_domain")
    for value in (conversation_id, message_id, channel_id):
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or value.startswith("0")
            or int(value) > (1 << 63) - 1
        ):
            return False
    if (
        channel_id != conversation_id
        or channel_domain != expected_authority
        or not isinstance(message_domain, str)
        or not message_domain
    ):
        return False
    common = {
        "message_id",
        "message_domain",
        "channel_id",
        "channel_domain",
    }
    if event_type in {"dm.poll.vote.add", "dm.poll.vote.remove"}:
        answer_id = content.get("answer_id")
        return bool(
            set(content) == common | {"answer_id"}
            and isinstance(answer_id, int)
            and not isinstance(answer_id, bool)
            and 1 <= answer_id <= 10
        )
    finalized_at = content.get("finalized_at")
    if set(content) != common | {"finalized_at"} or not isinstance(finalized_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(finalized_at)
    except ValueError:
        return False
    return parsed.tzinfo is not None
