from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.link_previews import PreviewRequest, normalize_preview_url, preview_metadata


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:password@example.com/",
        "https://example.com:8443/",
        "//example.com/no-scheme",
    ],
)
def test_preview_url_rejects_unsafe_shapes(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_preview_url(value)


def test_preview_url_is_canonical_and_drops_fragments() -> None:
    assert normalize_preview_url("HTTPS://Example.COM/post?q=1#section") == (
        "https://example.com/post?q=1"
    )


def test_preview_request_rejects_nul_text_before_url_processing() -> None:
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate({"url": "https://example.com/\x00preview"})


def test_preview_metadata_extracts_bounded_open_graph_values() -> None:
    result = preview_metadata(
        """
        <html><head>
          <title>Fallback title</title>
          <meta property="og:title" content="A useful page">
          <meta property="og:description" content="  A   compact description.  ">
          <meta property="og:site_name" content="Example">
          <meta property="og:image" content="/preview.png">
        </head></html>
        """,
        "https://example.com/articles/one",
    )
    assert result == {
        "title": "A useful page",
        "description": "A compact description.",
        "site_name": "Example",
        "media_source": "https://example.com/preview.png",
        "media_type": "image",
    }


def test_preview_metadata_never_returns_unsafe_media_schemes() -> None:
    result = preview_metadata(
        '<meta property="og:image" content="file:///etc/passwd">',
        "https://example.com/",
    )
    assert result["media_source"] is None
    assert result["media_type"] is None
