from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.interactions import (
    InteractionCreate,
    UserInstallationCreate,
    UserInstallationPatch,
    ephemeral_message_payload,
    merge_application_commands,
    private_interaction_context,
    resolve_context_command_target,
)
from app.api.webhooks import (
    SlackWebhookExecute,
    WebhookExecute,
    WebhookMessageEdit,
    create_webhook_attachment_ticket,
    execute_webhook,
    github_webhook_embed,
    has_interactive_components,
    slack_embeds,
    validate_webhook_components_v2_body,
)
from app.chat.message_flags import MESSAGE_FLAG_IS_COMPONENTS_V2
from app.chat.rich_content import (
    ActionRow,
    Button,
    ChannelSelect,
    CheckboxV2,
    Embed,
    EmbedAuthor,
    EmbedField,
    EmbedFooter,
    EmbedMedia,
    FileComponent,
    Label,
    MentionableSelect,
    Modal,
    PartialEmoji,
    PollAnswer,
    PollCreate,
    PollMedia,
    RoleSelect,
    Section,
    SelectDefaultValue,
    SelectOption,
    StringSelect,
    TextDisplay,
    TextInput,
    UnfurledMediaItem,
    UserSelect,
    validate_attachment_url_references,
)
from app.chat.schemas import MessageCreate, MessageEdit
from app.core.types import EntityRef
from app.db.models import User
from app.media.schemas import UploadTicketRequest


