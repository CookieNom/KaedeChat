import pytest

from app.api.gifs import parse_klipy_items


@pytest.mark.parametrize(
    ("host", "format_", "identifier", "has_next_page"),
    [("media.klipy.com", "gif", "safe", True), ("static.klipy.com", "webp", 42, False)],
)
def test_klipy_response_parser_only_accepts_provider_https_media(
    host: str, format_: str, identifier: str | int, has_next_page: bool
) -> None:
    payload = {
        "result": True,
        "data": {
            "has_next": has_next_page,
            "data": [
                {
                    "id": identifier,
                    "title": "Waving",
                    "file": {
                        "md": {
                            format_: {
                                "url": f"https://{host}/example/wave.{format_}",
                                "width": 320,
                                "height": 180,
                            }
                        },
                        "sm": {"webp": {"url": f"https://{host}/example/wave.webp"}},
                    },
                },
                {
                    "id": "unsafe",
                    "file": {"md": {"gif": {"url": "https://evil.example/tracker.gif"}}},
                },
            ],
        },
    }
    items, has_next = parse_klipy_items(payload)
    assert has_next is has_next_page
    assert items == [
        {
            "id": str(identifier),
            "title": "Waving",
            "url": f"https://{host}/example/wave.{format_}",
            "preview_url": f"https://{host}/example/wave.webp",
            "width": 320,
            "height": 180,
        }
    ]

    from copy import deepcopy

    for size, invalid_format, invalid_url in (
        ("md", format_, f"http://{host}/example/wave.{format_}"),
        ("sm", "webp", "https://evil.example/tracker.webp"),
    ):
        invalid = deepcopy(payload)
        invalid["data"]["data"] = [invalid["data"]["data"][0]]
        invalid["data"]["data"][0]["file"][size][invalid_format]["url"] = invalid_url
        assert parse_klipy_items(invalid)[0] == []
