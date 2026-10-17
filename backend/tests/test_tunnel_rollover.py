"""Tunnel streams must not stay stranded on an old instance after a deploy.

Observed 2026-10-17: a Cloud Run rollout (herald-00003 -> herald-00004) left
the plugin's GET /tunnel/events pinned to the draining old instance, which
kept heartbeating; the new instance had an empty tunnel registry, so every
/hermes/d/<token>/** answered 503 device_offline until the stream hit the
3600 s request timeout. These tests pin the server half of the fix.
"""
import asyncio
import json

import pytest

import routers.tunnel as tunnel_mod


async def _drain(gen, n):
    out = []
    async for chunk in gen:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


@pytest.mark.asyncio
async def test_stream_recycles_after_max_lifetime_when_idle():
    q: asyncio.Queue = asyncio.Queue()
    chunks = []
    async for c in tunnel_mod._event_generator(q, heartbeat_interval=0.05, max_lifetime=0.2):
        chunks.append(c)
        assert len(chunks) < 50, "stream never recycled"
    assert chunks and all(c == ": heartbeat\n\n" for c in chunks)
    assert q._herald_recycled is True


@pytest.mark.asyncio
async def test_recycle_never_drops_a_queued_item():
    """Expiry waits for the queue to drain — a forward is never stranded."""
    q: asyncio.Queue = asyncio.Queue()
    await q.put({"type": "forward_request", "request_id": "a"})
    await q.put({"type": "forward_request", "request_id": "b"})
    got = []
    async for c in tunnel_mod._event_generator(q, heartbeat_interval=0.05, max_lifetime=0):
        got.append(c)
    data = [json.loads(c[6:]) for c in got if c.startswith("data: ")]
    assert [d["request_id"] for d in data] == ["a", "b"]


@pytest.mark.asyncio
async def test_close_sentinel_ends_stream_immediately():
    q: asyncio.Queue = asyncio.Queue()
    tunnel_mod._plugin_queues["tok-close"] = q
    try:
        gen = tunnel_mod._event_generator(q, heartbeat_interval=30)
        assert tunnel_mod.close_all_streams() >= 1
        got = await asyncio.wait_for(_drain(gen, 1), timeout=1)
        assert got == []
    finally:
        tunnel_mod._plugin_queues.pop("tok-close", None)


@pytest.mark.asyncio
async def test_events_stream_opens_with_hello_and_identity_headers(mock_firestore, monkeypatch):
    monkeypatch.setenv("K_REVISION", "herald-00042-abc")
    mock_firestore.collection("devices").document("tok-hello").set({"fcm_token": "x"})
    resp = await tunnel_mod.tunnel_events(device_token="tok-hello", authorization=None,
                                          last_event_id=None)
    try:
        assert resp.headers["x-herald-revision"] == "herald-00042-abc"
        assert resp.headers["x-herald-instance"] == tunnel_mod.INSTANCE_ID
        first = await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=1)
        hello = json.loads(first[6:])
        assert hello == {"type": "hello", "revision": "herald-00042-abc",
                         "instance": tunnel_mod.INSTANCE_ID}
    finally:
        await resp.body_iterator.aclose()
    assert "tok-hello" not in tunnel_mod._plugin_queues


@pytest.mark.asyncio
async def test_status_reports_instance_of_a_fresh_request():
    tunnel_mod._plugin_queues["tok-status"] = asyncio.Queue()
    try:
        s = await tunnel_mod.tunnel_status(device_token="tok-status")
        assert s["instance"] == tunnel_mod.INSTANCE_ID and s["connected"] is True
        s = await tunnel_mod.tunnel_status(device_token="nobody")
        assert s["connected"] is False
    finally:
        tunnel_mod._plugin_queues.pop("tok-status", None)


