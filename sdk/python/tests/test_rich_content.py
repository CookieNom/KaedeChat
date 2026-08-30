import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from kaede_bot import (
    ActionRow,
    Button,
    ButtonStyle,
    ChannelSelect,
    CheckboxV2,
    ChoiceOption,
    Container,
    Embed,
    EmbedAuthor,
    EmbedField,
    EmbedFooter,
    EmbedMedia,
    EntityRef,
    Interaction,
    Message,
    MentionableSelect,
    Label,
    RadioGroup,
    Section,
    Modal,
    PartialEmoji,
    Poll,
    PollAnswer,
    PollMedia,
    RoleSelect,
    SelectDefaultValue,
    SelectOption,
    StringSelect,
    TextInput,
    TextInputStyle,
    TextDisplay,
    UserSelect,
    View,
    serialize_embeds,
    validate_embeds,
)


def test_embed_serialization_matches_backend_wire_shape() -> None:
    embed = Embed(
        title="Build complete",
        description="All checks passed",
        timestamp=datetime(2026, 8, 26, tzinfo=UTC),
        color=0x57F287,
        author=EmbedAuthor("Kaede", icon_url="attachment://avatar.png"),
        footer=EmbedFooter("CI"),
        image=EmbedMedia("attachment://graph.png"),
        fields=[EmbedField("Tests", "128", inline=True)],
    )

    assert embed.character_count == 46
    assert serialize_embeds([embed]) == [
        {
            "title": "Build complete",
            "description": "All checks passed",
            "timestamp": "2026-08-26T00:00:00+00:00",
            "color": 0x57F287,
            "footer": {"text": "CI"},
            "image": {"url": "attachment://graph.png"},
            "author": {"name": "Kaede", "icon_url": "attachment://avatar.png"},
            "fields": [{"name": "Tests", "value": "128", "inline": True}],
        }
    ]


def test_sdk_enforces_embed_limits_before_transport() -> None:
    with pytest.raises(ValueError):
        Embed(title="x" * 257)
    with pytest.raises(ValueError, match="6000"):
        validate_embeds(
            [Embed(description="a" * 3_001), Embed(description="b" * 3_000)]
        )
    with pytest.raises(ValueError):
        Embed(url="javascript:alert(1)")
    with pytest.raises(ValueError):
        Embed(url="https://chat.example:bad")


def test_button_and_view_serialize_with_stable_types() -> None:
    view = View(timeout=None)
    view.add_row(
        ActionRow(
            [
                Button(
                    style=ButtonStyle.primary,
                    label="Details",
                    custom_id="build:details",
                )
            ]
        )
    )
    view.add_row(ActionRow([Button(label="Watch", custom_id="build:watch")]))

    assert view.is_persistent
    assert view.to_components()[0]["components"][0]["type"] == 2
    assert view.to_components()[1]["components"][0]["custom_id"] == "build:watch"
    with pytest.raises(ValueError, match="86400"):
        View(timeout=86_401)


def test_link_buttons_and_action_rows_fail_closed() -> None:
    assert Button(style=ButtonStyle.link, label="Docs", url="https://docs.example")
    with pytest.raises(ValueError, match="link button"):
        Button(style=ButtonStyle.link, label="Docs", custom_id="docs")
    with pytest.raises(ValueError, match="cannot share"):
        ActionRow(
            [
                Button(label="One", custom_id="one"),
                StringSelect(
                    custom_id="two",
                    options=[SelectOption("Two", "two")],
                ),
            ]
        )


def test_select_serialization_and_typed_defaults() -> None:
    select = StringSelect(
        custom_id="fruit",
        min_values=1,
        max_values=2,
        options=[
            SelectOption("Apple", "apple", default=True),
            SelectOption("Pear", "pear"),
        ],
    )
    assert select.to_dict()["options"] == [
        {"label": "Apple", "value": "apple", "default": True},
        {"label": "Pear", "value": "pear", "default": False},
    ]
    channel = ChannelSelect(
        custom_id="channel",
        channel_types=[0, 15, 17],
        default_values=[SelectDefaultValue(EntityRef(1, "chat.example"), "channel")],
    )
    assert channel.to_dict()["default_values"] == [
        {"id": "1@chat.example", "type": "channel"}
    ]
    with pytest.raises(ValueError, match="incompatible"):
        ChannelSelect(
            custom_id="channel",
            default_values=[SelectDefaultValue(EntityRef(1, "chat.example"), "user")],
        )


