from __future__ import annotations

from dataclasses import dataclass
import re


MAX_SNOWFLAKE = (1 << 63) - 1
_SNOWFLAKE_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_FEDERATION_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def canonical_federation_domain(value: object) -> str:
    """Validate the canonical authority form shared by all SDK references."""

    if not isinstance(value, str) or _FEDERATION_DOMAIN_RE.fullmatch(value) is None:
        raise ValueError("a canonical instance domain is required")
    return value


def _canonical_snowflake(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Kaede entity IDs must be canonical positive snowflakes")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _SNOWFLAKE_RE.fullmatch(value) is not None:
        parsed = int(value)
    else:
        raise ValueError("Kaede entity IDs must be canonical positive snowflakes")
    if not 0 < parsed <= MAX_SNOWFLAKE:
        raise ValueError("Kaede entity ID is outside the signed BIGINT range")
    return parsed


def _bounded_wire_counter(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be a canonical decimal integer")
    rendered = str(value)
    if (
        not rendered.isascii()
        or not rendered.isdecimal()
        or (len(rendered) > 1 and rendered.startswith("0"))
    ):
        raise ValueError(f"{label} must be a canonical decimal integer")
    parsed = int(rendered)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} is outside its supported range")
    return parsed


@dataclass(frozen=True, slots=True)
class EntityRef:
    """An opaque Kaede snowflake paired with its authoritative instance."""

    id: int
    domain: str

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise ValueError("Kaede entity IDs must be integers after parsing")
        _canonical_snowflake(self.id)
        canonical_federation_domain(self.domain)

    @classmethod
    def from_wire(cls, identifier: object, domain: object) -> EntityRef:
        """Decode an exact JSON ID/domain pair without erasing wire evidence."""

        if not isinstance(identifier, str):
            raise ValueError("Kaede wire entity IDs must be decimal strings")
        return cls(
            _canonical_snowflake(identifier),
            canonical_federation_domain(domain),
        )

    @classmethod
    def parse(cls, value: object, *, default_domain: str | None = None) -> EntityRef:
        if not isinstance(value, str):
            raise ValueError("Kaede entity references must be strings")
        raw_id, separator, raw_domain = value.partition("@")
        domain = raw_domain if separator else default_domain
        return cls.from_wire(raw_id, domain)

    def __str__(self) -> str:
        return f"{self.id}@{self.domain}"


@dataclass(frozen=True, slots=True)
class User:
    ref: EntityRef
    username: str
    display_name: str | None = None
    bot: bool = False
    avatar_hash: str | None = None
    banner_hash: str | None = None
    bio: str | None = None
    custom_status: str | None = None
    profile_version: int = 1
    e2ee_device_generation: int = 0
    profile_resolved: bool = True
    account_type: str = "human"

    @property
    def handle(self) -> str:
        """The normal human-readable username form, independent of the snowflake."""
        return f"{self.username}@{self.ref.domain}"

    @property
    def name(self) -> str:
        return self.display_name or self.username

    @property
    def mention(self) -> str:
        return f"<@{self.ref}>"

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> User:
        raw_account_type = payload.get("account_type")
        raw_bot = payload.get("bot")
        if raw_account_type is None:
            if raw_bot is not None and not isinstance(raw_bot, bool):
                raise ValueError("user bot state must be a boolean")
            account_type = "bot" if raw_bot is True else "human"
        elif raw_account_type in {"human", "bot"}:
            account_type = str(raw_account_type)
        else:
            raise ValueError("user account type is invalid")
        if raw_bot is None:
            bot = account_type == "bot"
        elif isinstance(raw_bot, bool):
            bot = raw_bot
        else:
            raise ValueError("user bot state must be a boolean")
        if bot != (account_type == "bot"):
            raise ValueError("user bot state conflicts with its account type")
        profile_resolved = payload.get("profile_resolved", True)
        if not isinstance(profile_resolved, bool):
            raise ValueError("user profile resolution state must be a boolean")
        return cls(
            ref=EntityRef.from_wire(payload["id"], payload["origin_domain"]),
            username=str(payload["username"]),
            display_name=(
                str(payload["display_name"])
                if payload.get("display_name") is not None
                else None
            ),
            bot=bot,
            avatar_hash=(
                str(payload["avatar_hash"])
                if payload.get("avatar_hash") is not None
                else None
            ),
            banner_hash=(
                str(payload["banner_hash"])
                if payload.get("banner_hash") is not None
                else None
            ),
            bio=str(payload["bio"]) if payload.get("bio") is not None else None,
            custom_status=(
                str(payload["custom_status"])
                if payload.get("custom_status") is not None
                else None
            ),
            profile_version=_bounded_wire_counter(
                payload.get("profile_version", 1),
                label="user profile version",
                minimum=1,
                maximum=2_147_483_647,
            ),
            e2ee_device_generation=_bounded_wire_counter(
                payload.get("e2ee_device_generation", 0),
                label="user E2EE device generation",
                minimum=0,
                maximum=MAX_SNOWFLAKE,
            ),
            profile_resolved=profile_resolved,
            account_type=account_type,
        )
