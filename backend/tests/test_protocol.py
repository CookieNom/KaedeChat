import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.federation import (
    SigningInput,
    canonical_request_target,
    content_sha256,
    sign_request,
    verify_request,
)
from app.core.permissions import ALL_PERMISSIONS, Permission


def test_canonical_query_is_sorted_and_in_signature_scope() -> None:
    assert canonical_request_target("/_kaede/v1/events", "z=2&a=hello%20world") == (
        "/_kaede/v1/events?a=hello+world&z=2"
    )


def test_ed25519_signing_round_trip_and_tamper_rejection() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    request = SigningInput("post", "/_kaede/v1/inbox", "alpha.test", "beta.test", 42, "abc")
    signature = sign_request(request, private)
    assert verify_request(request, signature, private.public_key())
    changed = SigningInput("post", "/_kaede/v1/inbox?x=1", "alpha.test", "beta.test", 42, "abc")
    assert not verify_request(changed, signature, private.public_key())


def test_kaede_fed_v1_fixed_request_signing_vector() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    body = b'{"events":[]}'
    request = SigningInput(
        method="POST",
        request_target=canonical_request_target("/_kaede/v1/inbox", "z=2&a=hello%20world"),
        origin="alpha.example",
        destination="beta.example",
        timestamp=1_783_886_400,
        content_hash=content_sha256(body),
    )

    assert request.content_hash == (
        "24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e"
    )
    assert request.canonical_bytes() == (
        b'{"content_sha256":"24de1c4a19c43ad41b013f13dcd858c17b0daa7f33a53f19913e5b11366d1c2e",'
        b'"destination":"beta.example","method":"POST","origin":"alpha.example",'
        b'"request_target":"/_kaede/v1/inbox?a=hello+world&z=2","ts":1783886400}'
    )
    assert base64.b64encode(private.public_key().public_bytes_raw()).decode() == (
        "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
    )
    assert base64.b64encode(sign_request(request, private)).decode() == (
        "Gj8RsqP7pnJVuSpHhbXZLERH24yA3RyZzsoDHoH9AWdxv6z60PbS4ztNg4BzPb6n4H5oVB/41ZFjaDJKevzPDQ=="
    )


def test_dm_pair_is_order_independent_and_authority_is_lower_domain() -> None:
    first = dm_pair_key("Alice@beta.test", "bob@alpha.test")
    assert first == dm_pair_key("bob@alpha.test", "alice@beta.test")
    assert dm_authority_domain("Alice@beta.test", "bob@alpha.test") == "alpha.test"


def test_permissions_fit_decimal_64_bit_wire_format() -> None:
    assert ALL_PERMISSIONS < 1 << 64
    assert Permission.CONNECT & Permission.SPEAK == 0
