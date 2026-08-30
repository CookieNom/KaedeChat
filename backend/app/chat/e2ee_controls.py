from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import validate_channel_encryption_policy
from app.core.types import validate_snowflake
from app.db.models import E2EEControlRecord, Message


def _valid_operation_reference(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 47
        and value.startswith("keo_")
        and all(character.isalnum() or character in "-_" for character in value[4:])
    )


def _canonical_snowflake(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(validate_snowflake(value)) == value
    except ValueError:
        return False


def e2ee_control_record_payload(record: E2EEControlRecord) -> dict[str, object] | None:
    """Render one authenticated durable MLS control record.

    Both human clients and bot workers recover from this same minimal wire
    contract.  Keep the projection centralized so one surface cannot silently
    omit the authority or apply-mode binding enforced by the other.
    """

    envelope = record.envelope if isinstance(record.envelope, dict) else None
    if envelope is None or envelope.get("operation") not in {"welcome", "commit"}:
        return None
    return {
        "id": str(record.id),
        "origin_domain": record.origin_domain,
        "channel_id": str(record.channel_id),
        "channel_domain": record.channel_domain,
        "author_id": str(record.author_id),
        "author_domain": record.author_domain,
        "e2ee": envelope,
        "encryption_policy_generation": str(record.policy_generation),
        "encryption_epoch": str(record.epoch),
        "apply": record.apply_mode != "audit",
        "room_operation_id": record.room_operation_id,
        "room_operation_domain": record.room_operation_domain,
    }


def room_policy_change_context(channel: Any, actor: Any) -> dict[str, object]:
    """Render the signed context for a device-change room-policy update."""

    channel_id = str(channel.id)
    channel_domain = str(channel.origin_domain)
    guild_id = getattr(channel, "guild_id", None)
    guild_domain = getattr(channel, "guild_domain", None)
    return {
        "reason": "e2ee.device-list.changed",
        "actor": {
            "id": str(actor.id),
            "domain": str(actor.origin_domain),
        },
        "channel": {"id": channel_id, "domain": channel_domain},
        "scope": (
            {"type": "guild", "id": str(guild_id), "domain": str(guild_domain)}
            if guild_id is not None and guild_domain is not None
            else {"type": "dm", "id": channel_id, "domain": channel_domain}
        ),
    }


def authority_attested_room_policy_change(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor_id: str,
    actor_domain: str,
) -> bool:
    """Recognize a remote actor only for its exact authority-minted pause event."""

    if event_type != "e2ee.room-policy.changed":
        return False
    if not isinstance(content, dict) or set(content) != {
        "channel_id",
        "channel_domain",
        "encryption_policy",
    }:
        return False
    if not isinstance(context, dict) or set(context) != {
        "reason",
        "actor",
        "channel",
        "scope",
    }:
        return False
    actor = context.get("actor")
    channel = context.get("channel")
    scope = context.get("scope")
    if (
        context.get("reason") != "e2ee.device-list.changed"
        or not isinstance(actor, dict)
        or set(actor) != {"id", "domain"}
        or actor != {"id": actor_id, "domain": actor_domain}
        or not isinstance(channel, dict)
        or set(channel) != {"id", "domain"}
        or not isinstance(scope, dict)
        or set(scope) != {"type", "id", "domain"}
        or scope.get("type") not in {"guild", "dm"}
    ):
        return False
    channel_id = content.get("channel_id")
    if (
        not _canonical_snowflake(channel_id)
        or channel.get("id") != channel_id
        or content.get("channel_domain") != expected_authority
        or channel.get("domain") != expected_authority
        or not _canonical_snowflake(scope.get("id"))
        or scope.get("domain") != expected_authority
        or (scope.get("type") == "dm" and scope.get("id") != channel_id)
    ):
        return False
    raw_policy = content.get("encryption_policy")
    if not isinstance(raw_policy, dict) or set(raw_policy) != {
        "mode",
        "state",
        "generation",
        "protocol",
        "suite",
        "group_id",
        "epoch",
    }:
        return False
    try:
        policy = validate_channel_encryption_policy(raw_policy)
    except ValueError:
        return False
    return bool(policy["mode"] == "e2ee" and policy["state"] == "rekeying")


def authority_attested_direct_dm_control(
    event_type: object,
    content: object,
    *,
    expected_authority: str,
    actor_id: str,
    actor_domain: str,
) -> bool:
    """Recognize the only direct-DM event an authority may sign for a remote actor.

    A remote participant can initiate a room activation or rekey at the DM
    authority.  The authority mints the resulting MLS control message, while
    preserving that participant as its semantic author.  Keep this exception
    narrower than ordinary ``dm.message.create`` so it cannot become a general
    remote-actor impersonation path.
    """

    if (
        event_type != "dm.message.create"
        or not isinstance(content, dict)
        or set(content) != {"message", "author", "encryption_policy", "e2ee_control"}
    ):
        return False
    message = content.get("message")
    author = content.get("author")
    metadata = content.get("e2ee_control")
    if not isinstance(message, dict) or not isinstance(author, dict):
        return False
    if not isinstance(metadata, dict) or set(metadata) != {
        "operation_id",
        "operation_domain",
        "apply",
    }:
        return False
    envelope = message.get("e2ee")
    operation = envelope.get("operation") if isinstance(envelope, dict) else None
    raw_policy = content.get("encryption_policy")
    if not isinstance(raw_policy, dict) or set(raw_policy) != {
        "mode",
        "state",
        "generation",
        "protocol",
        "suite",
        "group_id",
        "epoch",
    }:
        return False
    try:
        policy = validate_channel_encryption_policy(raw_policy)
    except ValueError:
        return False
    apply = metadata.get("apply")
    return bool(
        operation in {"welcome", "commit"}
        and policy["mode"] == "e2ee"
        and policy["state"] == "active"
        and isinstance(envelope, dict)
        and envelope.get("protocol") == policy["protocol"]
        and envelope.get("suite") == policy["suite"]
        and envelope.get("group_id") == policy["group_id"]
        and envelope.get("policy_generation") == str(policy["generation"])
        and envelope.get("epoch") == str(policy["epoch"])
        and _canonical_snowflake(message.get("channel_id"))
        and type(message.get("message_type")) is int
        and message.get("message_type") == 7
        and type(message.get("flags")) is int
        and message.get("flags") == 4
        and message.get("origin_domain") == expected_authority
        and message.get("channel_domain") == expected_authority
        and message.get("author_id") == actor_id
        and message.get("author_domain") == actor_domain
        and str(author.get("id")) == actor_id
        and author.get("origin_domain") == actor_domain
        and _valid_operation_reference(metadata.get("operation_id"))
        and metadata.get("operation_domain") == expected_authority
        and isinstance(apply, bool)
        and (operation != "welcome" or apply)
    )


async def apply_e2ee_control_metadata(
    session: AsyncSession,
    message: Message,
    value: object,
    *,
    expected_authority: str,
) -> None:
    """Reconcile an MLS control with its signed, authority-bound metadata.

    The message trigger only provides loss-resistant capture.  A control is not
    eligible for application until this function has bound it to the signed
    room operation metadata at the common message-ingestion boundary.
    """

    envelope = message.e2ee if isinstance(message.e2ee, dict) else None
    operation = envelope.get("operation") if envelope is not None else None
    if operation not in {"welcome", "commit"}:
        if value is not None:
            raise ValueError("non-control message contains E2EE control metadata")
        return
    if not isinstance(value, dict) or set(value) != {
        "operation_id",
        "operation_domain",
        "apply",
    }:
        raise ValueError("E2EE control metadata is invalid")
    operation_id = value.get("operation_id")
    operation_domain = value.get("operation_domain")
    apply = value.get("apply")
    if (
        not _valid_operation_reference(operation_id)
        or operation_domain != expected_authority
        or not isinstance(apply, bool)
        or (operation == "welcome" and not apply)
    ):
        raise ValueError("E2EE control metadata is invalid")
    apply_mode = "join" if operation == "welcome" else "process" if apply else "audit"
    record = await session.get(E2EEControlRecord, (message.id, message.origin_domain))
    if record is None:
        record = E2EEControlRecord(
            id=message.id,
            origin_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            author_id=message.author_id,
            author_domain=message.author_domain,
            policy_generation=message.encryption_policy_generation,
            epoch=message.encryption_epoch or 0,
            operation=str(operation),
            apply_mode=apply_mode,
            room_operation_id=operation_id,
            room_operation_domain=str(operation_domain),
            envelope=envelope,
            created_at=message.created_at,
        )
        session.add(record)
        return
    if (
        record.operation != operation
        or record.envelope != envelope
        or (
            record.room_operation_id is not None
            and (
                record.room_operation_id != operation_id
                or record.room_operation_domain != operation_domain
                or record.apply_mode != apply_mode
            )
        )
    ):
        raise ValueError("E2EE control metadata conflicts with its durable record")
    record.apply_mode = apply_mode
    record.room_operation_id = operation_id
    record.room_operation_domain = str(operation_domain)
