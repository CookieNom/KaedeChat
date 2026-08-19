from __future__ import annotations

from app.auth.schemas import (
    GuildNavigationGuildItem,
    GuildNavigationUpdate,
)


def normalize_guild_navigation(
    navigation: GuildNavigationUpdate,
    accessible_guilds: list[tuple[int, str]],
    local_domain: str,
) -> dict[str, object]:
    """Drop stale references and append newly joined guilds deterministically."""

    accessible = {f"{guild_id}@{domain}" for guild_id, domain in accessible_guilds}
    seen: set[str] = set()
    items: list[dict[str, object]] = []
    for item in navigation.items:
        if isinstance(item, GuildNavigationGuildItem):
            guild_id, domain = item.guild.resolve(local_domain)
            guild = f"{guild_id}@{domain}"
            if guild in accessible and guild not in seen:
                items.append({"kind": "guild", "guild": guild})
                seen.add(guild)
            continue
        guilds: list[str] = []
        for raw in item.guilds:
            guild_id, domain = raw.resolve(local_domain)
            guild = f"{guild_id}@{domain}"
            if guild in accessible and guild not in seen:
                guilds.append(guild)
                seen.add(guild)
        if len(guilds) == 1:
            items.append({"kind": "guild", "guild": guilds[0]})
        elif guilds:
            items.append(
                {
                    "kind": "group",
                    "id": item.id,
                    "name": item.name,
                    "guilds": guilds,
                    "collapsed": item.collapsed,
                }
            )
    for guild_id, domain in accessible_guilds:
        guild = f"{guild_id}@{domain}"
        if guild not in seen:
            items.append({"kind": "guild", "guild": guild})
    return {"items": items}


def parse_stored_guild_navigation(value: object) -> GuildNavigationUpdate:
    try:
        return GuildNavigationUpdate.model_validate(value)
    except ValueError:
        return GuildNavigationUpdate()
