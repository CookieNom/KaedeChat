from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, JsonValue

from app.core.model_validation import UnambiguousInputModel
from app.db.models import AuditLogEntry

REDACTED_AUDIT_VALUE = "[redacted]"
_SENSITIVE_KEY_PARTS = (
    "access_key",
    "authorization",
    "credential",
    "encryption_key",
    "object_key",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "signature",
    "signing_key",
    "webhook_token",
)


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized == "token"
        or normalized.endswith(("_token", "_api_key"))
        or normalized.startswith("token_")
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
    )


def redact_audit_value(value: Any, *, key: str | None = None) -> JsonValue:
    """Return a JSON-safe audit value with credentials removed recursively."""

    if key is not None and _sensitive_key(key):
        return REDACTED_AUDIT_VALUE
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(child_key): redact_audit_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_audit_value(item) for item in value]
    # Audit rows are JSONB today. This defensive conversion keeps old/imported
    # rows serializable without exposing an object's representation.
    return str(value)


class AuditLogChangePayload(UnambiguousInputModel):
    model_config = ConfigDict(extra="allow")

    key: str = Field(min_length=1, max_length=128)
    old_value: JsonValue | None = None
    new_value: JsonValue | None = None
    added: JsonValue | None = None
    removed: JsonValue | None = None


class AuditLogEntryPayload(UnambiguousInputModel):
    id: str
    guild_id: str
    guild_domain: str
    actor_id: str
    actor_domain: str
    action_type: int
    target_type: str | None
    target_ref: dict[str, JsonValue] | None
    reason: str | None
    changes: list[AuditLogChangePayload]
    created_at: datetime


def _audit_changes(value: JsonValue) -> list[AuditLogChangePayload]:
    if not isinstance(value, list):
        return []
    changes: list[AuditLogChangePayload] = []
    for index, item in enumerate(value):
        normalized: dict[str, JsonValue] = (
            dict(item) if isinstance(item, dict) else {"new_value": item}
        )
        raw_key = normalized.get("key")
        key = str(raw_key).strip() if raw_key is not None else ""
        change_key = key[:128] or f"change_{index + 1}"
        normalized["key"] = change_key
        # A change record names the mutated field in ``key``; its old/new
        # values otherwise sit under generic JSON keys that cannot reveal
        # whether they contain a credential.
        for value_field in ("old_value", "new_value", "added", "removed"):
            if value_field in normalized:
                normalized[value_field] = redact_audit_value(
                    normalized[value_field], key=change_key
                )
        changes.append(AuditLogChangePayload.model_validate(normalized))
    return changes


def audit_log_payload(entry: AuditLogEntry) -> AuditLogEntryPayload:
    target = redact_audit_value(entry.target_ref)
    raw_changes = redact_audit_value(entry.changes or [])
    return AuditLogEntryPayload(
        id=str(entry.id),
        guild_id=str(entry.guild_id),
        guild_domain=entry.guild_domain,
        actor_id=str(entry.actor_id),
        actor_domain=entry.actor_domain,
        action_type=entry.action_type,
        target_type=entry.target_type,
        target_ref=target if isinstance(target, dict) else None,
        reason=entry.reason,
        changes=_audit_changes(raw_changes),
        created_at=entry.created_at,
    )
