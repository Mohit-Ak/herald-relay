"""Run events must stream INCREMENTALLY, not arrive in one burst at the end.

Measured before this fix, on a real 75-second run through the relay:

    [  4.1s] RUN mode=working
    [ 88.5s] CHECKPOINT: Still going — started terminal.
    [ 88.5s] CHECKPOINT: Still going — finished terminal.
    [ 88.5s] CHECKPOINT: Still going — started terminal; finished terminal.
    [ 88.5s] RUN mode=done

Every spoken checkpoint landed at 88.5s. The user heard NOTHING for ~85
seconds — exactly the silence checkpoints exist to prevent — because
``_sse_generator`` did a single ``forward_http`` round-trip and emitted the
whole buffered body as one event.
"""
import asyncio

import pytest

import routers.tunnel as tunnel_mod


@pytest.fixture
def token():
    return "dev-token-stream"


@pytest.mark.asyncio
class TestOpenStream:
    async def test_chunks_are_yielded_as_they_arrive(self, token, monkeypatch):
        """Each pushed chunk must surface immediately, not at the end."""
        q: asyncio.Queue = asyncio.Queue()
        monkeypatch.setitem(tunnel_mod._plugin_queues, token, q)

        got = []

        async def consume():
            async for chunk in tunnel_mod.open_stream(token, "GET", "/v1/runs/x/events"):
                got.append(chunk)

        task = asyncio.create_task(consume())
        # Let open_stream register its waiter and enqueue the request.
        req = await asyncio.wait_for(q.get(), timeout=2)
        assert req["stream"] is True
        rid = req["request_id"]

        # Push three chunks with gaps; each must be observable before the next.
        for i in range(3):
            await tunnel_mod.tunnel_http_stream(
                tunnel_mod.HttpStreamChunkRequest(
                    device_token=token, request_id=rid, chunk=f"data: e{i}\n\n"
                )
            )
            await asyncio.sleep(0.05)
            assert len(got) == i + 1, f"chunk {i} was buffered, not streamed"

        await tunnel_mod.tunnel_http_stream(
            tunnel_mod.HttpStreamChunkRequest(
                device_token=token, request_id=rid, chunk="", done=True
            )
        )
        await asyncio.wait_for(task, timeout=2)
        assert got == ["data: e0\n\n", "data: e1\n\n", "data: e2\n\n"]

    async def test_done_closes_the_stream(self, token, monkeypatch):
        q: asyncio.Queue = asyncio.Queue()
        monkeypatch.setitem(tunnel_mod._plugin_queues, token, q)

        got = []

        async def consume():
            async for chunk in tunnel_mod.open_stream(token, "GET", "/p"):
                got.append(chunk)

        task = asyncio.create_task(consume())
        req = await asyncio.wait_for(q.get(), timeout=2)
        await tunnel_mod.tunnel_http_stream(
            tunnel_mod.HttpStreamChunkRequest(
                device_token=token, request_id=req["request_id"],
                chunk="data: only\n\n", done=True,
            )
        )
        await asyncio.wait_for(task, timeout=2)
        assert got == ["data: only\n\n"]

    async def test_legacy_single_shot_plugin_still_works(self, token, monkeypatch):
        """An older plugin answers /tunnel/http_response instead of streaming."""
        q: asyncio.Queue = asyncio.Queue()
        monkeypatch.setitem(tunnel_mod._plugin_queues, token, q)

        got = []

        async def consume():
            async for chunk in tunnel_mod.open_stream(token, "POST", "/v1/runs"):
                got.append(chunk)

        task = asyncio.create_task(consume())
        req = await asyncio.wait_for(q.get(), timeout=2)
        await tunnel_mod.tunnel_http_response(
            tunnel_mod.HttpResponseRequest(
                device_token=token, request_id=req["request_id"],
                status=202, body={"run_id": "run_legacy"},
            )
        )
        await asyncio.wait_for(task, timeout=2)
        assert len(got) == 1
        assert "run_legacy" in got[0]

    async def test_unknown_plugin_raises(self):
        with pytest.raises(ConnectionError):
            async for _ in tunnel_mod.open_stream("nope", "GET", "/p"):
                pass

    async def test_late_chunk_is_ignored(self, token):
        """A chunk for a finished request must not blow up."""
        r = await tunnel_mod.tunnel_http_stream(
            tunnel_mod.HttpStreamChunkRequest(
                device_token=token, request_id="gone", chunk="x"
            )
        )
        assert r["ok"] is False
