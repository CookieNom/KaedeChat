from __future__ import annotations

import ctypes
import json
from pathlib import Path
from typing import Any

import pytest

import kaede_bot.e2ee as e2ee
from kaede_bot.e2ee import (
    E2EEProtocolError,
    E2EEUnavailableError,
    NativeOpenMLSProvider,
    bot_mls_credential,
)
from kaede_bot.refs import EntityRef


class NativeFunction:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self.callback(*args)


class FakeNativeLibrary:
    def __init__(self) -> None:
        self.allocations: list[ctypes.Array[ctypes.c_char]] = []
        self.closed: list[int] = []
        self.freed = 0
        self.kaede_e2ee_invoke = NativeFunction(self.invoke)
        self.kaede_e2ee_close = NativeFunction(self.close)
        self.kaede_e2ee_buffer_free = NativeFunction(self.free)

    def invoke(
        self,
        handle: int,
        method_pointer: object,
        method_length: int,
        input_pointer: object,
        input_length: int,
    ) -> e2ee._NativeBuffer:
        method = ctypes.string_at(method_pointer, method_length).decode()
        payload = json.loads(ctypes.string_at(input_pointer, input_length))
        if method == "generate":
            assert handle == 0
            assert payload["credential"]
            result: dict[str, object] = {"handle": "7"}
        elif method == "public_identity_key":
            assert handle == 7
            result = {"bytes": e2ee._b64(b"i" * 32)}
        elif method == "sign":
            result = {"bytes": e2ee._b64(b"s" * 64)}
        elif method == "export_epoch_secret":
            result = {"bytes": e2ee._b64(b"k" * int(payload["length"]))}
        elif method == "group_epoch":
            result = {"epoch": "7"}
        else:
            encoded = json.dumps({"ok": False, "error": "unsupported in test"}).encode()
            return self.buffer(encoded)
        return self.buffer(json.dumps({"ok": True, "result": result}).encode())

    def buffer(self, value: bytes) -> e2ee._NativeBuffer:
        allocation = ctypes.create_string_buffer(value)
        self.allocations.append(allocation)
        return e2ee._NativeBuffer(
            ctypes.cast(allocation, ctypes.POINTER(ctypes.c_uint8)),
            len(value),
        )

    def close(self, handle: int) -> None:
        self.closed.append(handle)

    def free(self, _buffer: e2ee._NativeBuffer) -> None:
        self.freed += 1


def test_native_provider_uses_bounded_ffi_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = FakeNativeLibrary()
    monkeypatch.setattr(e2ee, "_load_library", lambda _path=None: library)

    provider = NativeOpenMLSProvider.generate(
        bot_mls_credential(EntityRef(20, "apps.example"), 40, b"i" * 32)
    )
    assert provider.public_identity_key() == b"i" * 32
    assert provider.sign(b"challenge") == b"s" * 64
    assert (
        provider.export_epoch_secret(b"group", "kaede livekit v1", b"context", 32)
        == b"k" * 32
    )
    assert provider.group_epoch(b"group") == 7
    provider.close()
    provider.close()

    assert library.freed == 5
    assert library.closed == [7]


def test_native_provider_surfaces_openmls_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = FakeNativeLibrary()
    monkeypatch.setattr(e2ee, "_load_library", lambda _path=None: library)
    provider = NativeOpenMLSProvider.generate(b"credential")

    with pytest.raises(E2EEProtocolError, match="unsupported in test"):
        provider.generate_key_package()

    provider.close()


def test_loader_rejects_world_writable_library(tmp_path: Path) -> None:
    library = tmp_path / "libkaede_e2ee_ffi.so"
    library.write_bytes(b"not a real library")
    library.chmod(0o666)

    with pytest.raises(E2EEUnavailableError, match="world-writable"):
        e2ee._load_library(library)