def test_message_create_accepts_and_serializes_rich_only_body() -> None:
    message = MessageCreate(
        embeds=[
            Embed(
                title="Build complete",
                description="All checks passed",
                url="https://chat.example/build/1",
                timestamp=datetime(2026, 8, 26, tzinfo=UTC),
                color=0x57F287,
                author=EmbedAuthor(name="Kaede", icon_url="attachment://avatar.png"),
                footer=EmbedFooter(text="CI"),
                image=EmbedMedia(url="attachment://graph.png"),
                fields=[EmbedField(name="Tests", value="128", inline=True)],
            )
        ],
        components=[
            ActionRow(components=[Button(style=1, label="Details", custom_id="build:details")]),
            ActionRow(components=[Button(style=2, label="Watch", custom_id="build:watch")]),
        ],
    )

    payload = message.model_dump(mode="json", exclude_none=True)

    assert payload["embeds"][0]["timestamp"] == "2026-08-26T00:00:00Z"
    assert payload["components"][0]["type"] == 1
    assert payload["components"][0]["components"][0]["type"] == 2
    assert payload["components"][1]["components"][0]["type"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"components": [{"type": True, "components": []}]},
        {
            "components": [
                {
                    "type": 1,
                    "components": [{"type": 2, "style": True, "label": "No", "custom_id": "no"}],
                }
            ]
        },
        {
            "components": [
                {
                    "type": 1,
                    "components": [{"type": 8, "custom_id": "where", "channel_types": [True]}],
                }
            ]
        },
        {
            "poll": {
                "question": {"text": "Strict?"},
                "answers": [
                    {"poll_media": {"text": "Yes"}},
                    {"poll_media": {"text": "No"}},
                ],
                "duration": True,
                "layout_type": 1,
            }
        },
        {
            "poll": {
                "question": {"text": "Strict?"},
                "answers": [
                    {"poll_media": {"text": "Yes"}},
                    {"poll_media": {"text": "No"}},
                ],
                "duration": 24,
                "layout_type": True,
            }
        },
    ],
)
def test_rich_integer_wire_fields_reject_json_booleans(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MessageCreate.model_validate(payload)


@pytest.mark.parametrize(
    "embed",
    [
        {"title": "x" * 257},
        {"description": "x" * 4_097},
        {"url": "javascript:alert(1)"},
        {"timestamp": datetime(2026, 8, 26)},
        {"fields": [{"name": "n", "value": "v"}] * 26},
        {},
    ],
)
def test_embed_rejects_invalid_shape(embed: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Embed.model_validate(embed)


@pytest.mark.parametrize("url", ["https://bad host.example", "https://chat.example:bad"])
def test_embed_rejects_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Embed(url=url)


@pytest.mark.parametrize("url", ["attachment://document.pdf", "attachment://vector.svg"])
def test_embed_attachment_media_accepts_only_discord_image_formats(url: str) -> None:
    with pytest.raises(ValidationError, match="JPG"):
        Embed(image=EmbedMedia(url=url))


def test_embed_collection_enforces_discord_total_character_limit() -> None:
    with pytest.raises(ValidationError, match="6000"):
        MessageCreate(embeds=[Embed(description="a" * 3_001), Embed(description="b" * 3_000)])


def test_attachment_rich_media_urls_must_name_an_attached_file() -> None:
    embeds = [Embed(image=EmbedMedia(url="attachment://graph.png"))]
    components = [{"type": 13, "file": {"url": "attachment://report.pdf"}}]

    validate_attachment_url_references(
        embeds=embeds,
        components=components,
        attachments=[
            SimpleNamespace(filename="graph.png"),
            {"filename": "report.pdf"},
        ],
    )

    with pytest.raises(ValueError, match="attached filename"):
        validate_attachment_url_references(
            embeds=embeds,
            components=components,
            attachments=[SimpleNamespace(filename="graph.png")],
        )


def test_partial_emoji_is_federation_safe_and_validated() -> None:
    emoji = PartialEmoji(id="12@emoji.example", name="wave", animated=True)
    assert emoji.model_dump(mode="json") == {
        "id": "12@emoji.example",
        "name": "wave",
        "animated": True,
    }
    with pytest.raises(ValidationError):
        PartialEmoji(animated=True)


def test_buttons_enforce_link_and_interactive_targets() -> None:
    assert Button(style=5, label="Docs", url="https://docs.example").type == 2
    with pytest.raises(ValidationError, match="link button"):
        Button(style=5, label="Docs", custom_id="docs")
    with pytest.raises(ValidationError, match="non-link button"):
        Button(style=1, label="Docs", url="https://docs.example")
    with pytest.raises(ValidationError, match="label or emoji"):
        Button(custom_id="empty")


def test_action_rows_reject_mixed_or_overcrowded_components() -> None:
    with pytest.raises(ValidationError, match="cannot share"):
        ActionRow(
            components=[
                Button(label="One", custom_id="one"),
                StringSelect(
                    custom_id="two",
                    options=[SelectOption(label="Two", value="two")],
                ),
            ]
        )
    with pytest.raises(ValidationError):
        ActionRow(components=[Button(label=str(index), custom_id=str(index)) for index in range(6)])


def test_selects_validate_options_ranges_and_typed_defaults() -> None:
    select = StringSelect(
        custom_id="fruit",
        min_values=1,
        max_values=2,
        options=[
            SelectOption(label="Apple", value="apple", default=True),
            SelectOption(label="Pear", value="pear"),
        ],
    )
    assert select.type == 3
    with pytest.raises(ValidationError, match="unique"):
        StringSelect(
            custom_id="fruit",
            options=[
                SelectOption(label="Apple", value="same"),
                SelectOption(label="Pear", value="same"),
            ],
        )
    with pytest.raises(ValidationError, match="reference channels"):
        ChannelSelect(
            custom_id="place",
            default_values=[SelectDefaultValue(id="1@chat.example", type="user")],
        )


def test_all_discord_auto_select_types_preserve_their_wire_tags() -> None:
    user = UserSelect(
        custom_id="user",
        default_values=[SelectDefaultValue(id="1@chat.example", type="user")],
    )
    role = RoleSelect(
        custom_id="role",
        default_values=[SelectDefaultValue(id="2@chat.example", type="role")],
    )
    mentionable = MentionableSelect(
        custom_id="mentionable",
        max_values=2,
        default_values=[
            SelectDefaultValue(id="1@chat.example", type="user"),
            SelectDefaultValue(id="2@chat.example", type="role"),
        ],
    )
    channel = ChannelSelect(custom_id="channel", channel_types=[0, 2, 15, 17])
    assert [user.type, role.type, mentionable.type, channel.type] == [5, 6, 7, 8]
    with pytest.raises(ValidationError, match="unsupported channel type"):
        ChannelSelect(custom_id="channel", channel_types=[9])


def test_text_inputs_are_modal_only_and_modal_ids_are_unique() -> None:
    row = ActionRow(
        components=[
            TextInput(
                custom_id="reason",
                label="Reason",
                style=2,
                min_length=2,
                max_length=200,
            )
        ]
    )
    assert Modal(title="Report", custom_id="report", components=[row]).components == [row]
    with pytest.raises(ValidationError, match="only valid in modals"):
        MessageCreate(components=[row])
    with pytest.raises(ValidationError, match="unique"):
        Modal(
            title="Duplicate",
            custom_id="duplicate",
            components=[
                ActionRow(components=[TextInput(custom_id="same", label="One")]),
                Label(label="Two", component=CheckboxV2(custom_id="same")),
            ],
        )


def test_message_component_ids_are_unique_across_rows() -> None:
    with pytest.raises(ValidationError, match="unique"):
        MessageCreate(
            components=[
                ActionRow(components=[Button(custom_id="same", label="One")]),
                ActionRow(
                    components=[
                        StringSelect(
                            custom_id="same",
                            options=[SelectOption(label="Two", value="two")],
                        )
                    ]
                ),
            ]
        )


def test_poll_wire_shape_and_discord_limits() -> None:
    poll = PollCreate(
        question=PollMedia(text="Ship it?"),
        answers=[
            PollAnswer(poll_media=PollMedia(text="Yes")),
            PollAnswer(poll_media=PollMedia(emoji=PartialEmoji(name="👎"))),
        ],
        duration=24,
        allow_multiselect=False,
    )
    payload = MessageCreate(poll=poll).model_dump(mode="json", exclude_none=True)
    assert payload["poll"]["layout_type"] == 1
    assert payload["poll"]["answers"][1]["poll_media"]["emoji"]["name"] == "👎"
    with pytest.raises(ValidationError, match="55"):
        PollAnswer(poll_media=PollMedia(text="x" * 56))
    with pytest.raises(ValidationError):
        PollCreate(
            question=PollMedia(text="Only one?"),
            answers=[PollAnswer(poll_media=PollMedia(text="Yes"))],
            duration=24,
        )

    with pytest.raises(ValidationError, match="cannot be added or replaced"):
        MessageEdit.model_validate({"poll": payload["poll"]})


@pytest.mark.parametrize(
    "rich",
    [
        {"embeds": [{"title": "Visible"}]},
        {
            "components": [
                {
                    "type": 1,
                    "components": [{"type": 2, "label": "Visible", "custom_id": "visible"}],
                }
            ]
        },
        {
            "poll": {
                "question": {"text": "Visible?"},
                "answers": [
                    {"poll_media": {"text": "Yes"}},
                    {"poll_media": {"text": "No"}},
                ],
                "duration": 24,
            }
        },
    ],
)
def test_e2ee_messages_reject_all_rich_plaintext(rich: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="rich plaintext"):
        MessageCreate.model_validate({"e2ee": {"version": 1, "ciphertext": "opaque"}, **rich})


def test_rich_models_reject_unknown_fields_and_nul() -> None:
    with pytest.raises(ValidationError):
        Embed(title="valid", provider={"name": "read-only"})
    with pytest.raises(ValidationError, match="NUL"):
        Button(label="bad\x00label", custom_id="bad")


def test_ephemeral_component_target_requires_response_identity_and_version() -> None:
    interaction = InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="component",
        response_id=42,
        view_version=3,
        custom_id="build:details",
    )
    assert interaction.response_id == 42
    assert interaction.view_version == 3
    with pytest.raises(ValidationError, match="view_version"):
        InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="component",
            response_id=42,
            custom_id="build:details",
        )
    with pytest.raises(ValidationError, match="exactly one"):
        InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="component",
            message_ref="9@chat.example",
            response_id=42,
            view_version=1,
            custom_id="build:details",
        )


