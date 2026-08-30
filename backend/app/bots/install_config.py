from __future__ import annotations

from typing import Final

GUILD_INSTALL: Final = "guild_install"
USER_INSTALL: Final = "user_install"

SUPPORTED_INSTALL_TYPES: Final = frozenset({GUILD_INSTALL, USER_INSTALL})
USER_INSTALL_SCOPES: Final = frozenset(
    {
        "applications.commands",
        "interactions.respond",
        "attachments.read",
        "attachments.write",
    }
)
REQUIRED_USER_INSTALL_SCOPES: Final = frozenset({"applications.commands", "interactions.respond"})
USER_INSTALL_CONTEXTS: Final = frozenset({"guild", "bot_dm", "private_channel"})

DEFAULT_INSTALL_TYPES: Final = [GUILD_INSTALL]
DEFAULT_USER_INSTALL_SCOPES: Final = ["applications.commands", "interactions.respond"]
DEFAULT_USER_INSTALL_CONTEXTS: Final = ["guild", "bot_dm", "private_channel"]


def integration_types_config(
    *,
    supported_install_types: list[str],
    guild_scopes: list[str],
    guild_permissions: int,
    user_scopes: list[str],
    user_contexts: list[str],
) -> dict[str, dict[str, object]]:
    """Project application install defaults in Discord's per-target shape."""

    config: dict[str, dict[str, object]] = {}
    if GUILD_INSTALL in supported_install_types:
        config[GUILD_INSTALL] = {
            "oauth2_install_params": {
                "scopes": list(guild_scopes),
                "permissions": str(guild_permissions),
            }
        }
    if USER_INSTALL in supported_install_types:
        config[USER_INSTALL] = {
            "oauth2_install_params": {"scopes": list(user_scopes)},
            "contexts": list(user_contexts),
        }
    return config
