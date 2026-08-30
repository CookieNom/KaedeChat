from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from app.core.model_validation import UnambiguousInputModel
from app.core.types import EntityRef


class AllowedMentions(UnambiguousInputModel):
    """Discord-compatible notification policy for an application message."""

    model_config = ConfigDict(extra="forbid")

    parse: list[Literal["everyone", "users", "roles"]] = Field(
        default_factory=list,
        max_length=3,
    )
    users: list[EntityRef] = Field(default_factory=list, max_length=100)
    roles: list[EntityRef] = Field(default_factory=list, max_length=100)
    replied_user: bool = False

    @model_validator(mode="after")
    def unique_nonoverlapping_mentions(self) -> AllowedMentions:
        if len(self.parse) != len(set(self.parse)):
            raise ValueError("allowed mention parse values must be unique")
        if len(self.users) != len(set(self.users)) or len(self.roles) != len(set(self.roles)):
            raise ValueError("allowed mention IDs must be unique")
        if ("users" in self.parse and self.users) or ("roles" in self.parse and self.roles):
            raise ValueError("allowed mentions cannot parse and explicitly list the same type")
        return self


def regular_message_allowed_mentions(
    value: AllowedMentions | None,
) -> AllowedMentions:
    """Apply Discord's default for ordinary (non-webhook/interaction) messages."""

    return value if value is not None else AllowedMentions(parse=["everyone", "users", "roles"])


__all__ = ["AllowedMentions", "regular_message_allowed_mentions"]
