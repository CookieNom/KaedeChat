from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, TypedDict

from .embeds import PartialEmoji, _boolean, _text
from .refs import EntityRef


@dataclass(frozen=True, slots=True)
class PollMedia:
    text: str | None = None
    emoji: PartialEmoji | None = None

    def __post_init__(self) -> None:
        _text(
            self.text,
            name="poll text",
            minimum=1,
            maximum=300,
            meaningful=True,
        )
        if self.emoji is not None and not isinstance(self.emoji, PartialEmoji):
            raise TypeError("poll emoji must be PartialEmoji")
        if self.text is None and self.emoji is None:
            raise ValueError("poll media requires text or emoji")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.text is not None:
            payload["text"] = self.text
        if self.emoji is not None:
            payload["emoji"] = self.emoji.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class PollAnswer:
    poll_media: PollMedia

    def __post_init__(self) -> None:
        if not isinstance(self.poll_media, PollMedia):
            raise TypeError("poll answer media must be PollMedia")
        if self.poll_media.text is not None and len(self.poll_media.text) > 55:
            raise ValueError("poll answer text cannot exceed 55 characters")

    def to_dict(self) -> dict[str, object]:
        return {"poll_media": self.poll_media.to_dict()}


@dataclass(frozen=True, slots=True)
class Poll:
    question: PollMedia
    answers: Sequence[PollAnswer]
    duration: int
    allow_multiselect: bool = False
    layout_type: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "answers", tuple(self.answers))
        if not isinstance(self.question, PollMedia):
            raise TypeError("poll question must be PollMedia")
        if self.question.text is None or self.question.emoji is not None:
            raise ValueError("a poll question requires text and cannot contain emoji")
        if not 2 <= len(self.answers) <= 10:
            raise ValueError("a poll requires between 2 and 10 answers")
        if not all(isinstance(answer, PollAnswer) for answer in self.answers):
            raise TypeError("poll answers must be PollAnswer instances")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int):
            raise TypeError("poll duration must be an integer")
        if not 1 <= self.duration <= 768:
            raise ValueError("poll duration must be between 1 and 768 hours")
        _boolean(self.allow_multiselect, name="poll allow_multiselect")
        if isinstance(self.layout_type, bool) or not isinstance(self.layout_type, int):
            raise TypeError("poll layout_type must be an integer")
        if self.layout_type != 1:
            raise ValueError("only Discord's default poll layout is supported")

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question.to_dict(),
            "answers": [answer.to_dict() for answer in self.answers],
            "duration": self.duration,
            "allow_multiselect": self.allow_multiselect,
            "layout_type": self.layout_type,
        }


@dataclass(frozen=True, slots=True)
class PollResultAnswerCount:
    id: int
    count: int


@dataclass(frozen=True, slots=True)
class PollResult:
    """Discord type-46 result metadata.

    ``total_votes`` is the total number of selected answers. For a
    multi-select poll it can therefore exceed the number of voters.
    """

    poll_message_ref: EntityRef
    source_encryption_mode: Literal["plaintext", "e2ee"]
    answer_counts: tuple[PollResultAnswerCount, ...]
    total_votes: int
    victor_answer_id: int | None
    victor_answer_votes: int
    question_text: str | None = None
    victor_answer_text: str | None = None
    victor_answer_emoji_id: str | None = None
    victor_answer_emoji_name: str | None = None
    victor_answer_emoji_animated: bool | None = None

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        referenced_message_ref: EntityRef | None,
        embeds: object,
    ) -> PollResult:
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "poll_message_ref",
            "source_encryption_mode",
            "answer_counts",
            "total_votes",
            "victor_answer_id",
            "victor_answer_votes",
        }:
            raise ValueError("poll result projection has an invalid shape")
        if _strict_int(payload["version"], "poll result version") != 1:
            raise ValueError("poll result version is unsupported")
        raw_ref = payload["poll_message_ref"]
        if not isinstance(raw_ref, str):
            raise ValueError("poll result source reference is invalid")
        poll_message_ref = EntityRef.parse(raw_ref)
        if (
            poll_message_ref.id <= 0
            or str(poll_message_ref) != raw_ref
            or poll_message_ref.domain is None
            or poll_message_ref != referenced_message_ref
        ):
            raise ValueError("poll result source reference is inconsistent")
        source_mode = payload["source_encryption_mode"]
        if source_mode not in {"plaintext", "e2ee"}:
            raise ValueError("poll result encryption mode is invalid")
        raw_counts = payload["answer_counts"]
        if not isinstance(raw_counts, list) or not 1 <= len(raw_counts) <= 10:
            raise ValueError("poll result answer counts are invalid")
        counts: list[PollResultAnswerCount] = []
        for raw_count in raw_counts:
            if not isinstance(raw_count, dict) or set(raw_count) != {"id", "count"}:
                raise ValueError("poll result answer count is invalid")
            answer_id = _strict_int(raw_count["id"], "poll result answer ID")
            count = _strict_int(raw_count["count"], "poll result answer count")
            if not 1 <= answer_id <= 10 or count < 0:
                raise ValueError("poll result answer count is out of range")
            counts.append(PollResultAnswerCount(answer_id, count))
        identifiers = [item.id for item in counts]
        if identifiers != sorted(set(identifiers)):
            raise ValueError("poll result answer IDs must be sorted and unique")
        total_votes = _strict_int(payload["total_votes"], "poll result total")
        victor_votes = _strict_int(
            payload["victor_answer_votes"], "poll result victor votes"
        )
        raw_victor = payload["victor_answer_id"]
        victor_id = (
            None
            if raw_victor is None
            else _strict_int(raw_victor, "poll result victor answer ID")
        )
        highest = max(item.count for item in counts)
        winners = [item.id for item in counts if item.count == highest]
        expected_victor = winners[0] if highest > 0 and len(winners) == 1 else None
        if (
            total_votes != sum(item.count for item in counts)
            or victor_votes != highest
            or victor_id != expected_victor
        ):
            raise ValueError("poll result vote totals are inconsistent")
        labels = _poll_result_embed_labels(
            embeds,
            total_votes=total_votes,
            victor_id=victor_id,
            victor_votes=victor_votes,
            encrypted=source_mode == "e2ee",
        )
        return cls(
            poll_message_ref=poll_message_ref,
            source_encryption_mode=source_mode,
            answer_counts=tuple(counts),
            total_votes=total_votes,
            victor_answer_id=victor_id,
            victor_answer_votes=victor_votes,
            **labels,
        )

    def with_verified_poll(self, poll: Mapping[str, object]) -> PollResult:
        """Resolve encrypted labels only from a separately verified source poll."""

        if self.source_encryption_mode != "e2ee":
            return self
        question = poll.get("question")
        question_text = question.get("text") if isinstance(question, Mapping) else None
        answer_text: str | None = None
        emoji_id: str | None = None
        emoji_name: str | None = None
        emoji_animated: bool | None = None
        answers = poll.get("answers")
        if self.victor_answer_id is not None and isinstance(answers, Sequence):
            for raw_answer in answers:
                if not isinstance(raw_answer, Mapping):
                    continue
                answer_id = raw_answer.get("answer_id")
                if answer_id != self.victor_answer_id:
                    continue
                media = raw_answer.get("poll_media")
                if not isinstance(media, Mapping):
                    break
                raw_text = media.get("text")
                answer_text = raw_text if isinstance(raw_text, str) else None
                emoji = media.get("emoji")
                if isinstance(emoji, Mapping):
                    raw_emoji_id = emoji.get("id")
                    emoji_id = str(raw_emoji_id) if raw_emoji_id is not None else None
                    raw_name = emoji.get("name")
                    emoji_name = raw_name if isinstance(raw_name, str) else None
                    raw_animated = emoji.get("animated")
                    emoji_animated = (
                        raw_animated if isinstance(raw_animated, bool) else None
                    )
                break
        return replace(
            self,
            question_text=question_text if isinstance(question_text, str) else None,
            victor_answer_text=answer_text,
            victor_answer_emoji_id=emoji_id,
            victor_answer_emoji_name=emoji_name,
            victor_answer_emoji_animated=emoji_animated,
        )


