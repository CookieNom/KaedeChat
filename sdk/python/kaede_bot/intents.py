from __future__ import annotations

from dataclasses import dataclass

from .generated import BOT_INTENT_ALIASES, BOT_INTENT_NAMES

SUPPORTED_INTENTS = frozenset(BOT_INTENT_NAMES)
INTENT_ALIASES = BOT_INTENT_ALIASES


@dataclass(frozen=True, slots=True)
class Intents:
    guilds: bool = True
    guild_members: bool = False
    guild_moderation: bool = False
    guild_expressions: bool = False
    guild_integrations: bool = False
    guild_webhooks: bool = False
    guild_invites: bool = False
    guild_voice_states: bool = False
    guild_presences: bool = False
    guild_messages: bool = True
    guild_message_reactions: bool = False
    guild_message_typing: bool = False
    direct_messages: bool = True
    direct_message_reactions: bool = True
    direct_message_typing: bool = False
    guild_scheduled_events: bool = False
    message_content: bool = False
    message_reactions: bool = True
    guild_message_polls: bool = False
    direct_message_polls: bool = False
    guild_typing: bool = False
    voice_states: bool = False
    interactions: bool = True
    guild_tasks: bool = False
    auto_moderation_configuration: bool = False
    auto_moderation_execution: bool = False

    @classmethod
    def default(cls) -> Intents:
        return cls()

    @classmethod
    def all(cls) -> Intents:
        return cls(
            guilds=True,
            guild_members=True,
            guild_moderation=True,
            guild_expressions=True,
            guild_integrations=True,
            guild_webhooks=True,
            guild_invites=True,
            guild_voice_states=True,
            guild_presences=True,
            guild_messages=True,
            guild_message_reactions=True,
            guild_message_typing=True,
            direct_messages=True,
            direct_message_reactions=True,
            direct_message_typing=True,
            guild_scheduled_events=True,
            message_content=True,
            message_reactions=True,
            guild_message_polls=True,
            direct_message_polls=True,
            guild_typing=True,
            voice_states=True,
            interactions=True,
            guild_tasks=True,
            auto_moderation_configuration=True,
            auto_moderation_execution=True,
        )

    def names(self) -> list[str]:
        return [name for name in self.__dataclass_fields__ if getattr(self, name)]
