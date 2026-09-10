import httpx
import pytest

from kaede_bot.client import Client
from kaede_bot.errors import ApiError, Forbidden, NotFound, RateLimited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,kind",
    [(401, ApiError), (403, Forbidden), (404, NotFound), (429, RateLimited)],
)
@pytest.mark.parametrize("legacy", [False, True])
async def test_current_and_legacy_error_envelopes(status, kind, legacy):
    detail = {
        "code": "BOT_TOKEN_INVALID",
        "message": "The bot token is invalid.",
        "trace_id": "trace-123",
        "retry_after_ms": 2500,
    }
    body = {"detail": detail} if legacy else detail
    response = httpx.Response(status, json=body, headers={"Retry-After": "2.5"})
    with pytest.raises(kind) as caught:
        await Client._raise(None, response)
    error = caught.value
    assert error.status == status
    assert error.code == detail["code"]
    assert str(error) == detail["message"]
    assert error.detail == detail
    if status == 429:
        assert error.retry_after == 2.5


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], None, {"detail": "Unauthorized"}])
async def test_unstructured_error_falls_back(body):
    with pytest.raises(ApiError) as caught:
        await Client._raise(None, httpx.Response(401, json=body))
    assert caught.value.code == "KAEDE_API_ERROR"


@pytest.mark.asyncio
async def test_non_json_error_and_success():
    with pytest.raises(ApiError) as caught:
        await Client._raise(None, httpx.Response(502, text="Bad gateway"))
    assert caught.value.code == "KAEDE_API_ERROR"
    await Client._raise(None, httpx.Response(204))


@pytest.mark.asyncio
async def test_current_envelope_takes_precedence_over_legacy_detail():
    with pytest.raises(ApiError) as caught:
        await Client._raise(
            None,
            httpx.Response(
                401,
                json={
                    "code": "BOT_TOKEN_INVALID",
                    "message": "Invalid token",
                    "detail": {"code": "OLD_ERROR"},
                },
            ),
        )
    assert caught.value.code == "BOT_TOKEN_INVALID"