def test_ephemeral_view_payload_has_bounded_public_version_fence() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    interaction_expiry = now + timedelta(minutes=15)
    message = MessageCreate(
        components=[
            ActionRow(components=[Button(style=1, label="Details", custom_id="build:details")])
        ],
        view_timeout_seconds=60,
    )
    payload = ephemeral_message_payload(
        message,
        flags=0,
        interaction_expires_at=interaction_expiry,
        now=now,
        version=4,
    )
    assert payload["flags"] == 64
    assert payload["view_version"] == 4
    assert payload["view_persistent"] is False
    assert payload["view_expires_at"] == (now + timedelta(minutes=1)).isoformat()


def test_autocomplete_requires_explicit_focused_option_and_generation_is_bounded() -> None:
    interaction = InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="autocomplete",
        command_name="search",
        focused_option="filters.query",
        autocomplete_generation=9,
        options={"filters": {"query": "kae"}},
    )
    assert interaction.focused_option == "filters.query"
    assert interaction.autocomplete_generation == 9
    with pytest.raises(ValidationError, match="focused_option"):
        InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="autocomplete",
            command_name="search",
        )


def test_context_commands_require_authority_resolved_targets() -> None:
    user_command = InteractionCreate(
        application_ref="1@apps.example",
        command_name="inspect",
        command_type="user",
        target_ref="22@chat.example",
    )
    assert str(user_command.target_ref) == "22@chat.example"

    message_command = InteractionCreate(
        application_ref="1@apps.example",
        command_name="summarize",
        command_type="message",
        target_ref="33@chat.example",
    )
    assert str(message_command.target_ref) == "33@chat.example"

    with pytest.raises(ValidationError, match="target_ref"):
        InteractionCreate(
            application_ref="1@apps.example",
            command_name="inspect",
            command_type="user",
        )
    with pytest.raises(ValidationError, match="target_ref"):
        InteractionCreate(
            application_ref="1@apps.example",
            command_name="search",
            target_ref="22@chat.example",
        )
    with pytest.raises(ValidationError, match="autocomplete"):
        InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="autocomplete",
            command_name="inspect",
            command_type="user",
            focused_option="query",
        )


