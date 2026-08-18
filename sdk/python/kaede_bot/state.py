from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .refs import EntityRef


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def canonical_target_origin(value: str) -> str:
    """Return a canonical HTTPS origin suitable for authenticated bot traffic."""

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("bot targets must be canonical HTTPS origins") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname.endswith(".")
    ):
        raise ValueError("bot targets must be canonical HTTPS origins")
    authority = parsed.hostname.lower()
    port_suffix = f":{port}" if port not in {None, 443} else ""
    return f"https://{authority}{port_suffix}"


def canonical_application_home(value: str, application_ref: EntityRef) -> str:
    """Bind a control-token destination to its authoritative application domain."""

    origin = canonical_target_origin(value)
    if urlsplit(origin).hostname != application_ref.domain:
        raise ValueError(
            "application_home must use the authoritative application_ref domain"
        )
    return origin


@dataclass(slots=True)
class WorkerState:
    application_ref: EntityRef
    worker_id: int
    private_key: Ed25519PrivateKey
    name: str
    directory: Path | None = None

    @property
    def public_key(self) -> str:
        return _b64(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )

    def save(self, directory: str | os.PathLike[str]) -> None:
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise PermissionError(
                f"{root} must not be accessible to group or other users"
            )
        path = root / "worker.json"
        temporary = root / ".worker.json.tmp"
        raw_key = self.private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        temporary.write_text(
            json.dumps(
                {
                    "application_ref": str(self.application_ref),
                    "worker_id": str(self.worker_id),
                    "name": self.name,
                    "private_key": _b64(raw_key),
                },
                separators=(",", ":"),
            )
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        self.directory = root

    def load_cursors(self) -> dict[str, dict[str, int]]:
        if self.directory is None:
            return {}
        path = self.directory / "gateway-cursors.json"
        if not path.exists():
            return {}
        if path.stat().st_mode & 0o077:
            raise PermissionError(
                f"{path} must not be accessible to group or other users"
            )
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("gateway cursor state must be an object")
        result: dict[str, dict[str, int]] = {}
        for target, cursors in payload.items():
            if not isinstance(target, str) or not isinstance(cursors, dict):
                raise ValueError("gateway cursor state is invalid")
            parsed = {
                str(topic): int(sequence)
                for topic, sequence in cursors.items()
                if isinstance(topic, str)
                and isinstance(sequence, int)
                and sequence >= 0
            }
            if len(parsed) != len(cursors):
                raise ValueError("gateway cursor state is invalid")
            result[target] = parsed
        return result

    def save_cursors(self, cursors: dict[str, dict[str, int]]) -> None:
        if self.directory is None:
            return
        path = self.directory / "gateway-cursors.json"
        temporary = self.directory / ".gateway-cursors.json.tmp"
        temporary.write_text(json.dumps(cursors, separators=(",", ":"), sort_keys=True))
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @classmethod
    def load(cls, directory: str | os.PathLike[str]) -> WorkerState:
        path = Path(directory) / "worker.json"
        if path.stat().st_mode & 0o077:
            raise PermissionError(
                f"{path} must not be accessible to group or other users"
            )
        payload = json.loads(path.read_text())
        encoded_key = payload["private_key"]
        if not isinstance(encoded_key, str):
            raise ValueError("worker private key must be URL-safe base64")
        key = base64.b64decode(
            encoded_key + "=" * (-len(encoded_key) % 4),
            altchars=b"-_",
            validate=True,
        )
        return cls(
            EntityRef.parse(payload["application_ref"]),
            int(payload["worker_id"]),
            Ed25519PrivateKey.from_private_bytes(key),
            payload["name"],
            path.parent,
        )

    @classmethod
    async def enroll(
        cls,
        *,
        application_home: str,
        application_ref: str,
        control_token: str,
        directory: str | os.PathLike[str],
        name: str = "production",
        scopes: list[str],
        intents: list[str],
        target_domains: list[str] | None = None,
    ) -> WorkerState:
        """One-time enrollment. Only the public key is sent to Kaede."""
        ref = EntityRef.parse(application_ref)
        origin = canonical_application_home(application_home, ref)
        key = Ed25519PrivateKey.generate()
        public_key = _b64(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        async with httpx.AsyncClient(
            base_url=origin,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            response = await http.post(
                f"/api/v1/bot-control/applications/{ref}/workers",
                headers={"Authorization": f"BotControl {control_token}"},
                json={
                    "name": name,
                    "public_key": public_key,
                    "scopes": scopes,
                    "intents": intents,
                    "target_domains": target_domains or [],
                    "session_limit": 1,
                },
            )
            response.raise_for_status()
            worker_id = int(response.json()["id"])
        state = cls(ref, worker_id, key, name)
        state.save(directory)
        return state
