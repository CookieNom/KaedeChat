from app.core.proxy import resolve_client_ip


def test_authenticated_proxy_address_is_used() -> None:
    assert (
        resolve_client_ip(
            supplied_secret="secret",
            configured_secret="secret",
            forwarded_for="2001:0db8::1",
            direct_host="127.0.0.1",
        )
        == "2001:db8::1"
    )


def test_unauthenticated_or_unsanitized_forwarding_is_rejected() -> None:
    arguments = {
        "configured_secret": "secret",
        "forwarded_for": "198.51.100.4",
        "direct_host": "127.0.0.1",
    }
    assert resolve_client_ip(supplied_secret="wrong", **arguments) == "127.0.0.1"
    assert (
        resolve_client_ip(
            supplied_secret="secret",
            configured_secret="secret",
            forwarded_for="198.51.100.4, 10.0.0.1",
            direct_host="127.0.0.1",
        )
        == "127.0.0.1"
    )


def test_invalid_direct_address_is_unknown() -> None:
    assert (
        resolve_client_ip(
            supplied_secret=None,
            configured_secret=None,
            forwarded_for=None,
            direct_host="not-an-address",
        )
        == "unknown"
    )
