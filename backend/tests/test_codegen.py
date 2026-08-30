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
    assert "CREATE_INSTANT_INVITE: 1n" in render()["permissions.ts"]
    assert "MANAGE_GUILD_EXPRESSIONS: 1073741824n" in render()["permissions.ts"]
    assert "pub const CREATE_INSTANT_INVITE: u64 = 1;" in render_rust()
    assert "pub const MANAGE_GUILD_EXPRESSIONS: u64 = 1073741824;" in render_rust()
    assert "static const createInstantInvite = 1;" in render_dart()
    assert "static const manageGuildExpressions = 1073741824;" in render_dart()


def test_permission_schema_is_generated_for_every_client_protocol() -> None:
    assert (
        "export const PERMISSION_SCHEMA = 'kaede-permissions-v1' as const;"
        in render()["permissions.ts"]
    )
    assert 'pub const PERMISSION_SCHEMA: &str = "kaede-permissions-v1";' in render_rust()
    assert 'const permissionSchema = "kaede-permissions-v1";' in render_dart()
    assert 'PERMISSION_SCHEMA = "kaede-permissions-v1"' in render_python()


def test_priority_speaker_contract_is_generated_for_every_client_protocol() -> None:
    assert "PRIORITY_SPEAKER_TOPIC = 'kaede.priority-speaker.v1'" in render()["ops.ts"]
    assert "PRIORITY_SPEAKER_ACTIVE_PAYLOAD = [1]" in render()["ops.ts"]
    assert 'PRIORITY_SPEAKER_TOPIC: &str = "kaede.priority-speaker.v1"' in render_rust()
    assert "prioritySpeakerActivePayload = <int>[1]" in render_dart()
    assert 'PRIORITY_SPEAKER_TOPIC = "kaede.priority-speaker.v1"' in render_python()
    assert 'PRIORITY_SPEAKER_ACTIVE_PAYLOAD = b"\\x01"' in render_python()
