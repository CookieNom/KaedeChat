from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kaede_bot import Message


FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "static"
    / "protocol"
    / "poll-result-v1.json"
)


def result_payload(*, mode: str = "plaintext") -> dict[str, Any]:
    fields = [
        {"name": "victor_answer_votes", "value": "3", "inline": False},
        {"name": "total_votes", "value": "4", "inline": False},
        {"name": "victor_answer_id", "value": "1", "inline": False},
    ]
    if mode == "plaintext":
        fields[:0] = [
            {"name": "poll_question_text", "value": "Ship it?", "inline": False}
        ]
        fields.append({"name": "victor_answer_text", "value": "Yes", "inline": False})
    return {
        "id": "124",
        "origin_domain": "polls.example",
        "channel_id": "77",
        "channel_domain": "polls.example",
        "message_type": 46,
        "referenced_message_id": "123",
        "referenced_message_domain": "polls.example",
        "poll_result": {
            "version": 1,
            "poll_message_ref": "123@polls.example",
            "source_encryption_mode": mode,
            "answer_counts": [{"id": 1, "count": 3}, {"id": 2, "count": 1}],
            "total_votes": 4,
            "victor_answer_id": 1,
            "victor_answer_votes": 3,
        },
        "embeds": [{"type": "poll_result", "fields": fields}],
        "attachments": [],
        "components": [],
        "sticker_items": [],
        "flags": 0,
    }


def parse_message(payload: dict[str, Any]) -> Message:
    return Message.from_payload(cast(Any, SimpleNamespace()), "polls.example", payload)


def test_sdk_retains_plaintext_poll_result_labels() -> None:
    result = parse_message(result_payload()).poll_result

    assert result is not None
    assert result.total_votes == 4
    assert result.question_text == "Ship it?"
    assert result.victor_answer_text == "Yes"


def test_sdk_derives_encrypted_labels_only_from_verified_referenced_poll() -> None:
    payload = result_payload(mode="e2ee")
    payload["referenced_message"] = {
        "id": "123",
        "origin_domain": "polls.example",
        "channel_id": "77",
        "channel_domain": "polls.example",
        "poll": {
            "question": {"text": "Secret question"},
            "answers": [
                {"answer_id": 1, "poll_media": {"text": "Secret winner"}},
                {"answer_id": 2, "poll_media": {"text": "Other"}},
            ],
        },
        "embeds": [],
        "attachments": [],
    }

    result = parse_message(payload).poll_result

    assert result is not None
    assert result.question_text == "Secret question"
    assert result.victor_answer_text == "Secret winner"


def test_sdk_keeps_encrypted_labels_unavailable_without_verified_source() -> None:
    result = parse_message(result_payload(mode="e2ee")).poll_result

    assert result is not None
    assert result.question_text is None
    assert result.victor_answer_text is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("message_type",), True),
        (("poll_result", "total_votes"), True),
        (("poll_result", "poll_message_ref"), "0123@polls.example"),
        (("poll_result", "source_encryption_mode"), "e2ee"),
    ],
)
def test_sdk_rejects_tampered_poll_results(
    path: tuple[str, ...], value: object
) -> None:
    payload = result_payload()
    target: dict[str, Any] = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValueError):
        parse_message(payload)


def test_sdk_rejects_private_labels_on_encrypted_result() -> None:
    payload = result_payload(mode="e2ee")
    tampered = copy.deepcopy(payload)
    tampered["embeds"][0]["fields"].append(
        {"name": "poll_question_text", "value": "secret", "inline": False}
    )

    with pytest.raises(ValueError, match="leaks private labels"):
        parse_message(tampered)


def test_sdk_consumes_shared_poll_result_vectors() -> None:
    fixture = json.loads(FIXTURE.read_text())
    for vector in fixture["vectors"]:
        message = vector["message"]
        raw_id = str(message["id"]).split("@", 1)[0]
        parsed = parse_message({**message, "id": raw_id})
        assert parsed.poll_result is not None
        assert parsed.poll_result.poll_message_ref == parsed.referenced_message_ref
