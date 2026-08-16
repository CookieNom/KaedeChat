from __future__ import annotations

import hashlib

# Group membership is a wire-level invariant. Keeping this fixed prevents two
# federated homes with different operator settings from accepting different
# authoritative states for the same conversation.
MAX_GROUP_DM_PARTICIPANTS = 10

# Persisted message types used for group membership notices.  They are kept
# separate from ordinary user messages so every client can render them as
# trusted conversation activity instead of user-authored chat text.
GROUP_DM_MEMBER_ADDED = 3
GROUP_DM_MEMBER_LEFT = 4
GROUP_DM_MEMBER_REMOVED = 5


def group_dm_notice_text(
    message_type: int,
    actor_name: str,
    target_name: str,
    new_owner_name: str | None = None,
) -> str:
    if message_type == GROUP_DM_MEMBER_ADDED:
        return f"{actor_name} added {target_name} to the group."
    if message_type == GROUP_DM_MEMBER_LEFT:
        text = f"{actor_name} left the group."
    elif message_type == GROUP_DM_MEMBER_REMOVED:
        text = f"{actor_name} removed {target_name} from the group."
    else:
        raise ValueError("unsupported group DM notice type")
    if new_owner_name is not None:
        text += f" {new_owner_name} is now the owner."
    return text


def normalize_handle(handle: str) -> str:
    username, separator, domain = handle.rpartition("@")
    if not separator or not username or not domain:
        raise ValueError("handle must be username@domain")
    return f"{username.lower()}@{domain.rstrip('.').lower()}"


def dm_pair_key(first: str, second: str) -> str:
    handles = sorted((normalize_handle(first), normalize_handle(second)))
    if handles[0] == handles[1]:
        raise ValueError("a direct-message pair requires two distinct handles")
    return hashlib.sha256("\n".join(handles).encode()).hexdigest()


def dm_authority_domain(first: str, second: str) -> str:
    domains = sorted(
        (
            normalize_handle(first).rpartition("@")[2],
            normalize_handle(second).rpartition("@")[2],
        )
    )
    return domains[0]


def group_dm_key(authority_domain: str, conversation_id: int) -> str:
    """Return the stable unique lookup key for an authority-owned group DM."""

    authority = authority_domain.rstrip(".").lower()
    return hashlib.sha256(f"group\n{authority}\n{conversation_id}".encode()).hexdigest()
