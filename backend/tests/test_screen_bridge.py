"""The screen pipe, against a real server and real WebSocket clients.

What must hold: a viewing joins exactly one phone to the computer that made it;
bytes pass both ways untouched; a wrong device, an unknown or reused sid, and a
computer nobody came for all end closed.
"""
from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
import websockets

TOKEN = "device-token-for-screen-tests-01"
SID = "s" * 43


@pytest.fixture
async def base(monkeypatch):
    from main import app
    from routers import screen_bridge
    monkeypatch.setattr(screen_bridge, "ATTACH_TIMEOUT_S", 0.6)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    await task
    screen_bridge._waiting.clear()


def agent(base, sid=SID, token=TOKEN):
    return websockets.connect(f"{base}/tunnel/screen/{sid}",
                              additional_headers={"Authorization": f"Bearer {token}"})


def phone(base, sid=SID, token=TOKEN):
    return websockets.connect(f"{base}/hermes/d/{token}/_screen/{sid}", subprotocols=["binary"])


async def closed_with(ws) -> int:
    with pytest.raises(websockets.ConnectionClosed) as e:
        await asyncio.wait_for(ws.recv(), 3)
    return e.value.rcvd.code if e.value.rcvd else 1006


async def test_bytes_pass_both_ways_untouched(base):
    async with agent(base) as a:
        await a.send(b"RFB 003.008\n")          # sent before the phone exists: must not be lost
        await asyncio.sleep(0.05)
        async with phone(base) as p:
            assert p.subprotocol == "binary"
            assert await asyncio.wait_for(p.recv(), 3) == b"RFB 003.008\n"
            blob = bytes(range(256)) * 600       # 153,600 bytes, every byte value
            await p.send(blob)
            assert await asyncio.wait_for(a.recv(), 3) == blob
            await a.send(blob[::-1])
            assert await asyncio.wait_for(p.recv(), 3) == blob[::-1]


async def test_the_phone_leaving_closes_the_computer_side(base):
    async with agent(base) as a:
        async with phone(base):
            pass
        assert await closed_with(a) == 1000


async def test_a_broken_phone_link_is_told_apart_from_leaving(base):
    """The computer keeps the agent out for a moment after a broken link (the
    user may be mid sign-in) but hands control back at once when they leave."""
    async with agent(base) as a:
        p = await phone(base).__aenter__()
        p.transport.abort()                       # the link dies: no close frame
        assert await closed_with(a) == 4410


async def test_another_devices_address_cannot_take_the_viewing(base):
    async with agent(base) as a:
        async with phone(base, token="some-other-device-token-000002") as p:
            assert await closed_with(p) == 4404
        # ...and the real phone still can.
        async with phone(base) as p:
            await a.send(b"ok")
            assert await asyncio.wait_for(p.recv(), 3) == b"ok"


async def test_a_viewing_joins_one_phone_only(base):
    async with agent(base):
        async with phone(base):
            async with phone(base) as second:
                assert await closed_with(second) == 4404


async def test_unknown_sid_is_refused(base):
    async with phone(base, sid="u" * 43) as p:
        assert await closed_with(p) == 4404


async def test_a_computer_nobody_came_for_is_closed(base):
    async with agent(base) as a:
        assert await closed_with(a) == 4408
    async with phone(base) as p:                 # too late: the sid is gone
        assert await closed_with(p) == 4404


async def test_computer_side_needs_a_device_token_and_a_real_sid(base):
    async with websockets.connect(f"{base}/tunnel/screen/{SID}") as a:
        assert await closed_with(a) == 4400
    async with agent(base, sid="short") as a:
        assert await closed_with(a) == 4400


async def test_a_device_cannot_park_unlimited_sockets(base):
    held = [await agent(base, sid=f"{i}" * 43).__aenter__() for i in range(4)]
    try:
        async with agent(base, sid="9" * 43) as extra:
            assert await closed_with(extra) == 4429
    finally:
        for h in held:
            await h.close()
