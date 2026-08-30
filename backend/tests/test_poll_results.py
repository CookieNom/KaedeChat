from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.chat.poll_results import (
    authority_attested_direct_poll_result,
    authority_attested_dm_poll_mutation,
    build_poll_result_projection,
    poll_result_embed,
    validate_poll_result_embed,
    validate_poll_result_projection,
    validate_poll_result_wire_body,
)
from app.db.models import Message

SOURCE_REF = (123, "polls.example")
AUTHOR_REF = (42, "member.example")
CHANNEL_REF = (77, "polls.example")
FIXTURE = (
    Path(__file__).resolve().parents[2] / "frontend" / "static" / "protocol" / "poll-result-v1.json"
)


def projection(
    counts: list[tuple[int, int]] | None = None,
    *,
    mode: str = "plaintext",
) -> dict[str, object]:
    return build_poll_result_projection(
        source_ref=SOURCE_REF,
        answer_counts=counts or [(1, 3), (2, 1)],
        source_encryption_mode=mode,  # type: ignore[arg-type]
    )


def wire_message(*, mode: str = "plaintext") -> dict[str, object]:
    result = projection(mode=mode)
    embed = poll_result_embed(
        result,
        question=({"encrypted": True, "version": 1} if mode == "e2ee" else {"text": "Ship it?"}),
        answers=[(1, "Yes", None), (2, "No", None)],
    )
    return {
        "id": "124",
        "origin_domain": "polls.example",
        "channel_id": str(CHANNEL_REF[0]),
        "channel_domain": CHANNEL_REF[1],
        "author_id": str(AUTHOR_REF[0]),
        "author_domain": AUTHOR_REF[1],
        "content": None,
        "e2ee": None,
        "embeds": [embed],
        "components": [],
        "sticker_items": [],
        "application_id": None,
        "application_domain": None,
        "interaction_metadata": None,
        "view_version": 0,
        "view_persistent": False,
        "view_expires_at": None,
        "interaction_integration_type": None,
        "interaction_installation_ref": None,
        "interaction_installation_revision": None,
        "forwarded_message_id": None,
        "forwarded_message_domain": None,
        "forwarded_channel_id": None,
        "forwarded_channel_domain": None,
        "forward_snapshot": None,
        "message_snapshots": [],
        "poll": None,
        "poll_result": result,
        "message_type": 46,
        "tts": False,
        "flags": 0,
        "referenced_message_id": str(SOURCE_REF[0]),
        "referenced_message_domain": SOURCE_REF[1],
        "mention_user_refs": [{"id": str(AUTHOR_REF[0]), "origin_domain": AUTHOR_REF[1]}],
        "attachments": [],
        "webhook_id": None,
        "webhook": None,
    }


def test_poll_result_projection_uses_selection_count_for_multiselect() -> None:
    result = projection([(1, 4), (2, 2), (3, 1)])

    assert result["total_votes"] == 7
    assert result["victor_answer_id"] == 1
    assert result["victor_answer_votes"] == 4


@pytest.mark.parametrize(
    ("counts", "victor_id", "victor_votes"),
    [
        ([(1, 2), (2, 2)], None, 2),
        ([(1, 0), (2, 0)], None, 0),
    ],
)
def test_poll_result_projection_omits_victor_for_tie_or_no_votes(
    counts: list[tuple[int, int]],
    victor_id: int | None,
    victor_votes: int,
) -> None:
    result = projection(counts)

    assert result["victor_answer_id"] is victor_id
    assert result["victor_answer_votes"] == victor_votes


@pytest.mark.parametrize(
    "source_ref",
    ["0@polls.example", "001@polls.example", "123@Polls.example", "123@bad_domain"],
)
def test_poll_result_projection_rejects_noncanonical_source_ref(source_ref: str) -> None:
    value = projection()
    value["poll_message_ref"] = source_ref

    with pytest.raises(ValidationError):
        validate_poll_result_projection(value)


def test_poll_result_embed_is_canonical_and_requires_inline_false() -> None:
    result = projection()
    embed = poll_result_embed(
        result,
        question={"text": "Ship it?"},
        answers=[(1, "Yes", None), (2, "No", None)],
    )

    assert all(field["inline"] is False for field in embed["fields"])  # type: ignore[index]
    assert validate_poll_result_embed(embed, projection=result) == embed
    tampered = copy.deepcopy(embed)
    del tampered["fields"][0]["inline"]  # type: ignore[index]
    with pytest.raises(ValueError, match="field is invalid"):
        validate_poll_result_embed(tampered, projection=result)


