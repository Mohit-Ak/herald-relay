"""A deploy must not strand the tunnel on the old instance.

Reproduces the 2026-10-17 production incident with real processes:

  OLD relay ─┐
             ├── front proxy (routes each NEW request to the current target,
  NEW relay ─┘    leaves in-flight streams where they are — like Cloud Run's
                  front end during a revision rollout)

The plugin client connects through the proxy to OLD. We then "deploy": the
proxy switches to NEW. OLD keeps heartbeating the plugin's stream, NEW has an
empty tunnel registry, so phone requests answer 503 device_offline. The fix
must bring /hermes/d/<token>/health back to 200 within seconds, without
restarting the client.

The control test runs the same scenario with the rollover watchdog disabled
and proves the outage reproduces — so a pass above is not an accident of the
harness.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
BACKEND_DIR = ROOT / "backend"
PLUGIN_DIR = ROOT / "plugin"
TOKEN = "rollover-e2e-device-0001"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


def _spawn_relay(port: int, revision: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "HERALD_INMEMORY_DB": "1",
        "FCM_PROJECT_ID": "",
        "HERALD_EDGE_URL": "",
        "K_REVISION": revision,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


MOCK_HERMES = """
import sys, uvicorn
from fastapi import FastAPI
app = FastAPI()
@app.get("/health")
def health():
    return {"status": "ok", "who": "mock-hermes"}
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