def test_all_discord_auto_selects_have_stable_wire_tags() -> None:
    user = UserSelect(
        custom_id="user",
        default_values=[SelectDefaultValue(EntityRef(1, "chat.example"), "user")],
    )
    role = RoleSelect(
        custom_id="role",
        default_values=[SelectDefaultValue(EntityRef(2, "chat.example"), "role")],
    )
    mentionable = MentionableSelect(
        custom_id="mentionable",
        max_values=2,
        default_values=[
            SelectDefaultValue(EntityRef(1, "chat.example"), "user"),
            SelectDefaultValue(EntityRef(2, "chat.example"), "role"),
        ],
    )
    assert [
        user.to_dict()["type"],
        role.to_dict()["type"],
        mentionable.to_dict()["type"],
    ] == [
        5,
        6,
        7,
    ]
    with pytest.raises(ValueError, match="unsupported channel type"):
        ChannelSelect(custom_id="channel", channel_types=[9])


def test_text_inputs_are_modal_only_and_ids_are_unique() -> None:
    row = ActionRow(
        [
            TextInput(
                "reason",
                "Reason",
                style=TextInputStyle.paragraph,
                min_length=2,
                max_length=200,
            )
        ]
    )
    modal = Modal("Report", "report", [row])
    assert modal.to_dict()["components"][0]["components"][0]["type"] == 4
    with pytest.raises(ValueError, match="only valid in modals"):
        View([row])
    with pytest.raises(ValueError, match="unique"):
        Modal(
            "Duplicate",
            "duplicate",
            [
                ActionRow([TextInput("same", "One")]),
                Label("Two", CheckboxV2("same")),
            ],
        )


def test_components_v2_and_official_modal_inputs_serialize() -> None:
    view = View(
        [
            Container(
                [
                    Section(
                        [TextDisplay("**Ready**")],
                        Button(label="Run", custom_id="run"),
                    )
                ]
            )
        ]
    )
    assert view.to_components()[0]["type"] == 17
    modal = Modal(
        "Pick",
        "pick",
        [
            TextDisplay("Choose carefully"),
            Label(
                "Environment",
                RadioGroup(
                    "environment",
                    [
                        ChoiceOption("Production", "prod"),
                        ChoiceOption("Staging", "stage"),
                    ],
                ),
            ),
            Label("Confirm", CheckboxV2("confirm")),
        ],
    )
    assert [item["type"] for item in modal.to_dict()["components"]] == [10, 18, 18]
    premium = Button(style=ButtonStyle.premium, sku_id=EntityRef(1, "chat.example"))
    assert premium.to_dict()["sku_id"] == "1@chat.example"
    with pytest.raises(ValueError, match="premium button"):
        Button(
            style=ButtonStyle.premium,
            sku_id=EntityRef(1, "chat.example"),
            label="Buy",
        )


def test_poll_serialization_matches_discord_create_shape() -> None:
    poll = Poll(
        question=PollMedia(text="Ship it?"),
        answers=[
            PollAnswer(PollMedia(text="Yes")),
            PollAnswer(PollMedia(emoji=PartialEmoji(name="👎"))),
        ],
        duration=24,
    )

    assert poll.to_dict() == {
        "question": {"text": "Ship it?"},
        "answers": [
            {"poll_media": {"text": "Yes"}},
            {
                "poll_media": {
                    "emoji": {"animated": False, "name": "👎"},
                }
            },
        ],
        "duration": 24,
        "allow_multiselect": False,
        "layout_type": 1,
    }
    with pytest.raises(ValueError, match="55"):
        PollAnswer(PollMedia(text="x" * 56))


def test_custom_emoji_uses_a_composite_federated_reference() -> None:
    emoji = PartialEmoji(EntityRef(12, "emoji.example"), "wave", animated=True)
    assert emoji.to_dict() == {
        "id": "12@emoji.example",
        "name": "wave",
        "animated": True,
    }


