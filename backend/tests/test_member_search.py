from app.api.moderation import _federated_username_parts


def test_federated_username_search_parses_complete_handles_only() -> None:
    assert _federated_username_parts(" @cookie@remote.example ") == (
        "cookie",
        "remote.example",
    )
    assert _federated_username_parts("cookie") is None
    assert _federated_username_parts("@remote.example") is None
