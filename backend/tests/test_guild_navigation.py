import pytest
from pydantic import ValidationError

from app.auth.schemas import GuildNavigationUpdate
from app.core.guild_navigation import normalize_guild_navigation, parse_stored_guild_navigation


def test_guild_navigation_normalizes_composite_refs_and_appends_new_guilds() -> None:
    payload = GuildNavigationUpdate.model_validate(
        {
            "items": [
                {
                    "kind": "group",
                    "id": "friends",
                    "name": "Friends",
                    "guilds": [
                        {"id": "22", "origin_domain": "remote.example"},
                        "11",
                    ],
                    "collapsed": True,
                },
                {"kind": "guild", "guild": "999@gone.example"},
            ]
        }
    )

    assert normalize_guild_navigation(
        payload,
        [(11, "home.example"), (22, "remote.example"), (33, "home.example")],
        "home.example",
    ) == {
        "items": [
            {
                "kind": "group",
                "id": "friends",
                "name": "Friends",
                "guilds": ["22@remote.example", "11@home.example"],
                "collapsed": True,
            },
            {"kind": "guild", "guild": "33@home.example"},
        ]
    }


def test_guild_navigation_rejects_duplicate_and_malformed_groups() -> None:
    with pytest.raises(ValidationError, match="each guild can appear only once"):
        GuildNavigationUpdate.model_validate(
            {
                "items": [
                    {"kind": "guild", "guild": "11@home.example"},
                    {
                        "kind": "group",
                        "id": "again",
                        "name": "Again",
                        "guilds": ["11@home.example"],
                    },
                ]
            }
        )
    with pytest.raises(ValidationError):
        GuildNavigationUpdate.model_validate(
            {"items": [{"kind": "group", "id": "bad id", "name": " ", "guilds": []}]}
        )


def test_corrupt_stored_guild_navigation_falls_back_safely() -> None:
    parsed = parse_stored_guild_navigation({"items": [{"kind": "unknown"}]})
    assert parsed.items == []


def test_guild_navigation_canonicalizes_singleton_groups() -> None:
    payload = GuildNavigationUpdate.model_validate(
        {
            "items": [
                {
                    "kind": "group",
                    "id": "singleton",
                    "name": "Singleton",
                    "guilds": ["11@home.example"],
                }
            ]
        }
    )

    assert normalize_guild_navigation(
        payload,
        [(11, "home.example")],
        "home.example",
    ) == {"items": [{"kind": "guild", "guild": "11@home.example"}]}


def test_guild_navigation_rejects_nested_group_items() -> None:
    with pytest.raises(ValidationError):
        GuildNavigationUpdate.model_validate(
            {
                "items": [
                    {
                        "kind": "group",
                        "id": "outer",
                        "name": "Outer",
                        "guilds": [
                            {
                                "kind": "group",
                                "id": "nested",
                                "name": "Nested",
                                "guilds": ["11@home.example"],
                            }
                        ],
                    }
                ]
            }
        )
