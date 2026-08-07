from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.chat.schemas import ProfilePatch
from app.federation.relationships import (
    acceptance_matches,
    apply_relationship_event,
    queue_friend_profile_updates,
)
from app.federation.schemas import EventEnvelope, RelationshipEventContent, RemoteUserProfile
from app.federation.users import split_handle


def test_federated_handle_accepts_display_and_wire_forms() -> None:
    assert split_handle("turtle@example.test") == ("turtle", "example.test")
    assert split_handle("@Turtle@Example.Test") == ("turtle", "example.test")


def test_profile_patch_trims_text_and_allows_explicit_clearing() -> None:
    patch = ProfilePatch(
        display_name="  Maple  ",
        bio="  A quiet profile.  ",
        custom_status="   ",
    )
    assert patch.display_name == "Maple"
    assert patch.bio == "A quiet profile."
    assert patch.custom_status is None


def test_profile_patch_requires_a_field_and_bounds_public_text() -> None:
    with pytest.raises(ValidationError):
        ProfilePatch()
    with pytest.raises(ValidationError):
        ProfilePatch(custom_status="x" * 129)
    with pytest.raises(ValidationError):
        ProfilePatch(bio="x\x00y")


def test_federated_profile_carries_all_mutable_versioned_fields() -> None:
    profile = RemoteUserProfile(
        id="42",
        origin_domain="remote.example",
        username="maple",
        display_name="Maple",
        avatar_hash="avatar",
        banner_hash="banner",
        bio="About Maple",
        custom_status="Out walking",
        profile_version=7,
    )
    assert profile.profile_version == 7
    assert profile.bio == "About Maple"
    assert profile.custom_status == "Out walking"


def test_relationship_event_requires_bounded_correlation_token() -> None:
    with pytest.raises(ValidationError):
        RelationshipEventContent.model_validate(
            {
                "actor": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "maple",
                },
                "target": {"id": "7", "domain": "local.example"},
                "request_id": "guessable",
            }
        )


def test_stale_acceptance_cannot_resurrect_local_relationship_state() -> None:
    request_id = "kcr_abcdefghijklmnopqrstuvwxyz"
    assert acceptance_matches("pending_out", request_id, request_id)
    assert not acceptance_matches(None, None, request_id)
    assert not acceptance_matches("blocked", None, request_id)
    assert not acceptance_matches("pending_out", request_id, f"{request_id}2")


@pytest.mark.asyncio
async def test_profile_update_is_ignored_after_friendship_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipient = SimpleNamespace(id=7, origin_domain="local.example", is_local=True)
    actor = SimpleNamespace(id=42, origin_domain="remote.example", is_local=False)

    class FakeSession:
        async def get(self, _model: object, identity: object) -> object | None:
            return recipient if identity == (7, "local.example") else actor

    monkeypatch.setattr(
        "app.federation.relationships.lock_relationship_pair", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.federation.relationships.relationship", AsyncMock(return_value=None))
    upsert = AsyncMock()
    monkeypatch.setattr("app.federation.relationships.upsert_remote_user", upsert)
    envelope = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_abcdefghijklmnop",
            "origin": "remote.example",
            "type": "relationship.profile",
            "ts": 1,
            "actor": {"id": "42", "domain": "remote.example"},
            "content": {
                "actor": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "maple",
                    "avatar_hash": "a" * 64,
                    "profile_version": 2,
                },
                "target": {"id": "7", "domain": "local.example"},
            },
            "signatures": {"remote.example": {"ed25519:test": "signature"}},
        }
    )

    result = await apply_relationship_event(
        cast(Any, FakeSession()),
        cast(Any, SimpleNamespace(domain="local.example")),
        envelope,
    )

    assert result.relation_type is None
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_updates_are_queued_only_for_remote_friends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rows:
        def all(self) -> list[tuple[int, str]]:
            return [(42, "remote.example"), (43, "remote.example")]

    class FakeSession:
        async def execute(self, _statement: object) -> Rows:
            return Rows()

    actor = SimpleNamespace(
        id=7,
        origin_domain="local.example",
        username="maple",
        display_name="Maple",
        avatar_hash="a" * 64,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=3,
    )
    build = AsyncMock(side_effect=lambda *_args, **_kwargs: {"event_id": "test"})
    queue = AsyncMock()
    monkeypatch.setattr("app.federation.relationships.build_envelope", build)
    monkeypatch.setattr("app.federation.relationships.queue_event", queue)

    destinations = await queue_friend_profile_updates(
        cast(Any, FakeSession()),
        cast(Any, SimpleNamespace(domain="local.example")),
        cast(Any, actor),
    )

    assert destinations == {"remote.example"}
    assert build.await_count == 2
    assert queue.await_count == 2
