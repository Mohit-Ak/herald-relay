"""Files the agent made, fetched by the app (herald#399, herald#495).

Local Hermes is a tiny real HTTP server that knows one key per profile; the
home folder, the Hermes home and the temp folder are real directories under
tmp_path.
"""
import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import edge_tunnel, image_preview, is_local_door, local_files  # noqa: E402

KEYS = {"": "default-key", "/p/work": "work-key"}


@pytest.fixture
async def hermes():
    async def models(request):
        prefix = "/p/" + request.match_info["p"] if "p" in request.match_info else ""
        ok = request.headers.get("Authorization") == f"Bearer {KEYS.get(prefix)}"
        return web.json_response({"data": []}, status=200 if ok else 401)
    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_get("/p/{p}/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A home folder with a Hermes home inside it, and a temp folder."""
    home = tmp_path / "home" / "ana"
    hh = home / ".hermes"
    (hh / "profiles" / "work").mkdir(parents=True)
    (hh / "cache" / "images").mkdir(parents=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERALD_FILE_ROOTS", raising=False)
    monkeypatch.setattr(local_files, "_temp_roots", lambda: [tmp])
    return home, hh, tmp


async def call(hermes, hh, query: str, key="default-key", prefix="", py=None):
    hdrs = {"Authorization": f"Bearer {key}"} if key else {}
    return await local_files.handle("GET", f"{prefix}/_herald/file?{query}", hdrs, None,
                                    hermes_url=hermes, hermes_home=hh, hermes_python=py)


def q(path, **kw):
    from urllib.parse import urlencode
    return urlencode({"path": str(path), **kw})


def png(w=40, h=30, exif_gps=False) -> bytes:
    from PIL import Image
    im = Image.new("RGB", (w, h), (200, 30, 30))
    out = io.BytesIO()
    if exif_gps:
        ex = Image.Exif()
        ex[0x8825] = {2: (1.0, 2.0, 3.0)}  # GPSInfo
        ex[0x010F] = "PhoneMaker"
        im.save(out, "JPEG", exif=ex.tobytes())
    else:
        im.save(out, "PNG")
    return out.getvalue()


def test_route_and_door():
    assert local_files.matches("/_herald/file?path=/x")
    assert local_files.matches("/p/work/_herald/file")
    assert not local_files.matches("/_herald/files")
    assert not local_files.matches("/v1/_herald/file")
    assert not local_files.matches("/p/../_herald/file")
    assert is_local_door("/_herald/file?path=%2Fhome%2Fx.png&w=640")


async def test_key_required(hermes, box):
    home, hh, _ = box
    f = home / "report.csv"
    f.write_text("a,b\n1,2\n")
    assert (await call(hermes, hh, q(f, info=1), key=None))[0] == 401
    assert (await call(hermes, hh, q(f, info=1), key="wrong"))[0] == 401
    # The default key does not open another profile's door, nor the reverse.
    assert (await call(hermes, hh, q(f, info=1), key="default-key", prefix="/p/work"))[0] == 401
    assert (await call(hermes, hh, q(f, info=1), key="work-key"))[0] == 401
    s, b = await call(hermes, hh, q(f, info=1), key="work-key", prefix="/p/work")
    assert s == 200 and b["name"] == "report.csv"


async def test_info_and_pieces_rebuild_the_file(hermes, box):
    home, hh, _ = box
    data = os.urandom(local_files.CHUNK_DEFAULT * 2 + 12345)
    f = home / "Documents" / "deck.pptx"
    f.parent.mkdir()
    f.write_bytes(data)
    s, info = await call(hermes, hh, q(f, info=1))
    assert s == 200
    assert info["size"] == len(data) and info["kind"] == "file"
    assert info["mime"].startswith("application/vnd.openxmlformats")
    assert info["max_bytes"] == local_files.MAX_FILE_BYTES
    got, off = b"", 0
    while True:
        s, piece = await call(hermes, hh, q(f, offset=off, length=info["chunk_bytes"], mtime=info["mtime"]))
        assert s == 200
        chunk = base64.b64decode(piece["b64"])
        got += chunk
        off += len(chunk)
        if piece["eof"]:
            break
    assert got == data


async def test_piece_length_is_capped(hermes, box):
    home, hh, _ = box
    f = home / "big.bin"
    f.write_bytes(b"x" * (local_files.CHUNK_MAX + 10))
    s, b = await call(hermes, hh, q(f, offset=0, length=10 * local_files.CHUNK_MAX))
    assert s == 200 and len(base64.b64decode(b["b64"])) == local_files.CHUNK_MAX and not b["eof"]


async def test_changed_file_is_refused_mid_download(hermes, box):
    home, hh, _ = box
    f = home / "a.txt"
    f.write_text("one")
    s, info = await call(hermes, hh, q(f, info=1))
    os.utime(f, ns=(1, 1))
    s, b = await call(hermes, hh, q(f, offset=0, mtime=info["mtime"]))
    assert s == 409 and b["error"] == "changed"


async def test_too_large_says_how_big(hermes, box, monkeypatch):
    home, hh, _ = box
    monkeypatch.setattr(local_files, "MAX_FILE_BYTES", 100)
    f = home / "video.mp4"
    f.write_bytes(b"x" * 101)
    s, info = await call(hermes, hh, q(f, info=1))
    assert s == 200 and info["size"] == 101 and info["max_bytes"] == 100
    s, b = await call(hermes, hh, q(f, offset=0))
    assert s == 413 and b["error"] == "too_large" and b["size"] == 101 and b["max_bytes"] == 100


@pytest.mark.parametrize("rel", [
    ".ssh/id_ed25519", ".hermes/.env", ".hermes/auth.json", ".config/gcloud/creds.txt",
    "notes/.secret/plan.txt", ".bashrc", "keys/server.pem", "Documents/id_rsa.bak",
    "work/credentials.json", "proj/.env.local", ".hermes/profiles/work/sessions/s.json",
])
async def test_secrets_and_hidden_files_refused(hermes, box, rel):
    home, hh, _ = box
    f = home / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("secret")
    s, b = await call(hermes, hh, q(f, info=1))
    assert s == 403 and b["error"] == "path_not_allowed"
    assert str(f) not in json.dumps(b) and rel not in json.dumps(b)


async def test_private_key_content_refused_anywhere(hermes, box):
    home, hh, _ = box
    f = home / "notes.txt"
    f.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert (await call(hermes, hh, q(f, info=1)))[0] == 403


async def test_outside_home_traversal_and_symlinks(hermes, box, tmp_path):
    home, hh, tmp = box
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    assert (await call(hermes, hh, q(outside, info=1)))[0] == 403
    assert (await call(hermes, hh, q(f"{home}/../../elsewhere.txt", info=1)))[0] == 403
    (home / "link.txt").symlink_to(outside)
    assert (await call(hermes, hh, q(home / "link.txt", info=1)))[0] == 403
    (home / "passwd.png").symlink_to("/etc/passwd")
    assert (await call(hermes, hh, q(home / "passwd.png", info=1)))[0] == 403
    # A link INTO home is judged by its target, which is fine.
    (home / "real.csv").write_text("a")
    (tmp / "alias.csv").symlink_to(home / "real.csv")
    assert (await call(hermes, hh, q(tmp / "alias.csv", info=1)))[0] == 200
    assert (await call(hermes, hh, q("relative/path.txt", info=1)))[0] == 400
    assert (await call(hermes, hh, q(home / "missing.txt", info=1)))[0] == 404
    assert (await call(hermes, hh, q(home, info=1)))[0] == 404  # a folder


async def test_hermes_media_folder_and_temp_allowed(hermes, box):
    home, hh, tmp = box
    gen = hh / "cache" / "images" / "chart.png"
    gen.write_bytes(b"\x89PNG\r\n\x1a\n")
    s, b = await call(hermes, hh, q(gen, info=1))
    assert s == 200 and b["kind"] == "image" and b["mime"] == "image/png"
    t = tmp / "out.pdf"
    t.write_bytes(b"%PDF-1.4")
    s, b = await call(hermes, hh, q(t, info=1))
    assert s == 200 and b["mime"] == "application/pdf"


async def test_file_replaced_after_path_check_is_not_served(hermes, box, monkeypatch):
    home, hh, _ = box
    allowed = home / "attachment.txt"
    allowed.write_text("original")
    blocked = home / ".private"
    blocked.write_text("not an attachment")
    check = local_files.check_path

    def replace_after_check(path, hermes_home):
        result = check(path, hermes_home)
        allowed.unlink()
        allowed.symlink_to(blocked)
        return result

    monkeypatch.setattr(local_files, "check_path", replace_after_check)
    status, answer = await call(hermes, hh, q(allowed))
    assert status in (404, 409) and "b64" not in answer


async def test_operator_roots_replace_home(hermes, box, tmp_path, monkeypatch):
    home, hh, _ = box
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("x")
    (home / "b.txt").write_text("x")
    monkeypatch.setenv("HERALD_FILE_ROOTS", str(shared))
    assert (await call(hermes, hh, q(shared / "a.txt", info=1)))[0] == 200
    assert (await call(hermes, hh, q(home / "b.txt", info=1)))[0] == 403


async def test_image_preview_shrinks_and_strips_metadata(hermes, box):
    pytest.importorskip("PIL")
    from PIL import Image
    home, hh, _ = box
    f = home / "Pictures" / "photo.jpg"
    f.parent.mkdir()
    f.write_bytes(png(3000, 2000, exif_gps=True))
    s, b = await call(hermes, hh, q(f, w=640))
    assert s == 200 and b["mime"] == "image/webp" and b["width"] == 640
    im = Image.open(io.BytesIO(base64.b64decode(b["b64"])))
    assert im.format == "WEBP" and im.size == (640, 427)
    assert not im.getexif() and "exif" not in im.info and "icc_profile" not in im.info


async def test_preview_refuses_a_text_file_named_png(hermes, box):
    home, hh, _ = box
    f = home / "fake.png"
    f.write_text("not a picture")
    s, b = await call(hermes, hh, q(f, w=320))
    assert s == 415 and b["error"] == "not_an_image"
    s, b = await call(hermes, hh, q(f, info=1))
    assert s == 200 and b["kind"] == "file"  # by bytes, not by name


async def test_preview_falls_back_to_hermes_python(hermes, box, monkeypatch):
    """No Pillow in the tunnel's own Python: Hermes' interpreter shrinks it."""
    pytest.importorskip("PIL")
    home, hh, _ = box
    f = home / "chart.png"
    f.write_bytes(png(1200, 600))

    def no_pillow(*a, **k):
        raise ImportError("PIL")
    monkeypatch.setattr(image_preview, "answer", no_pillow)
    s, b = await call(hermes, hh, q(f, w=320), py=[sys.executable])
    assert s == 200 and b["mime"] == "image/webp" and b["width"] == 320
    # Never send an unprocessed preview that may still carry metadata.
    s, b = await call(hermes, hh, q(f, w=320), py=None)
    assert s == 503 and b["error"] == "preview_unavailable" and "b64" not in b


async def test_edge_sends_a_big_answer_in_pieces(hermes, box):
    """Through the edge socket a multi-megabyte answer arrives byte-exact, in
    frames no bigger than LOCAL_ANSWER_PIECE."""
    home, hh, _ = box
    data = os.urandom(local_files.CHUNK_MAX)
    f = home / "blob.zip"
    f.write_bytes(data)
    frames = []

    class WS:
        closed = False

        async def send_str(self, s):
            frames.append(json.loads(s))

    t = edge_tunnel.EdgeTunnel("wss://edge.invalid", "tok", hermes, "machine-key")

    async def on_screen(method, path, headers, body):
        assert headers.get("Authorization") == "Bearer default-key"
        return await local_files.handle(method, path, headers, body, hermes_url=hermes,
                                        hermes_home=hh, hermes_python=None)
    t.on_screen = on_screen
    await t._serve_one(WS(), {"id": "r1", "method": "GET",
                               "path": f"/_herald/file?{q(f, offset=0, length=local_files.CHUNK_MAX)}",
                               "headers": {"Authorization": "Bearer default-key"}})
    assert frames[0]["t"] == "res" and frames[0]["status"] == 200 and frames[-1]["t"] == "end"
    chunks = [x["data"] for x in frames if x["t"] == "chunk"]
    assert len(chunks) > 1 and all(len(c) <= edge_tunnel.LOCAL_ANSWER_PIECE for c in chunks)
    assert base64.b64decode(json.loads("".join(chunks))["b64"]) == data


async def test_post_not_allowed(hermes, box):
    home, hh, _ = box
    s, _ = await local_files.handle("POST", "/_herald/file", {"Authorization": "Bearer default-key"},
                                    {}, hermes_url=hermes, hermes_home=hh)
    assert s == 405