def test_interaction_preserves_ephemeral_view_identity() -> None:
    interaction = Interaction.from_payload(
        None,  # type: ignore[arg-type]
        "chat.example",
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "context": "private_channel",
            "integration_type": "user_install",
            "user_installation_id": "81",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
                "display_name": "Member",
            },
            "response_id": "42",
            "view_version": 3,
            "target_ref": "55@chat.example",
            "target_id": "55",
            "resolved": {"users": {"55@chat.example": {"id": "55"}}},
            "custom_id": "build:details",
            "component_type": 3,
        },
    )
    assert interaction.response_id == 42
    assert interaction.view_version == 3
    assert interaction.context == "private_channel"
    assert interaction.integration_type == "user_install"
    assert interaction.user_installation_id == 81
    assert interaction.target_ref == EntityRef(55, "chat.example")
    assert interaction.target_id == 55
    assert interaction.resolved == {"users": {"55@chat.example": {"id": "55"}}}
    assert interaction.component_type == 3


@pytest.mark.asyncio
async def test_interaction_deferred_update_uses_callback_type_six() -> None:
    calls: list[tuple[int, int, dict[str, Any], str]] = []

    class CallbackClient:
        async def interaction_callback(
            self,
            interaction_id: int,
            callback_type: int,
            data: dict[str, Any],
            *,
            target: str,
        ) -> None:
            calls.append((interaction_id, callback_type, data, target))

    interaction = Interaction.from_payload(
        CallbackClient(),  # type: ignore[arg-type]
        "chat.example",
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "message_ref": "8@chat.example",
            "type": "component",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
                "display_name": "Member",
            },
        },
    )

    await interaction.defer_update()

    assert calls == [(90, 6, {}, "chat.example")]


@pytest.mark.asyncio
async def test_interaction_update_message_registers_exact_private_source_view() -> None:
    calls: list[tuple[int, int, dict[str, Any], str]] = []
    registrations: list[dict[str, Any]] = []
    timeout_edits: list[tuple[str, str, int | None]] = []

    class UpdateClient:
        async def interaction_callback(
            self,
            interaction_id: int,
            callback_type: int,
            data: dict[str, Any],
            *,
            target: str,
        ) -> dict[str, Any]:
            calls.append((interaction_id, callback_type, data, target))
            return {
                "id": "42",
                "interaction_id": "90",
                "view_version": 4,
                "content": "Updated",
            }

        def _view_timeout_editor(
            self,
            path: str,
            *,
            target: str,
            view_version: int | None,
        ) -> Any:
            async def edit(view: View) -> None:
                del view
                timeout_edits.append((path, target, view_version))

            return edit

        def add_view(self, view: View, **kwargs: Any) -> None:
            registrations.append({"view": view, **kwargs})

    interaction = Interaction.from_payload(
        UpdateClient(),  # type: ignore[arg-type]
        "chat.example",
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "response_id": "42",
            "view_version": 3,
            "type": "component",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
                "display_name": "Member",
            },
        },
    )
    view = View(
        rows=[ActionRow([Button(custom_id="next", label="Next")])],
        timeout=30,
    )

    result = await interaction.update_message(
        content="Updated",
        embeds=[Embed(title="Status")],
        view=view,
        attachment_ids=[70],
    )

    assert result["id"] == "42"
    assert calls == [
        (
            90,
            7,
            {
                "content": "Updated",
                "embeds": [{"title": "Status"}],
                "components": view.to_components(),
                "view_persistent": False,
                "view_timeout_seconds": 30,
                "view_version": 3,
                "attachment_ids": ["70"],
            },
            "chat.example",
        )
    ]
    assert registrations[0]["view"] is view
    assert registrations[0]["response_id"] == 42
    assert "message_id" not in registrations[0]
    await registrations[0]["timeout_editor"](view)
    assert timeout_edits == [
        (
            "/api/v1/bots/interactions/90/responses/@original",
            "chat.example",
            4,
        )
    ]


