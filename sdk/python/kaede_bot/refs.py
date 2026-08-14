from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntityRef:
    """An opaque Kaede snowflake paired with its authoritative instance."""

    id: int
    domain: str

    @classmethod
    def parse(cls, value: str, *, default_domain: str | None = None) -> "EntityRef":
        raw_id, separator, raw_domain = value.partition("@")
        if not raw_id.isdigit() or int(raw_id) < 0:
            raise ValueError("Kaede entity IDs are unsigned decimal snowflakes")
        domain = raw_domain if separator else default_domain
        if not domain or domain.endswith(".") or domain.lower() != domain:
            raise ValueError("a canonical instance domain is required")
        return cls(int(raw_id), domain)

    def __str__(self) -> str:
        return f"{self.id}@{self.domain}"


@dataclass(frozen=True, slots=True)
class User:
    ref: EntityRef
    username: str
    display_name: str | None = None
    bot: bool = False

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
    def from_payload(cls, payload: dict[str, object]) -> "User":
        return cls(
            EntityRef(int(str(payload["id"])), str(payload["origin_domain"])),
            str(payload["username"]),
            str(payload["display_name"])
            if payload.get("display_name") is not None
            else None,
            bool(payload.get("bot", False)),
        )
