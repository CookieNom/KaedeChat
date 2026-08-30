from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .refs import EntityRef
from .wire import strict_payload_int, strict_payload_string


def _ref_list(value: object, label: str) -> tuple[EntityRef, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    refs = tuple(EntityRef.parse(item) for item in value)
    if len(refs) != len(set(refs)):
        raise ValueError(f"{label} contains duplicates")
    return refs


@dataclass(frozen=True, slots=True)
class ModerationFailure:
    user_ref: EntityRef
    code: str
    message: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModerationFailure:
        if set(payload) != {"user_id", "code", "message"}:
            raise ValueError("moderation failure response is invalid")
        code = strict_payload_string(payload["code"], "moderation failure code")
        message = strict_payload_string(
            payload["message"], "moderation failure message"
        )
        if not 1 <= len(code) <= 128 or not 1 <= len(message) <= 1_000:
            raise ValueError("moderation failure response is invalid")
        return cls(
            user_ref=EntityRef.parse(payload["user_id"]),
            code=code,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class PruneEstimate:
    pruned: int
    days: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PruneEstimate:
        if set(payload) != {"pruned", "days"}:
            raise ValueError("prune estimate response is invalid")
        return cls(
            pruned=strict_payload_int(
                payload["pruned"],
                "prune estimate count",
                minimum=0,
                maximum=(1 << 63) - 1,
            ),
            days=strict_payload_int(
                payload["days"], "prune estimate days", minimum=1, maximum=30
            ),
        )


@dataclass(frozen=True, slots=True)
class PruneResult:
    guild_ref: EntityRef
    pruned: int | None
    pruned_users: tuple[EntityRef, ...]
    failed_users: tuple[ModerationFailure, ...]
    days: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PruneResult:
        required = {
            "guild_id",
            "guild_domain",
            "pruned",
            "pruned_user_ids",
            "failed_users",
            "days",
        }
        if set(payload) != required:
            raise ValueError("prune result response is invalid")
        pruned_users = _ref_list(payload["pruned_user_ids"], "pruned users")
        raw_failures = payload["failed_users"]
        if not isinstance(raw_failures, list) or any(
            not isinstance(item, dict) for item in raw_failures
        ):
            raise ValueError("prune failures must be a list")
        failures = tuple(ModerationFailure.from_payload(item) for item in raw_failures)
        failed_refs = tuple(failure.user_ref for failure in failures)
        if len(failed_refs) != len(set(failed_refs)):
            raise ValueError("prune failures contain duplicates")
        if set(pruned_users) & set(failed_refs):
            raise ValueError("pruned and failed users overlap")
        raw_pruned = payload["pruned"]
        pruned = (
            None
            if raw_pruned is None
            else strict_payload_int(
                raw_pruned,
                "prune result count",
                minimum=0,
                maximum=(1 << 63) - 1,
            )
        )
        if pruned is not None and pruned != len(pruned_users):
            raise ValueError("prune result count conflicts with its user partition")
        return cls(
            guild_ref=EntityRef.from_wire(payload["guild_id"], payload["guild_domain"]),
            pruned=pruned,
            pruned_users=pruned_users,
            failed_users=failures,
            days=strict_payload_int(
                payload["days"], "prune result days", minimum=1, maximum=30
            ),
        )


@dataclass(frozen=True, slots=True)
class BulkBanResult:
    banned_users: tuple[EntityRef, ...]
    failed_users: tuple[EntityRef, ...]
    failed_user_details: tuple[ModerationFailure, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BulkBanResult:
        if set(payload) != {
            "banned_users",
            "failed_users",
            "failed_user_details",
        }:
            raise ValueError("bulk ban response is invalid")
        banned = _ref_list(payload["banned_users"], "banned users")
        failed = _ref_list(payload["failed_users"], "failed users")
        raw_details = payload["failed_user_details"]
        if not isinstance(raw_details, list) or any(
            not isinstance(item, dict) for item in raw_details
        ):
            raise ValueError("bulk ban failure details must be a list")
        details = tuple(ModerationFailure.from_payload(item) for item in raw_details)
        if tuple(detail.user_ref for detail in details) != failed:
            raise ValueError("bulk ban failure details conflict with failed users")
        if set(banned) & set(failed):
            raise ValueError("banned and failed users overlap")
        return cls(
            banned_users=banned,
            failed_users=failed,
            failed_user_details=details,
        )


__all__ = ["BulkBanResult", "ModerationFailure", "PruneEstimate", "PruneResult"]
