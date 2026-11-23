"""Files the agent made, fetched by the Herald app (herald#399, herald#495).

The phone reaches the tunnel client over the ordinary keyed proxy path, on an
address local Hermes never sees:

    GET [/p/<profile>]/_herald/file?path=<abs>&info=1
        -> {name, size, mime, kind, mtime, max_bytes, chunk_bytes}
    GET [/p/<profile>]/_herald/file?path=<abs>&offset=<n>&length=<n>[&mtime=<m>]
        -> {name, size, mime, mtime, offset, b64, eof}     (one piece of the file)
    GET [/p/<profile>]/_herald/file?path=<abs>&w=<px>
        -> {mime, b64, width, height}                      (a picture, shrunk for the screen)

The relay transports carry JSON frames, so bytes travel as base64, a piece at
a time (``CHUNK_MAX`` each): every frame stays small on both the edge socket
and the SSE fallback, and a big file never sits whole in anyone's memory.

Who may ask: whoever holds THAT profile's API key, checked by presenting the
caller's own Authorization header to that profile's local Hermes (the same
check as the screen routes). The machine's key is never substituted.

What may be read (owner decision on herald#495: the home folder minus hidden
folders and credentials):

* the path is resolved first (symlinks followed), so a link is judged by its
  target; it must be a regular file;
* it must be under the user's home folder with no hidden (dot) folder or
  file on the way, or in one of Hermes' own media folders (generated images,
  screenshots, documents), or a file this user owns in the temp folder;
  operators can replace the home folder with ``HERALD_FILE_ROOTS``
  (os.pathsep-separated);
* never inside a Hermes home outside those media folders (sessions, keys,
  config), never a credential by name, and never a file that starts like a
  private key, wherever it is.

Refusals never echo the path, and nothing here logs one.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from herald_relay import image_preview, local_screen

logger = logging.getLogger("herald.files")

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/file/?$")

# Largest file the phone may download. Above it the app says how big the file
# is and where it is, instead of trying.
MAX_FILE_BYTES = int(float(os.getenv("HERALD_FILE_MAX_MB", "50")) * 1024 * 1024)
CHUNK_DEFAULT = 1024 * 1024
# 2 MiB -> ~2.7 MB of base64: well inside the relay's 10 MB request cap and
# the edge's message size, with room to spare.
CHUNK_MAX = 2 * 1024 * 1024
HELPER_TIMEOUT_S = 40.0
_HELPER = Path(image_preview.__file__)
_IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif",
                "WEBP": "image/webp", "AVIF": "image/avif"}

# Hermes' own media folders (gateway/platforms/base.py MEDIA_DELIVERY_SAFE_ROOTS):
# where its image, screenshot and document tools save what they make.
_HERMES_MEDIA = ("image_cache", "audio_cache", "video_cache", "document_cache",
                 "browser_screenshots", "cache/images", "cache/audio", "cache/videos",
                 "cache/documents", "cache/screenshots", "cache/generated")

# Credentials by name, refused anywhere (lower-case).
_DENY_NAMES = {
    "credentials", "credentials.json", "auth.json", "token.json", "tokens.json",
    "google_token.json", "login data", "cookies", "key4.db", "logins.json",
    "known_hosts", "authorized_keys",
}
_DENY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "client_secret",
                  "service_account", "service-account")
_DENY_SUFFIXES = (".pem", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".gpg",
                  ".asc", ".ppk", ".ovpn")
_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# One file read at a time per piece, a few at once overall: a key holder
# could otherwise keep this computer's disk busy.
_reads = asyncio.Semaphore(4)


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _err(status: int, code: str, message: str, **extra) -> tuple[int, dict]:
    return status, {"error": code, "message": message, **extra}


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(code)
        self.answer = _err(status, code, message, **extra)


def _real(p: Path | str) -> Path | None:
    try:
        return Path(os.path.realpath(os.path.expanduser(str(p))))
    except (OSError, ValueError):
        return None


def _within(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _hermes_homes(hermes_home: Path) -> list[Path]:
    homes = [hermes_home]
    try:
        homes += [p for p in (hermes_home / "profiles").iterdir() if p.is_dir()]
    except OSError:
        pass
    return [r for r in (_real(h) for h in homes) if r is not None]


def _home_roots() -> list[Path]:
    raw = os.getenv("HERALD_FILE_ROOTS", "").strip()
    roots = [x for x in raw.split(os.pathsep) if x.strip()] if raw else [str(Path.home())]
    out = []
    for r in roots:
        rp = _real(r.strip())
        if rp is not None and rp.is_absolute() and rp != Path("/"):
            out.append(rp)
    return out


def _temp_roots() -> list[Path]:
    out = []
    for t in {"/tmp", tempfile.gettempdir()}:
        rp = _real(t)
        if rp is not None and rp not in out:
            out.append(rp)
    return out


def _denied_name(name: str) -> bool:
    n = name.lower()
    return (n in _DENY_NAMES or n.startswith(".env") or n.startswith(_DENY_PREFIXES)
            or n.endswith(_DENY_SUFFIXES))


def check_path(raw, hermes_home: Path) -> tuple[Path, os.stat_result]:
    """The real path and its stat if the app may read it; raises Refused."""
    if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw or len(raw) > 4096:
        raise Refused(400, "bad_path", "That isn't a file path on the computer.")
    real = _real(raw)
    if real is None:
        raise Refused(400, "bad_path", "That isn't a file path on the computer.")
    try:
        st = os.stat(real)
    except OSError:
        raise Refused(404, "not_found", "That file isn't on the computer any more.") from None
    if not stat.S_ISREG(st.st_mode):
        raise Refused(404, "not_found", "That file isn't on the computer any more.")
    if _denied_name(real.name):
        raise Refused(403, "path_not_allowed", "Herald can't open that file.")

    homes = _hermes_homes(hermes_home)
    for h in homes:
        for sub in _HERMES_MEDIA:
            media = _real(h / sub)
            if media is not None and _within(real, media) is not None:
                return real, st
    # Anything else inside a Hermes home (keys, sessions, config) is never read,
    # even when that home isn't a hidden folder (a container's /opt/data).
    if any(_within(real, h) is not None for h in homes):
        raise Refused(403, "path_not_allowed", "Herald can't open that file.")

    def visible(rel: Path) -> bool:
        return not any(part.startswith(".") for part in rel.parts)

    for root in _home_roots():
        rel = _within(real, root)
        if rel is not None and visible(rel):
            return real, st
    for root in _temp_roots():
        rel = _within(real, root)
        if rel is not None and visible(rel) and st.st_uid == os.getuid():
            return real, st
    raise Refused(403, "path_not_allowed", "Herald can't open that file.")


def _no_private_key(real: Path) -> None:
    try:
        with open(real, "rb") as f:
            head = f.read(8192)
    except OSError:
        raise Refused(404, "not_found", "That file isn't on the computer any more.") from None
    if _PRIVATE_KEY.search(head):
        raise Refused(403, "path_not_allowed", "Herald can't open that file.")


def _mime_of(real: Path, head: bytes) -> tuple[str, str]:
    """(mime, kind): kind is "image" only when the bytes say so."""
    kind = image_preview.sniff(head)
    if kind:
        return _IMAGE_MIMES[kind], "image"
    mime = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
    return mime, "file"


def _mtime(st: os.stat_result) -> str:
    return str(st.st_mtime_ns)


def _int(q: dict, key: str, default=None):
    v = (q.get(key) or [None])[0]
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise Refused(400, "bad_request", "That request isn't right.") from None


def _read(real: Path, offset: int, length: int) -> bytes:
    with open(real, "rb") as f:
        f.seek(offset)
        return f.read(length)


async def _preview(real: Path, st: os.stat_result, width: int, hermes_python) -> tuple[int, dict]:
    if st.st_size > image_preview.MAX_SOURCE_BYTES:
        return _err(413, "too_large", "That picture is too big to show.")
    async with _reads:
        data = await asyncio.to_thread(_read, real, 0, image_preview.MAX_SOURCE_BYTES + 1)
    kind = image_preview.sniff(data[:64])
    if kind is None:
        return _err(415, "not_an_image", "That file isn't a picture.")
    try:
        return 200, await asyncio.to_thread(image_preview.answer, data, width)
    except image_preview.PreviewError as e:
        return _err(e.status, e.code, "Herald can't show that picture.")
    except ImportError:
        pass  # this tunnel's Python has no Pillow: ask Hermes' interpreter
    ans = await _preview_with_hermes(real, width, hermes_python)
    if ans is not None:
        if "error" in ans:
            if ans.get("error") != "no_pillow":
                return _err(int(ans.get("status") or 415), str(ans["error"]),
                            "Herald can't show that picture.")
        else:
            return 200, ans
    # Last resort: the picture as it is, when it is small and a type every
    # phone draws. (It keeps its own metadata; it goes only to its owner.)
    if len(data) <= image_preview.MAX_OUT_BYTES and kind in ("PNG", "JPEG", "GIF", "WEBP"):
        return 200, {"mime": _IMAGE_MIMES[kind], "b64": base64.b64encode(data).decode("ascii"),
                     "width": None, "height": None}
    return _err(415, "not_an_image", "Herald can't show that picture.")


async def _preview_with_hermes(real: Path, width: int, hermes_python) -> dict | None:
    argv = local_screen.argv_of(hermes_python) if hermes_python else []
    if not argv:
        return None
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, str(_HELPER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps({"path": str(real), "w": width}).encode()), HELPER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                ans = json.loads(line)
            except ValueError:
                continue
            return ans if isinstance(ans, dict) else None
    return None


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path, hermes_python=None) -> tuple[int, dict]:
    bare, _, query = (path or "").partition("?")
    m = _ROUTE.match(bare)
    if m is None:
        return _err(404, "not_found", "Not found.")
    if (method or "GET").upper() != "GET":
        return _err(405, "method_not_allowed", "Method not allowed.")
    profile = m.group("profile")
    prefix = f"/p/{profile}" if profile else ""
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    if local_screen._home(Path(hermes_home), profile) is None:
        return _err(404, "not_found", "We couldn't find that agent.")
    q = parse_qs(query, keep_blank_values=True)
    try:
        real, st = check_path((q.get("path") or [None])[0], Path(hermes_home))
        await asyncio.to_thread(_no_private_key, real)
        width = _int(q, "w")
        if width is not None:
            status, ans = await _preview(real, st, width, hermes_python)
            logger.info("file preview -> %s (%s bytes)", status, len(ans.get("b64") or ""))
            return status, ans
        async with _reads:
            head = await asyncio.to_thread(_read, real, 0, 64)
        mime, kind = _mime_of(real, head)
        base = {"name": real.name, "size": st.st_size, "mime": mime, "mtime": _mtime(st)}
        too_big = st.st_size > MAX_FILE_BYTES
        if (q.get("info") or [""])[0] not in ("", "0"):
            logger.info("file info -> 200")
            return 200, {**base, "kind": kind, "max_bytes": MAX_FILE_BYTES,
                         "chunk_bytes": CHUNK_DEFAULT}
        if too_big:
            return _err(413, "too_large", "That file is too big to send to the phone.",
                        size=st.st_size, max_bytes=MAX_FILE_BYTES)
        offset = _int(q, "offset", 0)
        length = _int(q, "length", CHUNK_DEFAULT)
        if offset < 0 or offset > st.st_size or length <= 0:
            return _err(400, "bad_request", "That request isn't right.")
        length = min(length, CHUNK_MAX)
        want = (q.get("mtime") or [""])[0]
        if want and want != base["mtime"]:
            return _err(409, "changed", "That file changed on the computer. Try again.")
        async with _reads:
            data = await asyncio.to_thread(_read, real, offset, length)
        logger.info("file piece -> 200 (%s bytes)", len(data))
        return 200, {**base, "offset": offset, "b64": base64.b64encode(data).decode("ascii"),
                     "eof": offset + len(data) >= st.st_size}
    except Refused as r:
        logger.info("file -> %s %s", r.answer[0], r.answer[1].get("error"))
        return r.answer