@pytest.mark.asyncio
async def test_message_context_target_is_resolved_from_the_visible_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(
        id=33,
        origin_domain="chat.example",
        channel_id=7,
        channel_domain="chat.example",
        deleted_at=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=message))
    rendered = {
        "id": "33",
        "origin_domain": "chat.example",
        "channel_id": "7",
        "channel_domain": "chat.example",
        "content": "authority-owned body",
    }
    render = AsyncMock(return_value=rendered)
    monkeypatch.setattr("app.api.interactions.render_message_payload", render)
    access = SimpleNamespace(
        channel=SimpleNamespace(
            id=7,
            origin_domain="chat.example",
            encryption_mode="plaintext",
        ),
        guild=None,
        participants=[],
    )

    target_ref, target_id, resolved = await resolve_context_command_target(
        session,  # type: ignore[arg-type]
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        access,  # type: ignore[arg-type]
        SimpleNamespace(id=1, origin_domain="chat.example"),  # type: ignore[arg-type]
        "message",
        EntityRef("33@chat.example"),
    )

    assert target_ref == "33@chat.example"
    assert target_id == "33"
    assert resolved == {"messages": {"33@chat.example": rendered}}
    render.assert_awaited_once()


def test_user_install_grants_are_narrow_and_patchable() -> None:
    installation = UserInstallationCreate(
        application_ref="1@apps.example",
        scopes=["applications.commands", "interactions.respond"],
        contexts=["private_channel", "bot_dm"],
    )
    assert installation.scopes == ["applications.commands", "interactions.respond"]
    assert installation.contexts == ["private_channel", "bot_dm"]
    assert UserInstallationPatch(contexts=["bot_dm"]).contexts == ["bot_dm"]
    with pytest.raises(ValidationError, match="applications.commands"):
        UserInstallationCreate(
            application_ref="1@apps.example",
            scopes=["interactions.respond"],
        )
    with pytest.raises(ValidationError, match="at least one"):
        UserInstallationPatch()


def test_user_install_defaults_cover_all_discord_command_contexts() -> None:
    installation = UserInstallationCreate(application_ref="1@apps.example")
    assert installation.contexts == ["guild", "bot_dm", "private_channel"]
    assert installation.intents == ["interactions"]
    with pytest.raises(ValidationError, match="interactions intent"):
        UserInstallationPatch(intents=["guilds"])


