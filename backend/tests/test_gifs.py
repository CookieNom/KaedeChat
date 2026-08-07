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


def test_klipy_response_parser_accepts_current_static_media_host() -> None:
    payload = {
        "result": True,
        "data": {
            "has_next": False,
            "data": [
                {
                    "id": 42,
                    "title": "Current response",
                    "file": {
                        "md": {
                            "webp": {
                                "url": "https://static.klipy.com/ii/example/full.webp",
                                "width": 498,
                                "height": 314,
                            }
                        },
                        "sm": {
                            "webp": {
                                "url": "https://static.klipy.com/ii/example/preview.webp",
                                "width": 220,
                                "height": 138,
                            }
                        },
                    },
                }
            ],
        },
    }

    items, has_next = parse_klipy_items(payload)

    assert has_next is False
    assert items == [
        {
            "id": "42",
            "title": "Current response",
            "url": "https://static.klipy.com/ii/example/full.webp",
            "preview_url": "https://static.klipy.com/ii/example/preview.webp",
            "width": 498,
            "height": 314,
        }
    ]
