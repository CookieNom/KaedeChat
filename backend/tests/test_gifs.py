from app.api.gifs import parse_klipy_items


def test_klipy_response_parser_only_accepts_provider_https_media() -> None:
    payload = {
        "result": True,
        "data": {
            "has_next": True,
            "data": [
                {
                    "id": "safe",
                    "title": "Waving",
                    "file": {
                        "md": {
                            "gif": {
                                "url": "https://media.klipy.com/example/wave.gif",
                                "width": 320,
                                "height": 180,
                            }
                        },
                        "sm": {"webp": {"url": "https://media.klipy.com/example/wave.webp"}},
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
    assert has_next is True
    assert items == [
        {
            "id": "safe",
            "title": "Waving",
            "url": "https://media.klipy.com/example/wave.gif",
            "preview_url": "https://media.klipy.com/example/wave.webp",
            "width": 320,
            "height": 180,
        }
    ]