def test_private_context_is_relative_to_selected_bot_and_direct_dm() -> None:
    human = User(
        id=1,
        origin_domain="chat.example",
        username="human",
        display_name="Human",
        is_local=True,
        account_type="human",
    )
    bot = User(
        id=2,
        origin_domain="chat.example",
        username="bot",
        display_name="Bot",
        is_local=True,
        account_type="bot",
    )
    assert private_interaction_context([human, bot], bot, direct=True) == "bot_dm"
    assert private_interaction_context([human, bot], bot, direct=False) == "private_channel"
    assert private_interaction_context([human], bot, direct=True) == "private_channel"


def test_guild_install_command_discovery_takes_precedence() -> None:
    guild = [
        {
            "application_ref": "1@apps.example",
            "name": "build",
            "type": "chat_input",
            "integration_type": "guild_install",
            "interaction_context": "guild",
        }
    ]
    user = [
        {
            "application_ref": "1@apps.example",
            "name": "build",
            "type": "chat_input",
            "integration_type": "user_install",
            "interaction_context": "guild",
        },
        {
            "application_ref": "2@apps.example",
            "name": "search",
            "type": "chat_input",
            "integration_type": "user_install",
            "interaction_context": "guild",
        },
    ]
    assert merge_application_commands(guild, user) == [guild[0], user[1]]


def test_user_install_command_projection_preserves_private_context() -> None:
    bot_dm = {
        "application_ref": "1@apps.example",
        "name": "build",
        "type": "chat_input",
        "integration_type": "user_install",
        "interaction_context": "bot_dm",
    }
    private_channel = {
        **bot_dm,
        "interaction_context": "private_channel",
    }

    assert merge_application_commands([bot_dm], []) == [bot_dm]
    assert merge_application_commands([private_channel], []) == [private_channel]


def test_webhook_attachment_selection_is_unique_and_edit_can_clear() -> None:
    execute = WebhookExecute(attachment_ids=["11", "12"])
    assert [int(item) for item in execute.attachment_ids] == [11, 12]
    assert WebhookMessageEdit(attachment_ids=[]).attachment_ids == []
    with pytest.raises(ValidationError, match="unique"):
        WebhookExecute(attachment_ids=["11", "11"])


def test_webhook_components_v2_execute_rejects_an_attachment() -> None:
    file_component = FileComponent(file=UnfurledMediaItem(url="attachment://report.pdf"))
    payload = WebhookExecute(
        components=[file_component],
        attachment_ids=["11"],
        flags=MESSAGE_FLAG_IS_COMPONENTS_V2,
    )

    with pytest.raises(HTTPException) as raised:
        validate_webhook_components_v2_body(
            flags=payload.flags,
            content=payload.content,
            embeds=payload.embeds,
            components=payload.components,
            attachment_ids=payload.attachment_ids,
            poll=payload.poll,
            sticker_ids=payload.sticker_ids,
        )

    assert raised.value.detail == {"code": "COMPONENTS_V2_BODY_INVALID"}


def test_webhook_component_tree_detects_only_interactive_custom_ids() -> None:
    display = TextDisplay(content="Status")
    interactive = Section(
        components=[display],
        accessory=Button(label="Acknowledge", custom_id="ack"),
    )

    assert not has_interactive_components([display])
    assert has_interactive_components([interactive])


