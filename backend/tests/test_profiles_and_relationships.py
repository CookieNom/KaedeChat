from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.chat.schemas import ProfilePatch
from app.federation.relationships import acceptance_matches
from app.federation.schemas import RelationshipEventContent, RemoteUserProfile
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