@pytest.mark.asyncio
async def test_recycled_queue_is_kept_for_reconnect_and_reused(mock_firestore, monkeypatch):
    """A request that lands in the reconnect gap is delivered, not 503'd."""
    monkeypatch.setattr(tunnel_mod, "TUNNEL_MAX_STREAM_S", 0.15)
    monkeypatch.setattr(tunnel_mod, "TUNNEL_RECYCLE_GRACE_S", 5)
    mock_firestore.collection("devices").document("tok-gap").set({"fcm_token": "x"})

    resp = await tunnel_mod.tunnel_events(device_token="tok-gap", authorization=None,
                                          last_event_id=None)
    # Consume until the server recycles the stream.
    async for _ in resp.body_iterator:
        pass
    q = tunnel_mod._plugin_queues.get("tok-gap")
    assert q is not None, "queue dropped on recycle — gap requests would 503"
    assert tunnel_mod.is_plugin_connected("tok-gap")

    # A phone request during the gap is queued...
    await q.put({"type": "forward_request", "request_id": "gap-1"})

    # ...and the reconnecting plugin receives it on the new stream.
    monkeypatch.setattr(tunnel_mod, "TUNNEL_MAX_STREAM_S", 300)
    resp2 = await tunnel_mod.tunnel_events(device_token="tok-gap", authorization=None,
                                           last_event_id=None)
    try:
        assert tunnel_mod._plugin_queues["tok-gap"] is q
        it = resp2.body_iterator.__aiter__()
        hello = await asyncio.wait_for(it.__anext__(), timeout=1)
        assert '"hello"' in hello
        nxt = await asyncio.wait_for(it.__anext__(), timeout=1)
        assert json.loads(nxt[6:])["request_id"] == "gap-1"
    finally:
        await resp2.body_iterator.aclose()
        tunnel_mod._plugin_queues.pop("tok-gap", None)


@pytest.mark.asyncio
async def test_orphan_is_dropped_when_plugin_never_returns(mock_firestore, monkeypatch):
    monkeypatch.setattr(tunnel_mod, "TUNNEL_MAX_STREAM_S", 0.1)
    monkeypatch.setattr(tunnel_mod, "TUNNEL_RECYCLE_GRACE_S", 0.2)
    mock_firestore.collection("devices").document("tok-gone").set({"fcm_token": "x"})
    resp = await tunnel_mod.tunnel_events(device_token="tok-gone", authorization=None,
                                          last_event_id=None)
    async for _ in resp.body_iterator:
        pass
    assert tunnel_mod.is_plugin_connected("tok-gone")
    await asyncio.sleep(0.4)
    assert not tunnel_mod.is_plugin_connected("tok-gone")


@pytest.mark.asyncio
async def test_client_disconnect_is_not_given_a_grace(mock_firestore):
    """Only SERVER recycles get the grace; a plugin that hangs up is gone."""
    mock_firestore.collection("devices").document("tok-hangup").set({"fcm_token": "x"})
    resp = await tunnel_mod.tunnel_events(device_token="tok-hangup", authorization=None,
                                          last_event_id=None)
    it = resp.body_iterator.__aiter__()
    await it.__anext__()  # hello
    await resp.body_iterator.aclose()
    assert not tunnel_mod.is_plugin_connected("tok-hangup")


@pytest.mark.asyncio
async def test_recycle_is_deferred_while_a_forward_is_in_flight():
    """Older plugins close their HTTP client when the stream ends; recycling
    under a live forward would cut a long run off mid-answer."""
    q: asyncio.Queue = asyncio.Queue()
    tunnel_mod._plugin_queues["tok-busy"] = q
    try:
        fwd = asyncio.create_task(
            tunnel_mod.forward_http("tok-busy", "GET", "/slow", timeout=5))
        await asyncio.sleep(0)  # forward registers + enqueues
        gen = tunnel_mod._event_generator(
            q, heartbeat_interval=0.05, max_lifetime=0.1,
            busy=lambda: tunnel_mod._inflight.get("tok-busy", 0) > 0)
        first = await gen.__anext__()
        req = json.loads(first[6:])
        await asyncio.sleep(0.3)  # well past max_lifetime
        assert (await gen.__anext__()) == ": heartbeat\n\n", "recycled under a forward"
        await tunnel_mod.tunnel_http_response(tunnel_mod.HttpResponseRequest(
            device_token="tok-busy", request_id=req["request_id"], body={"ok": 1}))
        assert (await fwd)["body"] == {"ok": 1}
        assert "tok-busy" not in tunnel_mod._inflight
        rest = [c async for c in gen]  # now free to recycle
        assert len(rest) <= 1
    finally:
        tunnel_mod._plugin_queues.pop("tok-busy", None)