@pytest.mark.asyncio
async def test_interaction_edit_message_alias_registers_public_source_view() -> None:
    registrations: list[dict[str, Any]] = []
    source = Message(
        client=SimpleNamespace(),  # type: ignore[arg-type]
        target="chat.example",
        ref=EntityRef(8, "chat.example"),
        channel_ref=EntityRef(7, "chat.example"),
        author=None,
        content="Updated",
        created_at=None,
        attachments=[],
        view_version=5,
        interaction_id=90,
    )

    class UpdateClient:
        async def interaction_callback(self, *args: Any, **kwargs: Any) -> Message:
            return source

        def _view_timeout_editor(self, *args: Any, **kwargs: Any) -> Any:
            async def edit(view: View) -> None:
                del view

            return edit

        def add_view(self, view: View, **kwargs: Any) -> None:
            registrations.append({"view": view, **kwargs})

    interaction = Interaction.from_payload(
        UpdateClient(),  # type: ignore[arg-type]
        "chat.example",
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "message_ref": "8@chat.example",
            "view_version": 4,
            "type": "component",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
                "display_name": "Member",
            },
        },
    )
    view = View(rows=[ActionRow([Button(custom_id="next", label="Next")])])

    assert await interaction.edit_message(view=view) is source
    assert registrations[0]["view"] is view
    assert registrations[0]["message_id"] == EntityRef(8, "chat.example")
    assert "response_id" not in registrations[0]


@pytest.mark.asyncio
async def test_interaction_update_message_clears_private_view_registration() -> None:
    removed: list[int] = []

    class UpdateClient:
        async def interaction_callback(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            return {"id": "42", "interaction_id": "90", "view_version": 4}

        def remove_response_view(self, response_id: int, *, target: str) -> None:
            removed.append(response_id)

    interaction = Interaction.from_payload(
        UpdateClient(),  # type: ignore[arg-type]
        "chat.example",
        {
            "id": "90",
            "application_ref": "1@apps.example",
            "channel_ref": "7@chat.example",
            "response_id": "42",
            "view_version": 3,
            "type": "component",
            "user": {
                "id": "3",
                "origin_domain": "chat.example",
                "username": "member",
                "display_name": "Member",
            },
        },
    )

    await interaction.update_message(view=View(rows=[]))

    assert removed == [42]
    with pytest.raises(ValueError, match="at least one"):
        await interaction.update_message()
    with pytest.raises(ValueError, match="view_version requires a components update"):
        await interaction.update_message(view_version=3)


def test_view_check_error_timeout_disable_and_cleanup_lifecycle() -> None:
    class LifecycleView(View):
        allowed = False
        errors: list[str]
        timeout_called = False

        def __post_init__(self) -> None:
            super().__post_init__()
            self.errors = []

        async def interaction_check(self, interaction: Any) -> bool:
            del interaction
            return self.allowed

        async def on_error(self, interaction: Any, error: Exception, item: Any) -> None:
            del interaction, item
            self.errors.append(str(error))

        async def on_timeout(self) -> None:
            self.timeout_called = True

    async def exercise() -> None:
        calls: list[str] = []
        view = LifecycleView(
            [ActionRow([Button(label="Run", custom_id="run")])],
            timeout=0.01,
        )

        async def callback(interaction: Any) -> None:
            calls.append(interaction.custom_id)
            raise RuntimeError("callback failed")

        view.set_callback("run", callback)
        interaction = SimpleNamespace(custom_id="run")
        assert await view.dispatch(interaction) is False
        assert calls == []
        view.allowed = True
        assert await view.dispatch(interaction) is True
        assert calls == ["run"]
        assert view.errors == ["callback failed"]
        cleanup: list[bool] = []
        timeout_actions: list[bool] = []

        async def timeout_action() -> None:
            timeout_actions.append(True)

        view._start_listening(
            lambda: cleanup.append(True), timeout_action=timeout_action
        )
        assert await view.wait() is True
        assert view.timeout_called is True
        assert view.rows[0].components[0].disabled is True
        assert cleanup == [True]
        assert timeout_actions == [True]
        assert view.is_finished() is True

        stopped = View(
            [ActionRow([Button(label="Stop", custom_id="stop")])],
            timeout=10,
        )
        stopped_cleanup: list[bool] = []
        stopped._start_listening(lambda: stopped_cleanup.append(True))
        waiter = asyncio.create_task(stopped.wait())
        stopped.stop()
        assert await waiter is False
        assert stopped_cleanup == [True]

    asyncio.run(exercise())
