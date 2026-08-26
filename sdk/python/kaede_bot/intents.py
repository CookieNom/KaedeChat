from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Intents:
    guilds: bool = True
    guild_members: bool = False
    guild_presences: bool = False
    guild_messages: bool = True
    message_content: bool = False
    message_reactions: bool = True
    guild_typing: bool = False
    voice_states: bool = False
    interactions: bool = True
    guild_tasks: bool = False

    @classmethod
    def default(cls) -> Intents:
        return cls()

    @classmethod
    def all(cls) -> Intents:
        return cls(
            guilds=True,
            guild_members=True,
            guild_presences=True,
            guild_messages=True,
            message_content=True,
            message_reactions=True,
            guild_typing=True,
            voice_states=True,
            interactions=True,
            guild_tasks=True,
        )

    def names(self) -> list[str]:
        return [name for name in self.__dataclass_fields__ if getattr(self, name)]
