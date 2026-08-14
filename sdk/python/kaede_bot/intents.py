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
    voice_states: bool = False
    interactions: bool = True

    @classmethod
    def default(cls) -> "Intents":
        return cls()

    @classmethod
    def all(cls) -> "Intents":
        return cls(True, True, True, True, True, True, True, True)

    def names(self) -> list[str]:
        return [name for name in self.__dataclass_fields__ if getattr(self, name)]
