from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.types import EntityRef
from app.db.bot_models import (
    BotDMCapability,
    BotInstallation,
    BotInteraction,
    BotUserInstallation,
)

GUILD_INSTALL_OWNER = "guild_install"
USER_INSTALL_OWNER = "user_install"
BOT_DM_GUILD_OWNER = "0"
AUTHORIZING_INTEGRATION_OWNER_TYPES = (
    GUILD_INSTALL_OWNER,
    USER_INSTALL_OWNER,
)
INTERACTION_EVENT_SNAPSHOT_KEY = "_interaction_event_snapshot"
INTERACTION_INSTALLATION_LINEAGE_KEY = "_interaction_installation_lineage"


def _qualified_owner_ref(value: object, *, owner_type: str) -> str:
    if owner_type == GUILD_INSTALL_OWNER and value == BOT_DM_GUILD_OWNER:
        return BOT_DM_GUILD_OWNER
    if not isinstance(value, str):
        raise ValueError("interaction authorizing owner must be a qualified reference")
    reference = EntityRef(value)
    if reference.domain is None:
        raise ValueError("interaction authorizing owner must be a qualified reference")
    return str(reference)


def normalize_authorizing_integration_owners(
    value: object,
) -> dict[str, str]:
    """Validate and canonicalize Discord's one-or-two installation owners.

    Kaede uses descriptive integration keys instead of Discord's numeric wire
    keys.  Routable owners stay authority-qualified; Discord's documented
    ``"0"`` guild-install sentinel is preserved for guild-installed apps used
    in bot DMs.
    """

    if not isinstance(value, Mapping):
        raise ValueError("interaction authorizing owners are invalid")
    raw = {str(key): item for key, item in value.items()}
    if not raw or len(raw) > 2 or set(raw) - set(AUTHORIZING_INTEGRATION_OWNER_TYPES):
        raise ValueError("interaction authorizing owners are invalid")
    return {
        key: _qualified_owner_ref(raw[key], owner_type=key)
        for key in AUTHORIZING_INTEGRATION_OWNER_TYPES
        if key in raw
    }


def installation_authorizing_integration_owners(
    installation: BotInstallation | BotUserInstallation | BotDMCapability,
) -> dict[str, str]:
    """Return the actual authority-owned installer behind one runtime grant."""

    if isinstance(installation, BotInstallation):
        return {GUILD_INSTALL_OWNER: f"{installation.guild_id}@{installation.guild_domain}"}
    if isinstance(installation, BotUserInstallation):
        return {USER_INSTALL_OWNER: f"{installation.user_id}@{installation.user_domain}"}
    if installation.source_kind == "guild":
        if installation.guild_id is None or installation.guild_domain is None:
            raise ValueError("guild DM capability is missing its authorizing guild")
        # Discord uses the non-routable guild owner sentinel in a bot DM. Keep
        # the actual qualified source installation in the private lineage below.
        return {GUILD_INSTALL_OWNER: BOT_DM_GUILD_OWNER}
    if installation.source_kind == "user":
        if installation.installing_user_id is None or installation.installing_user_domain is None:
            raise ValueError("user DM capability is missing its authorizing installer")
        return {
            USER_INSTALL_OWNER: (
                f"{installation.installing_user_id}@{installation.installing_user_domain}"
            )
        }
    raise ValueError("DM capability has an invalid source type")


