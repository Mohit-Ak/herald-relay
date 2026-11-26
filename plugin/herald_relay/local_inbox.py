"""Authenticated PDF handoff to the selected Hermes profile.

The caller sends small, retryable pieces. The Bridge picks the only possible
destination and reveals it only when the complete original is on disk.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from pathlib import Path

from herald_relay import local_screen

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/inbox/?$")
_TRANSFER_ID = re.compile(r"[0-9a-f]{32}")
MAX_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
_QUOTA_BYTES = 64 * 1024 * 1024
_QUOTA_FILES = 512
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PARTIAL_SECONDS = 60 * 60
_LOCK = threading.Lock()


def matches(path: str) -> bool:
    return _ROUTE.fullmatch((path or "").split("?", 1)[0]) is not None


def _error(status: int, message: str) -> tuple[int, dict]:
    return status, {"message": message}


def _piece(body) -> tuple[str, int, int, bytes]:
    if isinstance(body, str):
        if len(body) > 4 * ((CHUNK_BYTES + 2) // 3) + 512:
            raise ValueError()
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError()
    transfer, offset, size, encoded = (body.get(k) for k in ("id", "offset", "size", "b64"))
    if not isinstance(transfer, str) or not _TRANSFER_ID.fullmatch(transfer):
        raise ValueError()
    if (type(offset) is not int or type(size) is not int
            or not 0 <= offset < size <= MAX_BYTES or offset % CHUNK_BYTES):
        raise ValueError()
    length = min(CHUNK_BYTES, size - offset)
    if not isinstance(encoded, str) or len(encoded) != 4 * ((length + 2) // 3):
        raise ValueError()
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) != length or (offset == 0 and not raw.startswith(b"%PDF")):
        raise ValueError()
    return transfer, offset, size, raw


def _prune(root: Path) -> tuple[int, int]:
    now = time.time()
    used = count = 0
    for folder in root.iterdir():
        if not _TRANSFER_ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        for name, age in (("incoming.part", _PARTIAL_SECONDS), ("original.pdf", _RETENTION_SECONDS)):
            file = folder / name
            try:
                info = file.lstat()
            except FileNotFoundError:
                continue
            if file.is_symlink() or not file.is_file():
                continue
            if now - info.st_mtime > age:
                file.unlink()
            else:
                used += info.st_size
        if any(folder.iterdir()):
            count += 1
        else:
            folder.rmdir()
    return used, count


def _write(home: Path, transfer: str, offset: int, size: int, raw: bytes) -> tuple[int, dict]:
    with _LOCK:
        root = home.resolve() / "herald-inbox"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise OSError("Unexpected attachment directory")
        used, count = _prune(root)
        folder = root / transfer
        if count >= _QUOTA_FILES and not folder.exists():
            return _error(507, "The agent's attachment inbox is full.")
        folder.mkdir(mode=0o700, exist_ok=True)
        part = folder / "incoming.part"
        full = folder / "original.pdf"
        if folder.is_symlink() or part.is_symlink() or full.is_symlink():
            raise OSError("Unexpected attachment path")
        complete = full.exists()
        file = full if complete else part
        if file.exists() and not file.is_file():
            raise OSError("Unexpected attachment file")
        have = file.stat().st_size if file.exists() else 0
        end = offset + len(raw)
        if have > size or offset > have or (complete and have != size):
            return _error(409, "The upload pieces don't match. Try sending the file again.")
        if offset < have:
            with file.open("rb") as f:
                f.seek(offset)
                if f.read(len(raw)) != raw:
                    return _error(409, "The upload pieces don't match. Try sending the file again.")
        elif used + len(raw) > _QUOTA_BYTES:
            return _error(507, "The agent's attachment inbox is full.")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(part, flags, 0o600), "ab") as f:
                f.write(raw)
        if end == size:
            if not complete:
                part.replace(full)
            return 200, {"offset": end, "path": str(full)}
        return 200, {"offset": end}


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path) -> tuple[int, dict]:
    match = _ROUTE.fullmatch((path or "").split("?", 1)[0])
    if match is None:
        return _error(404, "Not found.")
    if method.upper() != "PUT":
        return _error(405, "Method not allowed.")
    profile = match["profile"]
    prefix = f"/p/{profile}" if profile else ""
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _error(401, "Send this agent's API key with the request.")
    home = local_screen._home(hermes_home, profile)
    if home is None:
        return _error(404, "We couldn't find that agent.")
    try:
        piece = _piece(body)
    except ValueError:
        return _error(400, "Invalid PDF upload piece.")
    try:
        return await asyncio.to_thread(_write, home, *piece)
    except OSError:
        return _error(502, "Could not save the PDF on the agent's computer.")