def test_slack_and_github_compatibility_adapters_are_bounded_and_typed() -> None:
    slack = SlackWebhookExecute.model_validate(
        {
            "text": "Deploy finished",
            "username": "Release relay",
            "attachments": [
                {
                    "color": "good",
                    "title": "Build 42",
                    "title_link": "https://ci.example/build/42",
                    "text": "All checks passed",
                    "fields": [{"title": "Tests", "value": "128", "short": True}],
                }
            ],
            "channel": "#ignored-by-discord",
        }
    )
    embeds = slack_embeds(slack)
    assert embeds[0].title == "Build 42"
    assert embeds[0].color == 0x2EB886
    assert embeds[0].fields[0].inline is True
    with pytest.raises(ValidationError):
        SlackWebhookExecute.model_validate(
            {"attachments": [{"fallback": "invalid timestamp", "ts": True}]}
        )

    github = github_webhook_embed(
        "push",
        {
            "ref": "refs/heads/main",
            "compare": "https://github.example/org/repo/compare/1...2",
            "commits": [{"id": "2"}],
            "head_commit": {"message": "Ship webhook parity"},
            "repository": {
                "full_name": "org/repo",
                "html_url": "https://github.example/org/repo",
            },
            "sender": {
                "login": "octocat",
                "html_url": "https://github.example/octocat",
                "avatar_url": "https://github.example/octocat.png",
            },
        },
    )
    assert github.title == "octocat pushed 1 commit to refs/heads/main"
    assert github.description == "Ship webhook parity"
    with pytest.raises(HTTPException) as unsupported:
        github_webhook_embed("deployment", {})
    assert unsupported.value.detail["code"] == "GITHUB_WEBHOOK_EVENT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_incoming_webhook_rejects_interactive_components_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        id=7,
        type=1,
        application_id=None,
        application_domain=None,
    )
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=item))

    with pytest.raises(HTTPException) as denied:
        await execute_webhook(
            webhook_id=7,
            payload=WebhookExecute(
                components=[ActionRow(components=[Button(label="Run", custom_id="run")])]
            ),
            request=SimpleNamespace(),  # type: ignore[arg-type]
            wait=True,
            thread_id=None,
            with_components=True,
            idempotency_key=None,
            path_token="secret",
            authorization=None,
            session=SimpleNamespace(),  # type: ignore[arg-type]
            redis=SimpleNamespace(),  # type: ignore[arg-type]
            snowflake=AsyncMock(),  # type: ignore[arg-type]
            settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        )

    assert denied.value.detail["code"] == "WEBHOOK_COMPONENT_APPLICATION_REQUIRED"


def test_execute_webhook_rejects_unsupported_forward_fields() -> None:
    with pytest.raises(ValidationError, match="forwarded_message_id"):
        WebhookExecute.model_validate(
            {
                "content": "not a forward",
                "forwarded_message_id": "9",
                "forward_source_proof": {"proof": "unsupported"},
            }
        )


