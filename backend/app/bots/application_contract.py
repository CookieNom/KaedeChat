from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from app.bots.install_config import (
    REQUIRED_USER_INSTALL_SCOPES,
    USER_INSTALL,
    USER_INSTALL_CONTEXTS,
    USER_INSTALL_SCOPES,
)
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.permissions import ALL_PERMISSIONS
from app.federation.network import FederationNetworkError, normalize_domain

DEVELOPER_TEAM_APPLICATION_LIMIT = 75

SUPPORTED_APPLICATION_SCOPES = frozenset(
    {
        "applications.commands",
        "applications.assets.manage",
        "applications.emojis.manage",
        "interactions.respond",
        "audit_logs.read",
        "automod.executions.read",
        "automod.rules.read",
        "automod.rules.manage",
        "guilds.read",
        "guilds.manage",
        "guilds.assets.manage",
        "channels.read",
        "channels.manage",
        "channels.overwrites.read",
        "channels.overwrites.manage",
        "members.read",
        "roles.read",
        "roles.manage",
        "events.read",
        "events.manage",
        "expressions.read",
        "expressions.manage",
        "installations.read",
        "integrations.read",
        "integrations.manage",
        "messages.metadata",
        "messages.content",
        "messages.history",
        "messages.send",
        "messages.edit.own",
        "messages.delete.own",
        "messages.manage",
        "attachments.read",
        "attachments.write",
        "reactions.read",
        "reactions.write",
        "polls.read",
        "polls.write",
        "moderation.bans",
        "moderation.members",
        "moderation.messages",
        "moderation.prune",
        "soundboard.read",
        "soundboard.use",
        "soundboard.manage",
        "voice.states.read",
        "voice.connect",
        "voice.listen",
        "voice.speak",
        "voice.stream",
        "voice.moderate",
        "invites.read",
        "invites.manage",
        "webhooks.read",
        "webhooks.manage",
        "emojis.manage",
        "tasks.read",
        "tasks.write",
        "tasks.manage",
        "dm.send",
    }
)


def canonical_application_manifest_projection(
    *,
    name: str,
    description: str | None,
    icon_hash: str | None,
    support_url: str | None,
    privacy_url: str | None,
    target_policy: str,
    default_scopes: Sequence[str],
    default_intents: Sequence[str],
    default_permissions: int,
    supported_install_types: Sequence[str],
    user_install_scopes: Sequence[str],
    user_install_contexts: Sequence[str],
    e2ee_modes: Sequence[str],
) -> tuple[object, ...]:
    """Canonicalize fields whose authority is ``manifest_generation``.

    Grant/context arrays are protocol sets. Their wire order does not create a
    new projection, while human-facing scalar fields retain exact semantics.
    """

    return (
        name,
        description,
        icon_hash,
        support_url,
        privacy_url,
        target_policy,
        tuple(sorted(default_scopes)),
        tuple(sorted(default_intents)),
        default_permissions,
        tuple(sorted(supported_install_types)),
        tuple(sorted(user_install_scopes)),
        tuple(sorted(user_install_contexts)),
        tuple(sorted(e2ee_modes)),
    )


def validate_known_permission_mask(value: int, *, label: str) -> int:
    """Reject negative values and every unnamed permission bit, including gaps."""

    if value < 0 or value & ~ALL_PERMISSIONS:
        raise ValueError(f"{label} contain unknown bits")
    return value


def validate_application_icon_hash(value: str | None) -> str | None:
    if value is not None and (
        len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("application icon hash must be lowercase SHA-256")
    return value


def validate_application_https_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("application URL is invalid") from exc
    try:
        normalized_hostname = normalize_domain(hostname) if hostname is not None else None
    except FederationNetworkError as exc:
        raise ValueError("application URLs must use a canonical HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or normalized_hostname != hostname
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("application URLs must use a canonical HTTPS origin")
    return value


def validate_application_install_contract(
    *,
    default_scopes: Sequence[str],
    default_intents: Sequence[str],
    supported_install_types: Sequence[str],
    user_install_scopes: Sequence[str],
    user_install_contexts: Sequence[str],
    e2ee_modes: Sequence[str],
) -> None:
    """Validate the shared local, manifest, and developer-projection policy."""

    bounded_sets = (
        (default_scopes, SUPPORTED_APPLICATION_SCOPES, "application default scopes"),
        (default_intents, SUPPORTED_BOT_INTENTS, "application default intents"),
        (supported_install_types, frozenset({"guild_install", USER_INSTALL}), "install types"),
        (user_install_scopes, USER_INSTALL_SCOPES, "user-install scopes"),
        (user_install_contexts, USER_INSTALL_CONTEXTS, "user-install contexts"),
        (e2ee_modes, frozenset({"participant"}), "application E2EE modes"),
    )
    for values, supported, label in bounded_sets:
        if len(values) != len(set(values)) or not set(values) <= supported:
            raise ValueError(f"{label} are invalid")
    scopes = set(user_install_scopes)
    if not scopes >= REQUIRED_USER_INSTALL_SCOPES:
        raise ValueError("application has an invalid user-install scope policy")
    if USER_INSTALL in supported_install_types and (
        not scopes <= set(default_scopes) or "interactions" not in default_intents
    ):
        raise ValueError("user-install policy exceeds the application runtime grant")
