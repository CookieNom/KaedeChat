"""Stable bot Gateway intent names shared by validation and code generation.

Kaede published a few concise intent names before adopting Discord-compatible
names.  Those names remain valid wire values forever; new code may use either
the Discord-compatible spelling or the documented Kaede extension.
"""

DISCORD_BOT_INTENTS = (
    "guilds",
    "guild_members",
    "guild_moderation",
    "guild_expressions",
    "guild_integrations",
    "guild_webhooks",
    "guild_invites",
    "guild_voice_states",
    "guild_presences",
    "guild_messages",
    "guild_message_reactions",
    "guild_message_typing",
    "direct_messages",
    "direct_message_reactions",
    "direct_message_typing",
    "message_content",
    "guild_scheduled_events",
    "auto_moderation_configuration",
    "auto_moderation_execution",
    "guild_message_polls",
    "direct_message_polls",
)

# Product-specific event families are additive and deliberately do not reuse a
# Discord intent name with different semantics.
KAEDE_BOT_INTENTS = (
    "interactions",
    "guild_tasks",
)

# Previously published Kaede spellings.  Do not remove, normalize in storage,
# or silently rewrite them: existing application/install grants use these exact
# strings and the Gateway accepts both spellings for the same event family.
BOT_INTENT_ALIASES = {
    "voice_states": "guild_voice_states",
    "message_reactions": "guild_message_reactions",
    "guild_typing": "guild_message_typing",
}

BOT_INTENT_NAMES = DISCORD_BOT_INTENTS + KAEDE_BOT_INTENTS + tuple(BOT_INTENT_ALIASES)
SUPPORTED_BOT_INTENTS = frozenset(BOT_INTENT_NAMES)
