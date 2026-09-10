from datetime import UTC, datetime, timedelta

from app.api.interactions import InteractionResponseEdit, edit_ephemeral_message_payload


def test_clearing_controls_removes_retained_lifetime():
    now = datetime.now(UTC)
    result = edit_ephemeral_message_payload(
        {
            "content": "Confirm pairing",
            "components": [],
            "view_version": 4,
            "view_timeout_seconds": 180,
            "view_persistent": False,
            "view_expires_at": (now + timedelta(minutes=3)).isoformat(),
        },
        InteractionResponseEdit(
            content="Channel pair saved.",
            components=[],
            view_version=4,
            view_timeout_seconds=180,
            view_persistent=False,
        ),
        interaction_expires_at=now + timedelta(minutes=10),
        now=now,
    )
    assert result["components"] == []
    assert result["content"] == "Channel pair saved."
    assert not any(key.startswith("view_") for key in result)
