from app.api.applications import SUPPORTED_INTENTS, SUPPORTED_SCOPES
from scripts.generate_protocol import (
    DART_OUTPUT,
    OUTPUT,
    PYTHON_OUTPUT,
    RUST_OUTPUT,
    render,
    render_dart,
    render_python,
    render_rust,
)


def test_generated_protocol_files_are_current() -> None:
    for filename, expected in render().items():
        assert (OUTPUT / filename).read_text(encoding="utf-8") == expected
    assert RUST_OUTPUT.read_text(encoding="utf-8") == render_rust()
    assert DART_OUTPUT.read_text(encoding="utf-8") == render_dart()
    assert PYTHON_OUTPUT.read_text(encoding="utf-8") == render_python()


def test_permission_alias_is_generated_for_every_client_protocol() -> None:
    for language, output, expected in (
        (
            "render()['permissions.ts']",
            render()["permissions.ts"],
            [
                "CREATE_INSTANT_INVITE: 1n",
                "MANAGE_GUILD_EXPRESSIONS: 1073741824n",
                "export const PERMISSION_SCHEMA = 'kaede-permissions-v1' as const;",
            ],
        ),
        (
            "render_rust()",
            render_rust(),
            [
                "pub const CREATE_INSTANT_INVITE: u64 = 1;",
                "pub const MANAGE_GUILD_EXPRESSIONS: u64 = 1073741824;",
                'pub const PERMISSION_SCHEMA: &str = "kaede-permissions-v1";',
                'PRIORITY_SPEAKER_TOPIC: &str = "kaede.priority-speaker.v1"',
            ],
        ),
        (
            "render_dart()",
            render_dart(),
            [
                "static const createInstantInvite = 1;",
                "static const manageGuildExpressions = 1073741824;",
                'const permissionSchema = "kaede-permissions-v1";',
                "prioritySpeakerActivePayload = <int>[1]",
            ],
        ),
        (
            "render_python()",
            render_python(),
            [
                'PERMISSION_SCHEMA = "kaede-permissions-v1"',
                'PRIORITY_SPEAKER_TOPIC = "kaede.priority-speaker.v1"',
                'PRIORITY_SPEAKER_ACTIVE_PAYLOAD = b"\\x01"',
            ],
        ),
        (
            "render()['ops.ts']",
            render()["ops.ts"],
            [
                "PRIORITY_SPEAKER_TOPIC = 'kaede.priority-speaker.v1'",
                "PRIORITY_SPEAKER_ACTIVE_PAYLOAD = [1]",
            ],
        ),
    ):
        for contract in expected:
            assert contract in output, (language, contract)

    assert {
        "audit_logs.read",
        "automod.executions.read",
        "automod.rules.read",
        "automod.rules.manage",
        "guilds.manage",
        "guilds.assets.manage",
        "channels.manage",
        "channels.overwrites.read",
        "channels.overwrites.manage",
        "roles.manage",
        "events.read",
        "events.manage",
        "expressions.read",
        "expressions.manage",
        "installations.read",
        "integrations.read",
        "integrations.manage",
        "attachments.read",
        "attachments.write",
        "moderation.bans",
        "moderation.messages",
        "moderation.prune",
        "polls.read",
        "polls.write",
        "soundboard.read",
        "soundboard.use",
        "soundboard.manage",
        "voice.connect",
        "voice.listen",
        "voice.speak",
        "voice.stream",
        "invites.read",
        "voice.moderate",
        "invites.manage",
        "webhooks.read",
        "webhooks.manage",
        "emojis.manage",
        "tasks.read",
        "tasks.write",
        "tasks.manage",
        "dm.send",
    } <= SUPPORTED_SCOPES
    assert {
        "guild_moderation",
        "guild_expressions",
        "guild_integrations",
        "guild_webhooks",
        "guild_invites",
        "guild_scheduled_events",
        "guild_message_polls",
        "direct_message_polls",
        "auto_moderation_configuration",
        "auto_moderation_execution",
        "guild_tasks",
    } <= SUPPORTED_INTENTS
