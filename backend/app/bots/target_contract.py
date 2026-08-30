from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, model_validator

from app.core.model_validation import UnambiguousInputModel
from app.core.settings import DOMAIN_RE
from app.core.types import MAX_SNOWFLAKE

APPLICATION_TARGET_EVENT = "bot.application.target.changed"


def _nonnegative_decimal(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("application target counts must be decimal strings")
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
        or int(value) > MAX_SNOWFLAKE
    ):
        raise ValueError("application target count is outside the supported range")
    return value


NonnegativeDecimal = Annotated[str, BeforeValidator(_nonnegative_decimal)]


def _positive_decimal(value: object) -> str:
    rendered = _nonnegative_decimal(value)
    if int(rendered) == 0:
        raise ValueError("application target identity and generation must be positive")
    return rendered


def _domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("application target domain must be a string")
    normalized = value.rstrip(".").lower()
    if normalized != value or not DOMAIN_RE.fullmatch(normalized):
        raise ValueError("application target domain must be canonical")
    return normalized


PositiveDecimal = Annotated[str, BeforeValidator(_positive_decimal)]
FederationDomain = Annotated[str, BeforeValidator(_domain)]


def target_policy_allows(
    policy: str,
    rules: Mapping[str, str],
    target_domain: str,
) -> bool:
    """Evaluate the application authority's per-instance admission policy."""

    if policy == "local_only" or rules.get(target_domain) == "deny":
        return False
    return policy != "allowlist" or rules.get(target_domain) == "allow"


class ApplicationTargetSnapshot(UnambiguousInputModel):
    """Roster-free target-presence aggregate signed by the target authority."""

    model_config = ConfigDict(extra="forbid")

    application_id: PositiveDecimal
    application_domain: FederationDomain
    bot_user_id: PositiveDecimal
    bot_user_domain: FederationDomain
    target_domain: FederationDomain
    generation: PositiveDecimal
    guild_installations: NonnegativeDecimal
    user_installations: NonnegativeDecimal

    @model_validator(mode="after")
    def coherent_authority(self) -> ApplicationTargetSnapshot:
        if self.application_domain != self.bot_user_domain:
            raise ValueError("application and bot identity authorities must match")
        return self

    @property
    def active(self) -> bool:
        return bool(int(self.guild_installations) or int(self.user_installations))


def authority_attested_application_target(
    event_type: str,
    content: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    """Recognize the sole bot event a target may sign for an app-home actor."""

    if event_type != APPLICATION_TARGET_EVENT or not isinstance(content, dict):
        return False
    try:
        snapshot = ApplicationTargetSnapshot.model_validate(content)
    except ValueError:
        return False
    actor_id, actor_domain = actor
    return (
        snapshot.target_domain == expected_authority
        and snapshot.bot_user_id == actor_id
        and snapshot.bot_user_domain == actor_domain
    )
