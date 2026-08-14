from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .refs import EntityRef


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@dataclass(slots=True)
class WorkerState:
    application_ref: EntityRef
    worker_id: int
    private_key: Ed25519PrivateKey
    name: str

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

    @classmethod
    def load(cls, directory: str | os.PathLike[str]) -> "WorkerState":
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
    ) -> "WorkerState":
        """One-time enrollment. Only the public key is sent to Kaede."""
        ref = EntityRef.parse(application_ref)
        key = Ed25519PrivateKey.generate()
        public_key = _b64(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        async with httpx.AsyncClient(
            base_url=application_home.rstrip("/"), timeout=15
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
