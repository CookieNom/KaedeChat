"""Per-user Jellyfin accounts and pinned, authenticated HTTP requests."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
import httpx


def server_url(value):
    parts = urlsplit(value.strip())
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or any(ord(c) < 33 for c in value)):
        raise ValueError("Use your Jellyfin HTTPS address, without a username, query, or fragment.")
    return value.strip().rstrip("/")


def item_id(value):
    return UUID(value).hex


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = directory / "accounts.key"
        if not key_path.exists():
            if (directory / "accounts.sqlite3").exists():
                raise ValueError("Restore accounts.key before opening the existing account database")
            with key_path.open("xb") as file:
                os.chmod(key_path, 0o600)
                file.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.db = sqlite3.connect(directory / "accounts.sqlite3")
        os.chmod(directory / "accounts.sqlite3", 0o600)
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS accounts (owner TEXT PRIMARY KEY, connection BLOB NOT NULL)")
        self.db.commit()

    def get(self, owner):
        row = self.db.execute("SELECT connection FROM accounts WHERE owner = ?", (str(owner),)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def put(self, owner, connection):
        with self.db:
            self.db.execute("INSERT INTO accounts VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET connection=excluded.connection",
                            (str(owner), self.cipher.encrypt(json.dumps(connection).encode())))

    def delete(self, owner):
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE owner = ?", (str(owner),))


class Jellyfin:
    def __init__(self, server, owner, token=None, user_id=None):
        self.server = server_url(server)
        self.user_id = user_id
        self.connection = {"server": self.server, "token": token, "user_id": user_id}
        device = hashlib.sha256(str(owner).encode()).hexdigest()
        self.authorization = (f'MediaBrowser Client="Kaede Movie Night", Device="Kaede Bot", '
                              f'DeviceId="{device}", Version="1.0"')
        if token:
            if not isinstance(token, str) or not token.isalnum() or len(token) > 512:
                raise ValueError("Jellyfin returned an invalid access token")
            self.authorization += f', Token="{token}"'

    @asynccontextmanager
    async def stream(self, method, path, *, extra_headers=None, **kwargs):
        url = httpx.URL(self.server + path)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            url.host, url.port or 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise ValueError("Jellyfin must have a publicly reachable HTTPS address.")
        # Pin the validated address, retaining the original TLS name and Host.
        # Never follow redirects or inherit an environment proxy for user URLs.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
            async with client.stream(method, url.copy_with(host=str(ips[0])),
                                     headers={"Host": url.netloc.decode(), "Authorization": self.authorization, **(extra_headers or {})},
                                     extensions={"sni_hostname": url.host}, **kwargs) as response:
                response.raise_for_status()
                yield response

    async def request(self, method, path, **kwargs):
        async with self.stream(method, path, **kwargs) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ValueError("Jellyfin response is too large")
            return json.loads(data) if data else None

    async def browse(self, kind, *, query=None, series=None, page=0):
        params = {"userId": self.user_id, "limit": 25, "startIndex": page * 25,
                  "enableUserData": "true", "enableImages": "false"}
        path = "/Items"
        if kind in {"Movie", "Series"}:
            params.update(searchTerm=query, includeItemTypes=kind, recursive="true",
                          sortBy="SortName", isVirtualItem="false")
        elif kind == "Episode":
            path = f"/Shows/{item_id(series)}/Episodes"
            params["isMissing"] = "false"
        elif kind == "Resume":
            path = f"/Users/{item_id(self.user_id)}/Items/Resume"
            params["mediaTypes"] = "Video"
            params["includeItemTypes"] = "Movie,Episode"
        elif kind == "NextUp":
            path = "/Shows/NextUp"
            params["enableResumable"] = "true"
        else:
            raise ValueError("Unsupported library selection")
        return await self.request("GET", path, params=params)

    async def item(self, identifier):
        return await self.request("GET", f"/Users/{item_id(self.user_id)}/Items/{item_id(identifier)}")

    async def report(self, action, identifier, position, session, *, paused=False):
        paths = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                 "stop": "/Sessions/Playing/Stopped"}
        await self.request("POST", paths[action], json={
            "ItemId": item_id(identifier), "PositionTicks": position,
            "PlaySessionId": session, "PlayMethod": "DirectPlay", "CanSeek": False,
            "IsPaused": paused})


def resume_ticks(item):
    data = item.get("UserData") or {}
    position, duration = data.get("PlaybackPositionTicks", 0), item.get("RunTimeTicks")
    if (type(position) is not int or not 0 < position < 2**63
            or (type(duration) is int and position >= duration)):
        return 0
    return position


def time_label(ticks):
    seconds = ticks // 10_000_000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def media_label(item):
    name = str(item.get("Name") or "Untitled")
    if item.get("Type") == "Episode":
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        number = f"S{season:02}E{episode:02}" if type(season) is int and type(episode) is int else "Episode"
        name = f"{str(item.get('SeriesName') or 'TV')[:35]} · {number} · {name}"
    elif item.get("ProductionYear"):
        name += f" ({item['ProductionYear']})"
    return name[:100]
