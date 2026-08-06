from __future__ import annotations

import re

from app.core.settings import DOMAIN_RE
from app.core.types import MAX_SNOWFLAKE

ROOM_RE = re.compile(r"^(?P<kind>[gd])\.(?P<scope>[0-9]+)\.(?P<leaf>[0-9]+)$")
IDENTITY_RE = re.compile(r"^(?P<user>[0-9]+)@(?P<domain>[^@]+)$")


def _snowflake_component(value: int) -> str:
    if not 0 <= value <= MAX_SNOWFLAKE:
        raise ValueError("room identifiers must be PostgreSQL snowflakes")
    return str(value)


def guild_room_name(guild_id: int, channel_id: int) -> str:
    return f"g.{_snowflake_component(guild_id)}.{_snowflake_component(channel_id)}"


def dm_room_name(channel_id: int, call_id: int) -> str:
    return f"d.{_snowflake_component(channel_id)}.{_snowflake_component(call_id)}"


def participant_identity(user_id: int, domain: str) -> str:
    normalized = domain.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(normalized):
        raise ValueError("invalid participant domain")
    return f"{_snowflake_component(user_id)}@{normalized}"


def parse_participant_identity(identity: str) -> tuple[int, str]:
    match = IDENTITY_RE.fullmatch(identity)
    if match is None:
        raise ValueError("invalid LiveKit participant identity")
    user_id = int(match.group("user"))
    domain = match.group("domain").lower()
    if str(user_id) != match.group("user") or user_id > MAX_SNOWFLAKE:
        raise ValueError("invalid LiveKit participant identity")
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("invalid LiveKit participant identity")
    return user_id, domain


def parse_room_name(room: str) -> tuple[str, int, int]:
    match = ROOM_RE.fullmatch(room)
    if match is None:
        raise ValueError("invalid Kaede LiveKit room")
    scope = int(match.group("scope"))
    leaf = int(match.group("leaf"))
    if str(scope) != match.group("scope") or str(leaf) != match.group("leaf"):
        raise ValueError("invalid Kaede LiveKit room")
    if scope > MAX_SNOWFLAKE or leaf > MAX_SNOWFLAKE:
        raise ValueError("invalid Kaede LiveKit room")
    return match.group("kind"), scope, leaf