class FrontProxy:
    """Per-request HTTP reverse proxy with a switchable upstream.

    Each request is routed when it ARRIVES; a streaming response keeps
    flowing from whichever upstream it started on. That is the property of
    Cloud Run's front end that strands the tunnel during a rollout.
    """

    def __init__(self, target: str):
        self.target = target
        self.port = _free_port()
        self._server = None
        self._task = None

    async def start(self):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import StreamingResponse
        from starlette.routing import Route

        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))

        async def handle(request: Request):
            url = f"{self.target}{request.url.path}"
            if request.url.query:
                url += f"?{request.url.query}"
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "content-length", "connection")}
            body = await request.body()
            upstream = await client.send(
                client.build_request(request.method, url, headers=headers, content=body),
                stream=True,
            )

            async def relay():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()

            hop = {"content-length", "transfer-encoding", "connection"}
            return StreamingResponse(
                relay(), status_code=upstream.status_code,
                headers={k: v for k, v in upstream.headers.items() if k.lower() not in hop},
            )

        app = Starlette(routes=[Route("/{p:path}", handle,
                                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"])])
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                             lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("proxy did not start")

    async def stop(self):
        if self._server:
            self._server.should_exit = True
            self._server.force_exit = True
            await asyncio.wait_for(self._task, timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def stack():
    old_port, new_port, hermes_port = _free_port(), _free_port(), _free_port()
    procs = [
        _spawn_relay(old_port, "herald-00003-old"),
        _spawn_relay(new_port, "herald-00004-new"),
        subprocess.Popen([sys.executable, "-c", MOCK_HERMES, str(hermes_port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        old, new = f"http://127.0.0.1:{old_port}", f"http://127.0.0.1:{new_port}"
        hermes = f"http://127.0.0.1:{hermes_port}"
        for u in (f"{old}/health", f"{new}/health", f"{hermes}/health"):
            _wait_http(u)
        # Device registration lives in Firestore (shared in prod); each stub
        # relay has its own in-memory store, so register on both.
        for base in (old, new):
            r = httpx.post(f"{base}/push/register",
                           json={"device_token": TOKEN, "fcm_token": "e2e"}, timeout=5)
            assert r.status_code == 200, r.text
        yield {"old": old, "new": new, "hermes": hermes}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


async def _health(proxy: FrontProxy) -> int:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{proxy.url}/hermes/d/{TOKEN}/health")
        return r.status_code


async def _until(pred, timeout: float, step: float = 0.2) -> float | None:
    """Seconds until pred() is truthy, or None on timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await pred():
            return time.monotonic() - t0
        await asyncio.sleep(step)
    return None


async def _health_is(proxy, code) -> bool:
    try:
        return await _health(proxy) == code
    except httpx.HTTPError:
        return False


@contextlib.asynccontextmanager
async def _deployed(stack, monkeypatch, probe_s: float):
    """Tunnel up through OLD, then switch the front end to NEW (the deploy)."""
    sys.path.insert(0, str(PLUGIN_DIR))
    from herald_relay import relay_client as rc

    monkeypatch.setattr(rc, "_ROLLOVER_PROBE_S", probe_s)
    proxy = FrontProxy(stack["old"])
    await proxy.start()
    client = rc.HeraldRelayClient(relay_url=proxy.url, device_token=TOKEN,
                                  local_hermes_url=stack["hermes"])
    task = asyncio.create_task(client.run_forever())
    try:
        up = await _until(lambda: _health_is(proxy, 200), 15)
        assert up is not None, "tunnel never came up through OLD"
        # Let the watchdog probe OLD a few times first (steady state), so a
        # watchdog that gives up on an old relay is caught.
        await asyncio.sleep(min(probe_s * 4, 2.5))
        assert await _health(proxy) == 200, "watchdog churned in steady state"
        proxy.target = stack["new"]  # ── the deploy ──
        yield proxy
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await proxy.stop()


@pytest.mark.asyncio
async def test_tunnel_follows_a_rollout_within_seconds(stack, monkeypatch):
    async with _deployed(stack, monkeypatch, probe_s=0.5) as proxy:
        # Right after the switch NEW has never seen the plugin.
        assert await _health(proxy) == 503
        took = await _until(lambda: _health_is(proxy, 200), 10)
        assert took is not None, "tunnel stayed stranded on the old instance"
        assert took < 5, f"took {took:.1f}s to move to the new instance"
        # And it really is NEW that holds it now.
        async with httpx.AsyncClient(timeout=5) as c:
            s = (await c.get(f"{stack['new']}/tunnel/status",
                             params={"device_token": TOKEN})).json()
        assert s["connected"] is True and s["revision"] == "herald-00004-new"


@pytest.mark.asyncio
async def test_control_without_watchdog_the_outage_reproduces(stack, monkeypatch):
    """Same harness, watchdog effectively off: still 503 after 4 s."""
    async with _deployed(stack, monkeypatch, probe_s=3600) as proxy:
        assert await _health(proxy) == 503
        took = await _until(lambda: _health_is(proxy, 200), 4)
        assert took is None, "harness does not reproduce the stranded stream"


@pytest.mark.asyncio
async def test_silent_server_is_detected_and_client_reconnects(monkeypatch):
    """Half-dead connection: stream open, server sends NOTHING (no heartbeat).

    The client's read timeout is the server-silence watchdog; it must drop the
    stream and reconnect rather than wait forever.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    sys.path.insert(0, str(PLUGIN_DIR))
    from herald_relay import relay_client as rc

    monkeypatch.setattr(rc, "_HEARTBEAT_TIMEOUT_S", 0.8)
    stream_opens: list[float] = []

    async def connect(_):
        return JSONResponse({"ok": True})

    async def pending(_):
        return JSONResponse({"items": []})

    async def events(_):
        stream_opens.append(time.monotonic())

        async def gen():
            await asyncio.sleep(3600)  # frozen: no bytes, no heartbeat
            yield b""

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[
        Route("/tunnel/connect", connect, methods=["POST"]),
        Route("/tunnel/pending_approvals", pending),
        Route("/tunnel/events", events),
    ])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    client = rc.HeraldRelayClient(relay_url=f"http://127.0.0.1:{port}",
                                  device_token=TOKEN, local_hermes_url="http://127.0.0.1:9")
    task = asyncio.create_task(client.run_forever())
    try:
        took = await _until(lambda: _async_true(len(stream_opens) >= 2), 6, 0.1)
        assert took is not None, "client never noticed the silent stream"
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.should_exit = True
        server.force_exit = True
        await asyncio.wait_for(srv, timeout=5)


async def _async_true(v: bool) -> bool:
    return v


PRE_FIX_REF = "c3cf65e"  # last main before this fix: no hello, no /tunnel/status


@pytest.fixture(scope="module")
def prefix_stack(tmp_path_factory):
    """OLD = the actual pre-fix relay code, NEW = this tree.

    This is the very first deploy of the fix: the client is new, but the
    instance holding its stream runs the old relay.
    """
    dest = tmp_path_factory.mktemp("prefix")
    try:
        arc = subprocess.run(["git", "-C", str(ROOT), "archive", PRE_FIX_REF, "backend"],
                             check=True, capture_output=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"pre-fix ref {PRE_FIX_REF} not available (shallow clone?)")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=arc, check=True)
    old_port, new_port, hermes_port = _free_port(), _free_port(), _free_port()
    env = {**os.environ, "HERALD_INMEMORY_DB": "1", "FCM_PROJECT_ID": "", "HERALD_EDGE_URL": ""}
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                          "--port", str(old_port), "--log-level", "warning"],
                         cwd=str(dest / "backend"), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        _spawn_relay(new_port, "herald-00005-fixed"),
        subprocess.Popen([sys.executable, "-c", MOCK_HERMES, str(hermes_port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        old, new = f"http://127.0.0.1:{old_port}", f"http://127.0.0.1:{new_port}"
        hermes = f"http://127.0.0.1:{hermes_port}"
        for u in (f"{old}/health", f"{new}/health", f"{hermes}/health"):
            _wait_http(u)
        assert httpx.get(f"{old}/tunnel/status", timeout=5).status_code in (404, 405, 422), \
            "OLD is supposed to be the pre-fix relay"
        for base in (old, new):
            r = httpx.post(f"{base}/push/register",
                           json={"device_token": TOKEN, "fcm_token": "e2e"}, timeout=5)
            assert r.status_code == 200, r.text
        yield {"old": old, "new": new, "hermes": hermes}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.mark.asyncio
async def test_first_deploy_of_the_fix_moves_off_a_pre_fix_instance(prefix_stack, monkeypatch):
    async with _deployed(prefix_stack, monkeypatch, probe_s=0.5) as proxy:
        assert await _health(proxy) == 503
        took = await _until(lambda: _health_is(proxy, 200), 10)
        assert took is not None and took < 5, f"stranded on the pre-fix instance ({took})"