def test_encrypted_poll_result_wire_is_label_free_and_exact() -> None:
    raw = wire_message(mode="e2ee")

    result, embed = validate_poll_result_wire_body(
        raw,
        author_ref=AUTHOR_REF,
        channel_ref=CHANNEL_REF,
        source_ref=SOURCE_REF,
    )

    assert result["source_encryption_mode"] == "e2ee"
    assert {field["name"] for field in embed["fields"]} == {  # type: ignore[index]
        "victor_answer_votes",
        "total_votes",
        "victor_answer_id",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", "leak"),
        ("e2ee", {"ciphertext": "opaque"}),
        ("attachments", [{"id": "9"}]),
        ("components", [{"type": 1}]),
        ("sticker_items", [{"id": "9"}]),
        ("poll", {"question": {"text": "leak"}}),
        ("forwarded_message_id", "9"),
        ("tts", True),
        ("flags", 4),
        ("mention_user_refs", []),
    ],
)
def test_poll_result_wire_rejects_hostile_body_fields(field: str, value: object) -> None:
    raw = wire_message(mode="e2ee")
    raw[field] = value

    with pytest.raises(ValueError, match="canonical system projection"):
        validate_poll_result_wire_body(
            raw,
            author_ref=AUTHOR_REF,
            channel_ref=CHANNEL_REF,
            source_ref=SOURCE_REF,
        )


def test_encrypted_poll_result_wire_rejects_private_embed_labels() -> None:
    raw = wire_message(mode="e2ee")
    raw["embeds"][0]["fields"].append(  # type: ignore[index]
        {"name": "poll_question_text", "value": "secret", "inline": False}
    )

    with pytest.raises(ValueError, match="leaks private labels"):
        validate_poll_result_wire_body(
            raw,
            author_ref=AUTHOR_REF,
            channel_ref=CHANNEL_REF,
            source_ref=SOURCE_REF,
        )


def test_direct_dm_poll_result_authority_attestation_is_narrow() -> None:
    raw = wire_message(mode="e2ee")
    content = {
        "message": raw,
        "author": {"id": str(AUTHOR_REF[0]), "origin_domain": AUTHOR_REF[1]},
    }

    assert authority_attested_direct_poll_result(
        "dm.message.create",
        content,
        expected_authority="polls.example",
        actor=(str(AUTHOR_REF[0]), AUTHOR_REF[1]),
    )
    raw["content"] = "forged"
    assert not authority_attested_direct_poll_result(
        "dm.message.create",
        content,
        expected_authority="polls.example",
        actor=(str(AUTHOR_REF[0]), AUTHOR_REF[1]),
    )


def test_dm_poll_mutation_authority_attestation_is_exact() -> None:
    context = {"conversation_id": "77", "conversation_domain": "polls.example"}
    vote = {
        "message_id": "123",
        "message_domain": "member.example",
        "channel_id": "77",
        "channel_domain": "polls.example",
        "answer_id": 2,
    }
    assert authority_attested_dm_poll_mutation(
        "dm.poll.vote.add",
        vote,
        context,
        expected_authority="polls.example",
    )
    vote["answer_id"] = True
    assert not authority_attested_dm_poll_mutation(
        "dm.poll.vote.add",
        vote,
        context,
        expected_authority="polls.example",
    )


def test_shared_poll_result_vectors_are_backend_canonical() -> None:
    fixture = json.loads(FIXTURE.read_text())
    for vector in fixture["vectors"]:
        message = vector["message"]
        source_ref = (
            int(message["referenced_message_id"]),
            message["referenced_message_domain"],
        )
        result = validate_poll_result_projection(
            message["poll_result"],
            source_ref=source_ref,
        )
        assert validate_poll_result_embed(message["embeds"][0], projection=result)


def test_message_schema_pins_poll_result_type_and_reference() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in Message.__table__.constraints
        if hasattr(constraint, "sqltext")
    }

    assert "poll_result" in Message.__table__.columns
    matches_type = next(
        sql
        for name, sql in constraints.items()
        if name.endswith("poll_result_matches_message_type")
    )
    has_reference = next(
        sql for name, sql in constraints.items() if name.endswith("poll_result_has_reference")
    )
    assert matches_type == ("(message_type = 46) = (poll_result IS NOT NULL)")
    assert has_reference == ("message_type <> 46 OR referenced_message_id IS NOT NULL")


def test_foundation_migration_guards_poll_result_downgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()

    assert 'sa.Column("poll_result", postgresql.JSONB())' in migration
    assert "OR poll_result IS NOT NULL" in migration
    assert '"poll_result",' in migration