class _PollResultLabels(TypedDict):
    question_text: str | None
    victor_answer_text: str | None
    victor_answer_emoji_id: str | None
    victor_answer_emoji_name: str | None
    victor_answer_emoji_animated: bool | None


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _poll_result_embed_labels(
    embeds: object,
    *,
    total_votes: int,
    victor_id: int | None,
    victor_votes: int,
    encrypted: bool,
) -> _PollResultLabels:
    if not isinstance(embeds, list) or len(embeds) != 1:
        raise ValueError("poll result message requires one result embed")
    embed = embeds[0]
    if not isinstance(embed, dict) or set(embed) != {"type", "fields"}:
        raise ValueError("poll result embed has an invalid shape")
    if embed.get("type") != "poll_result" or not isinstance(embed.get("fields"), list):
        raise ValueError("poll result embed has an invalid type")
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
    fields: dict[str, str] = {}
    for raw_field in embed["fields"]:
        if (
            not isinstance(raw_field, dict)
            or set(raw_field) != {"name", "value", "inline"}
            or raw_field.get("inline") is not False
            or not isinstance(raw_field.get("name"), str)
            or raw_field["name"] not in allowed
            or raw_field["name"] in fields
            or not isinstance(raw_field.get("value"), str)
        ):
            raise ValueError("poll result embed field is invalid")
        fields[raw_field["name"]] = raw_field["value"]
    if (
        fields.get("victor_answer_votes") != str(victor_votes)
        or fields.get("total_votes") != str(total_votes)
        or fields.get("victor_answer_id")
        != (str(victor_id) if victor_id is not None else None)
    ):
        raise ValueError("poll result embed vote totals are inconsistent")
    private = {
        "poll_question_text",
        "victor_answer_text",
        "victor_answer_emoji_id",
        "victor_answer_emoji_name",
        "victor_answer_emoji_animated",
    }
    if encrypted and private & fields.keys():
        raise ValueError("encrypted poll result embed leaks private labels")
    if victor_id is None and (private - {"poll_question_text"}) & fields.keys():
        raise ValueError("poll result victor labels require a victor")
    animated = fields.get("victor_answer_emoji_animated")
    if animated not in {None, "true", "false"}:
        raise ValueError("poll result emoji animation marker is invalid")
    return {
        "question_text": fields.get("poll_question_text"),
        "victor_answer_text": fields.get("victor_answer_text"),
        "victor_answer_emoji_id": fields.get("victor_answer_emoji_id"),
        "victor_answer_emoji_name": fields.get("victor_answer_emoji_name"),
        "victor_answer_emoji_animated": (
            animated == "true" if animated is not None else None
        ),
    }


__all__ = [
    "Poll",
    "PollAnswer",
    "PollMedia",
    "PollResult",
    "PollResultAnswerCount",
]
