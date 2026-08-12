from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx

from app.core.settings import Settings

S3_SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class StorageError(RuntimeError):
    """An object store operation failed without exposing credentials or response bodies."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _retryable_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


@dataclass(frozen=True)
class ObjectMetadata:
    size: int
    content_type: str
    etag: str | None


@dataclass
class ObjectStream:
    """One bounded object-store response closed when iteration is cancelled."""

    client: httpx.AsyncClient
    response: httpx.Response
    max_bytes: int
    size: int | None

    async def chunks(self) -> AsyncIterator[bytes]:
        received = 0
        try:
            async for chunk in self.response.aiter_raw():
                received += len(chunk)
                if received > self.max_bytes:
                    raise StorageError("object exceeds the configured size limit")
                yield chunk
            if self.size is not None and received != self.size:
                raise StorageError("object store returned an incomplete object")
        finally:
            await self.response.aclose()
            await self.client.aclose()


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    path: str


def _target(endpoint: str, bucket: str, key: str, addressing_style: str) -> _Target:
    parsed = urlsplit(endpoint)
    if parsed.hostname is None:
        raise StorageError("object storage endpoint has no host")
    if addressing_style == "virtual":
        host = f"{bucket}.{parsed.hostname.lower()}"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = f"/{quote(key, safe='/-_.~')}"
    else:
        host = parsed.netloc.lower()
        path = f"/{quote(bucket, safe='-_.~')}/{quote(key, safe='/-_.~')}"
    return _Target(url=f"{parsed.scheme}://{host}{path}", host=host, path=path)


def _query(values: dict[str, str]) -> str:
    return "&".join(
        f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
        for key, value in sorted(values.items())
    )


def _signing_key(secret: str, date: str, region: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret}".encode(), date.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, S3_SERVICE.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


class S3Storage:
    """Small async SigV4 client for Garage and external S3-compatible stores.

    Keeping signing in-tree avoids sharing privileged object-store credentials with
    browsers. Browsers receive narrowly scoped, short-lived presigned URLs only.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.media_s3_access_key is None or settings.media_s3_secret_key is None:
            raise StorageError("media object storage credentials are not configured")
        self.endpoint = settings.media_s3_endpoint
        if settings.media_public_base_url is None:
            raise StorageError("media public object storage origin is not configured")
        self.public_endpoint = settings.media_public_base_url
        self.access_key = settings.media_s3_access_key.get_secret_value()
        self.secret_key = settings.media_s3_secret_key.get_secret_value()
        self.session_token = (
            settings.media_s3_session_token.get_secret_value()
            if settings.media_s3_session_token is not None
            else None
        )
        self.region = settings.media_s3_region
        self.addressing_style = settings.media_s3_addressing_style
        self.storage_backend = settings.media_storage_backend

    def presign(
        self,
        method: Literal["GET", "PUT"],
        bucket: str,
        key: str,
        *,
        expires: int,
        now: datetime | None = None,
        download_name: str | None = None,
        content_length: int | None = None,
        content_type: str | None = None,
    ) -> str:
        if not 1 <= expires <= 604_800:
            raise ValueError("presigned URL expiry must be between 1 second and 7 days")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp = current.strftime("%Y%m%dT%H%M%SZ")
        date = current.strftime("%Y%m%d")
        scope = f"{date}/{self.region}/{S3_SERVICE}/aws4_request"
        target = _target(self.public_endpoint, bucket, key, self.addressing_style)
        signed_header_values = {"host": target.host}
        if content_length is not None:
            if method != "PUT" or content_length < 0:
                raise ValueError("content length can only constrain a PUT")
            signed_header_values["content-length"] = str(content_length)
        if content_type is not None:
            if method != "PUT" or not content_type or "\n" in content_type or "\r" in content_type:
                raise ValueError("content type can only constrain a PUT")
            signed_header_values["content-type"] = content_type
        signed_headers = ";".join(sorted(signed_header_values))
        values = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": timestamp,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": signed_headers,
        }
        if self.session_token is not None:
            values["X-Amz-Security-Token"] = self.session_token
        if download_name is not None:
            safe_name = download_name.replace('"', "").replace("\r", "").replace("\n", "")
            values["response-content-disposition"] = f'attachment; filename="{safe_name}"'
        canonical_query = _query(values)
        canonical_headers = "".join(
            f"{name}:{signed_header_values[name]}\n" for name in sorted(signed_header_values)
        )
        canonical_request = "\n".join(
            [
                method,
                target.path,
                canonical_query,
                canonical_headers,
                signed_headers,
                "UNSIGNED-PAYLOAD",
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        values["X-Amz-Signature"] = hmac.new(
            _signing_key(self.secret_key, date, self.region),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{target.url}?{_query(values)}"

    def _headers_for_hash(self, method: str, target: _Target, payload_hash: str) -> dict[str, str]:
        current = datetime.now(UTC)
        timestamp = current.strftime("%Y%m%dT%H%M%SZ")
        date = current.strftime("%Y%m%d")
        scope = f"{date}/{self.region}/{S3_SERVICE}/aws4_request"
        signed_header_values = {
            "host": target.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": timestamp,
        }
        if self.session_token is not None:
            signed_header_values["x-amz-security-token"] = self.session_token
        signed_headers = ";".join(sorted(signed_header_values))
        canonical_headers = "".join(
            f"{name}:{signed_header_values[name]}\n" for name in sorted(signed_header_values)
        )
        canonical_request = "\n".join(
            [method, target.path, "", canonical_headers, signed_headers, payload_hash]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_key, date, self.region),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope},"
                f"SignedHeaders={signed_headers},Signature={signature}"
            ),
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": timestamp,
        }
        if self.session_token is not None:
            headers["X-Amz-Security-Token"] = self.session_token
        return headers

    def _headers(self, method: str, target: _Target, body: bytes) -> dict[str, str]:
        return self._headers_for_hash(method, target, hashlib.sha256(body).hexdigest())

    async def head(self, bucket: str, key: str) -> ObjectMetadata:
        target = _target(self.endpoint, bucket, key, self.addressing_style)
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.head(target.url, headers=self._headers("HEAD", target, b""))
        except httpx.HTTPError as exc:
            raise StorageError("object HEAD request failed") from exc
        if response.status_code == 404:
            raise StorageError("object does not exist")
        if response.status_code != 200:
            raise StorageError(f"object HEAD failed with status {response.status_code}")
        try:
            size = int(response.headers["Content-Length"])
        except (KeyError, ValueError) as exc:
            raise StorageError("object store returned invalid metadata") from exc
        if size < 0:
            raise StorageError("object store returned invalid metadata")
        return ObjectMetadata(
            size=size,
            content_type=response.headers.get("Content-Type", "application/octet-stream"),
            etag=response.headers.get("ETag"),
        )

    async def ensure_bucket(self, bucket: str, *, create_if_missing: bool = True) -> None:
        """Verify a bucket and optionally create it without weakening access errors."""

        target = _target(self.endpoint, bucket, "", self.addressing_style)
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.head(target.url, headers=self._headers("HEAD", target, b""))
                if response.status_code == 200:
                    return
                if response.status_code != 404:
                    raise StorageError(
                        f"bucket HEAD failed with status {response.status_code}",
                        retryable=_retryable_status(response.status_code),
                    )
                if not create_if_missing:
                    raise StorageError(f"required bucket {bucket!r} does not exist")
                body = b""
                if self.storage_backend == "s3" and self.region != "us-east-1":
                    body = (
                        "<CreateBucketConfiguration "
                        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        f"<LocationConstraint>{self.region}</LocationConstraint>"
                        "</CreateBucketConfiguration>"
                    ).encode()
                headers = self._headers("PUT", target, body)
                if body:
                    headers["Content-Type"] = "application/xml"
                response = await client.put(target.url, headers=headers, content=body)
                # A concurrent initializer can legitimately win after our HEAD.
                if response.status_code == 409:
                    response = await client.head(
                        target.url, headers=self._headers("HEAD", target, b"")
                    )
                    if response.status_code != 200:
                        raise StorageError(
                            "bucket creation conflicted but the bucket is not accessible",
                            retryable=_retryable_status(response.status_code),
                        )
                    return
        except httpx.HTTPError as exc:
            raise StorageError("bucket initialization request failed", retryable=True) from exc
        if response.status_code not in {200, 201, 204}:
            raise StorageError(
                f"bucket creation failed with status {response.status_code}",
                retryable=_retryable_status(response.status_code),
            )

    async def get(self, bucket: str, key: str, *, max_bytes: int) -> bytes:
        target = _target(self.endpoint, bucket, key, self.addressing_style)
        try:
            async with (
                httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client,
                client.stream(
                    "GET", target.url, headers=self._headers("GET", target, b"")
                ) as response,
            ):
                if response.status_code != 200:
                    raise StorageError(f"object GET failed with status {response.status_code}")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise StorageError("object store returned invalid metadata") from exc
                    if declared_size < 0:
                        raise StorageError("object store returned invalid metadata")
                    if declared_size > max_bytes:
                        raise StorageError("object exceeds the configured size limit")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_raw():
                    received += len(chunk)
                    if received > max_bytes:
                        raise StorageError("object exceeds the configured size limit")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise StorageError("object GET request failed") from exc
        return b"".join(chunks)

    async def open_get(self, bucket: str, key: str, *, max_bytes: int) -> ObjectStream:
        """Open a bounded streaming GET without retaining the object in memory."""

        target = _target(self.endpoint, bucket, key, self.addressing_style)
        client = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        response: httpx.Response | None = None
        try:
            request = client.build_request(
                "GET",
                target.url,
                headers={
                    **self._headers("GET", target, b""),
                    "Accept-Encoding": "identity",
                },
            )
            response = await client.send(request, stream=True)
            if response.status_code != 200:
                raise StorageError(f"object GET failed with status {response.status_code}")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise StorageError("object store returned an encoded object")
            declared = response.headers.get("Content-Length")
            size: int | None = None
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError as exc:
                    raise StorageError("object store returned invalid metadata") from exc
                if size < 0:
                    raise StorageError("object store returned invalid metadata")
                if size > max_bytes:
                    raise StorageError("object exceeds the configured size limit")
            return ObjectStream(client, response, max_bytes, size)
        except BaseException:
            with suppress(Exception):
                if response is not None:
                    await response.aclose()
            await client.aclose()
            raise

    async def put(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        target = _target(self.endpoint, bucket, key, self.addressing_style)
        headers = self._headers("PUT", target, body)
        headers["Content-Type"] = content_type
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.put(target.url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            raise StorageError("object PUT request failed") from exc
        if response.status_code not in {200, 201, 204}:
            raise StorageError(f"object PUT failed with status {response.status_code}")

    async def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        size: int,
        sha256: str,
        content_type: str,
    ) -> None:
        """Upload a previously hashed spool file without loading it into memory."""

        if size < 0 or len(sha256) != 64:
            raise StorageError("invalid file upload metadata")
        target = _target(self.endpoint, bucket, key, self.addressing_style)
        headers = self._headers_for_hash("PUT", target, sha256)
        headers.update({"Content-Type": content_type, "Content-Length": str(size)})

        async def body() -> AsyncIterator[bytes]:
            import anyio

            async with await anyio.open_file(path, "rb") as source:
                while chunk := await source.read(64 * 1024):
                    yield chunk

        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.put(target.url, headers=headers, content=body())
        except httpx.HTTPError as exc:
            raise StorageError("object PUT request failed") from exc
        if response.status_code not in {200, 201, 204}:
            raise StorageError(f"object PUT failed with status {response.status_code}")

    async def delete(self, bucket: str, key: str) -> None:
        target = _target(self.endpoint, bucket, key, self.addressing_style)
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.delete(
                    target.url, headers=self._headers("DELETE", target, b"")
                )
        except httpx.HTTPError as exc:
            raise StorageError("object DELETE request failed") from exc
        if response.status_code not in {200, 204, 404}:
            raise StorageError(f"object DELETE failed with status {response.status_code}")
