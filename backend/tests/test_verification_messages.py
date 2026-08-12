from scripts.verification import VerificationFailure, failure_message, require


def test_require_raises_a_verification_failure() -> None:
    try:
        require(False, "expected HTTP 200; received HTTP 503")
    except VerificationFailure as error:
        assert str(error) == "expected HTTP 200; received HTTP 503"
    else:  # pragma: no cover - assertion documents the failure type
        raise AssertionError("require() accepted a false condition")


def test_failure_message_includes_reason_and_recovery_command() -> None:
    message = failure_message(
        "chat",
        VerificationFailure("expected 3 members; received 0"),
        "make chat-check",
    )

    assert "chat verification failed: expected 3 members; received 0" in message
    assert "correct the reported invariant" in message
    assert "`make chat-check`" in message


def test_verification_failures_redact_credentials_from_response_bodies() -> None:
    error = VerificationFailure(
        "login expected HTTP 200; received HTTP 201: "
        '{"access_token":"kc1_at_do-not-display",'
        '"refresh_token":"kc1_rt_also-secret",'
        '"authorization":"Bearer eyJheader.payload.signature"}; '
        "remote=https://operator:github-token@example.test/repository"
    )

    rendered = str(error)
    assert "expected HTTP 200; received HTTP 201" in rendered
    assert "do-not-display" not in rendered
    assert "also-secret" not in rendered
    assert "github-token" not in rendered
    assert "[redacted]" in rendered


def test_verification_failures_bound_untrusted_output() -> None:
    rendered = str(VerificationFailure("response body: " + "x" * 10_000))

    assert len(rendered) <= 2_030
    assert rendered.endswith("[output truncated]")