def installation_authority_lineage(
    installation: BotInstallation | BotUserInstallation | BotDMCapability,
) -> dict[str, object]:
    """Capture immutable admission authority separately from Discord owners.

    This private snapshot is never projected as Discord's
    ``authorizing_integration_owners`` object.  It retains the qualified source
    installation needed to route a federated bot-DM interaction even when the
    public Discord-compatible guild owner is the non-routable ``"0"`` sentinel.
    Mutable grant configuration is copied so an admitted interaction can finish
    under the authority that existed at admission.
    """

    if isinstance(installation, BotInstallation):
        return {
            "integration_type": GUILD_INSTALL_OWNER,
            "installation_ref": f"{installation.id}@{installation.guild_domain}",
            "owner_ref": f"{installation.guild_id}@{installation.guild_domain}",
            "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
            "bot_user_ref": f"{installation.bot_user_id}@{installation.bot_user_domain}",
            "grant_revision": str(installation.grant_revision),
            "granted_scopes": sorted(set(installation.granted_scopes)),
        }
    if isinstance(installation, BotUserInstallation):
        installation_id = installation.source_id or installation.id
        installation_domain = installation.source_domain or installation.user_domain
        return {
            "integration_type": USER_INSTALL_OWNER,
            "installation_ref": f"{installation_id}@{installation_domain}",
            "owner_ref": f"{installation.user_id}@{installation.user_domain}",
            "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
            "grant_revision": str(installation.grant_revision),
            "granted_scopes": sorted(set(installation.granted_scopes)),
        }
    owner_ref = (
        f"{installation.guild_id}@{installation.guild_domain}"
        if installation.source_kind == "guild"
        else f"{installation.installing_user_id}@{installation.installing_user_domain}"
    )
    return {
        "integration_type": "dm_capability",
        "source_kind": installation.source_kind,
        "installation_ref": (
            f"{installation.source_installation_id}@{installation.source_installation_domain}"
        ),
        "owner_ref": owner_ref,
        "application_ref": (f"{installation.application_id}@{installation.application_domain}"),
        "bot_user_ref": f"{installation.bot_user_id}@{installation.bot_user_domain}",
        "dm_capability_ref": f"{installation.id}@{installation.authority_domain}",
        "dm_capability_grant_id": installation.grant_id,
        "grant_revision": str(installation.revision),
        "granted_scopes": sorted(set(installation.granted_scopes)),
    }


def stored_installation_authority_lineage(
    interaction: BotInteraction,
) -> dict[str, object] | None:
    """Return a minimally validated private admission-authority snapshot."""

    raw_payload = getattr(interaction, "payload", None)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    raw = payload.get(INTERACTION_INSTALLATION_LINEAGE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("interaction installation lineage is invalid")
    lineage = {str(key): item for key, item in raw.items()}
    expected_type = interaction.integration_type
    if lineage.get("integration_type") != expected_type:
        raise ValueError("interaction installation lineage is invalid")
    for key in ("installation_ref", "owner_ref", "application_ref"):
        value = lineage.get(key)
        if not isinstance(value, str) or EntityRef(value).domain is None:
            raise ValueError("interaction installation lineage is invalid")
    raw_revision = lineage.get("grant_revision")
    if (
        not isinstance(raw_revision, str)
        or not raw_revision.isascii()
        or not raw_revision.isdecimal()
        or str(int(raw_revision)) != raw_revision
        or int(raw_revision) != interaction.installation_revision
    ):
        raise ValueError("interaction installation lineage is invalid")
    raw_scopes = lineage.get("granted_scopes")
    if (
        not isinstance(raw_scopes, list)
        or any(not isinstance(scope, str) or not scope for scope in raw_scopes)
        or raw_scopes != sorted(set(raw_scopes))
    ):
        raise ValueError("interaction installation lineage is invalid")
    return lineage


def stored_interaction_event_snapshot(interaction: BotInteraction) -> dict[str, Any] | None:
    raw_payload = getattr(interaction, "payload", None)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    raw = payload.get(INTERACTION_EVENT_SNAPSHOT_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("interaction event snapshot is invalid")
    return {str(key): item for key, item in raw.items()}


def stored_authorizing_integration_owners(
    interaction: BotInteraction,
) -> dict[str, str] | None:
    snapshot = stored_interaction_event_snapshot(interaction)
    if snapshot is None:
        return None
    return normalize_authorizing_integration_owners(snapshot.get("authorizing_integration_owners"))