@pytest.mark.asyncio
async def test_application_webhook_uses_shared_authoritative_message_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        id=7,
        guild_id=1,
        guild_domain="chat.example",
        channel_id=2,
        channel_domain="chat.example",
        creator_id=3,
        creator_domain="apps.example",
        name="Build hook",
        avatar_hash="a" * 64,
        type=3,
        application_id=8,
        application_domain="apps.example",
    )
    channel = SimpleNamespace(
        id=2,
        origin_domain="chat.example",
        type=0,
        encryption_mode="plaintext",
        encryption_policy_generation=0,
        encryption_epoch=None,
        encryption_group_id=None,
    )
    creator = SimpleNamespace(
        id=3,
        origin_domain="apps.example",
        account_type="bot",
        is_local=False,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=creator))
    create = AsyncMock(return_value={"id": "90", "origin_domain": "chat.example"})
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=item))
    monkeypatch.setattr(
        "app.api.webhooks.local_guild",
        AsyncMock(return_value=SimpleNamespace(id=1, origin_domain="chat.example")),
    )
    monkeypatch.setattr("app.api.webhooks.guild_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr("app.api.webhooks.validate_message_encryption_policy", lambda *a, **k: None)
    monkeypatch.setattr("app.api.webhooks.create_message", create)

    result = await execute_webhook(
        webhook_id=7,
        payload=WebhookExecute(
            components=[ActionRow(components=[Button(label="Run", custom_id="run")])],
            avatar_url="https://cdn.example/" + "a" * 256,
            tts=True,
        ),
        request=SimpleNamespace(),  # type: ignore[arg-type]
        wait=True,
        thread_id=None,
        with_components=True,
        idempotency_key="deploy",
        path_token="secret",
        authorization=None,
        session=session,  # type: ignore[arg-type]
        redis=SimpleNamespace(eval=AsyncMock(return_value=1)),  # type: ignore[arg-type]
        snowflake=AsyncMock(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert result["id"] == "90"
    sent = create.await_args.args[1]
    admission = create.await_args.args[-1]
    assert sent.tts is True
    assert sent.components[0].components[0].custom_id == "run"
    assert admission.application_id == 8
    assert admission.webhook_avatar_hash is None
    assert admission.webhook_avatar_url.startswith("https://cdn.example/")
    assert admission.required_attachment_purpose == "webhook_attachment"
    assert admission.skip_client_rate_limit is True


@pytest.mark.asyncio
async def test_forum_webhook_execution_uses_atomic_thread_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook = SimpleNamespace(
        id=7,
        guild_id=1,
        channel_id=2,
        channel_domain="chat.example",
        creator_id=3,
        creator_domain="chat.example",
        name="Build hook",
        avatar_hash=None,
        type=1,
        application_id=None,
        application_domain=None,
    )
    forum = SimpleNamespace(
        id=2,
        origin_domain="chat.example",
        type=15,
        encryption_mode="plaintext",
        encryption_policy_generation=0,
        encryption_epoch=None,
        encryption_group_id=None,
    )
    creator = SimpleNamespace(
        id=3,
        origin_domain="chat.example",
        account_type="human",
        is_local=True,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=creator))
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    create_thread = AsyncMock(
        return_value={"message": {"id": "90", "origin_domain": "chat.example"}}
    )
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=webhook))
    monkeypatch.setattr(
        "app.api.webhooks.local_guild",
        AsyncMock(return_value=SimpleNamespace(id=1, origin_domain="chat.example")),
    )
    monkeypatch.setattr("app.api.webhooks.guild_channel", AsyncMock(return_value=forum))
    monkeypatch.setattr("app.api.webhooks.validate_message_encryption_policy", lambda *a, **k: None)
    monkeypatch.setattr("app.api.threads.create_thread_service", create_thread)

    result = await execute_webhook(
        webhook_id=7,
        payload=WebhookExecute(
            content="Release",
            attachment_ids=["41"],
            thread_name="Release notes",
            applied_tags=["5"],
        ),
        request=SimpleNamespace(),  # type: ignore[arg-type]
        wait=True,
        thread_id=None,
        with_components=False,
        idempotency_key="release-1",
        path_token="secret",
        authorization=None,
        session=session,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        snowflake=AsyncMock(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert result == {"id": "90", "origin_domain": "chat.example"}
    thread_payload = create_thread.await_args.args[1]
    admission = create_thread.await_args.kwargs["starter_admission_options"]
    assert thread_payload.name == "Release notes"
    assert [int(item) for item in thread_payload.applied_tag_ids] == [5]
    assert [int(item) for item in thread_payload.message.attachment_ids] == [41]
    assert admission.webhook_id == 7
    assert admission.webhook_channel_id == 2
    assert admission.webhook_channel_domain == "chat.example"
    assert admission.required_attachment_binding_prefix == "webhook-stage:7:"


@pytest.mark.asyncio
async def test_webhook_attachment_ticket_is_bound_to_exact_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook = SimpleNamespace(
        id=7,
        creator_id=3,
        creator_domain="chat.example",
        channel_id=2,
        channel_domain="chat.example",
    )
    target = SimpleNamespace(id=2, origin_domain="chat.example")
    creator = SimpleNamespace(
        id=3,
        origin_domain="chat.example",
        account_type="human",
        is_local=True,
    )
    attachment = SimpleNamespace(id=41, asset_binding=None, upload_expires_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=creator), commit=AsyncMock())
    monkeypatch.setattr("app.api.webhooks.token_webhook", AsyncMock(return_value=webhook))
    monkeypatch.setattr(
        "app.api.webhook_e2ee.webhook_e2ee_target_channel",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(
        "app.api.webhooks.create_upload_ticket",
        AsyncMock(return_value=(attachment, "https://uploads.example/41")),
    )
    monkeypatch.setattr(
        "app.api.webhooks.ticket_payload",
        lambda item, upload_url: {
            "id": str(item.id),
            "upload_url": upload_url,
            "media_origin": "https://uploads.example",
        },
    )

    result = await create_webhook_attachment_ticket(
        webhook_id=7,
        path_token="secret",
        payload=UploadTicketRequest(
            filename="report.txt",
            content_type="text/plain",
            size=5,
            encryption_mode="plaintext",
        ),
        session=session,  # type: ignore[arg-type]
        snowflake=AsyncMock(),  # type: ignore[arg-type]
        settings=SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert result["upload_url"] == "https://uploads.example/41"
    assert result["media_origin"] == "https://uploads.example"
    assert attachment.asset_binding == "webhook-stage:7:41"
    session.commit.assert_awaited_once()
