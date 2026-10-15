"""Live E2E for the edge relay: real Cloudflare DO, real WebSocket, local fake Hermes.

Usage: python edge_e2e.py <edge_url>
"""
import asyncio
import json
import secrets
import sys
import time

import aiohttp
from aiohttp import web

sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")
from herald_relay.edge_tunnel import EdgeTunnel  # noqa: E402

EDGE = sys.argv[1].rstrip("/")
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


cancelled = asyncio.Event()
seen_headers = {}


async def fake_hermes():
    app = web.Application()

    async def health(req):
        seen_headers.update(dict(req.headers))
        return web.json_response({"status": "ok", "emoji": "★ ♪ こんにちは"})

    async def echo(req):
        return web.json_response({"got": await req.json(), "q": req.query_string})

    async def events(req):
        resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(req)
        start = int(req.headers.get("Last-Event-ID", "0"))
        try:
            for i in range(start + 1, start + 6):
                await resp.write(f"id: {i}\ndata: {json.dumps({'seq': i, 't': time.time()})}\n\n".encode())
                await asyncio.sleep(0.6)
        except (ConnectionResetError, asyncio.CancelledError):
            cancelled.set()
            raise
        return resp

    async def slow(req):
        resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(req)
        try:
            for i in range(100):
                await resp.write(f"data: {i}\n\n".encode())
                await asyncio.sleep(0.3)
        except (ConnectionResetError, asyncio.CancelledError):
            cancelled.set()
            raise
        return resp

    app.add_routes([web.get("/health", health), web.post("/echo", echo),
                    web.get("/v1/runs/r1/events", events), web.get("/slow", slow)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18642)
    await site.start()
    return runner


async def main():
    runner = await fake_hermes()
    token = "dev_" + secrets.token_hex(16)
    base = f"{EDGE}/d/{token}"
    async with aiohttp.ClientSession() as http:
        # 1. offline device -> clean 503, not a hang
        t0 = time.time()
        async with http.get(f"{base}/health") as r:
            body = await r.json()
            check("offline device answers 503 device_offline", r.status == 503 and body.get("error") == "device_offline",
                  f"{r.status} {body} {time.time()-t0:.2f}s")

        tun = EdgeTunnel(EDGE, token, "http://127.0.0.1:18642", hermes_key="local-fallback-key")
        task = asyncio.create_task(tun.run_forever())
        await asyncio.wait_for(tun.connected.wait(), 15)
        await asyncio.sleep(0.5)

        async with http.get(f"{base}/_edge/status") as r:
            st = await r.json()
            check("status shows device online", st.get("online") is True, str(st))

        # 2. plain JSON round trip, caller auth forwarded, unicode intact
        lat = []
        for _ in range(5):
            t0 = time.time()
            async with http.get(f"{base}/health", headers={"Authorization": "Bearer caller-profile-key",
                                                          "X-Evil": "1"}) as r:
                body = await r.json()
            lat.append((time.time() - t0) * 1000)
        check("GET round trip + unicode", r.status == 200 and body.get("emoji") == "★ ♪ こんにちは", str(body))
        check("caller Authorization forwarded, others dropped",
              seen_headers.get("Authorization") == "Bearer caller-profile-key" and "X-Evil" not in seen_headers)
        print(f"     latency ms: {[round(x) for x in lat]}")

        # 3. POST body + query string
        async with http.post(f"{base}/echo?a=1&b=two", json={"x": [1, 2, 3]}) as r:
            body = await r.json()
            check("POST body + query preserved", body == {"got": {"x": [1, 2, 3]}, "q": "a=1&b=two"}, str(body))

        # 4. streaming arrives incrementally, not buffered
        stamps = []
        async with http.get(f"{base}/v1/runs/r1/events") as r:
            check("SSE content-type passed through", "text/event-stream" in r.headers.get("content-type", ""))
            async for line in r.content:
                if line.startswith(b"data:"):
                    stamps.append(time.time())
        spread = stamps[-1] - stamps[0] if len(stamps) > 1 else 0
        check("SSE streamed live (5 events spread over >=2s)", len(stamps) == 5 and spread >= 2.0,
              f"n={len(stamps)} spread={spread:.2f}s")

        # 5. resume with Last-Event-ID reaches Hermes
        seqs = []
        async with http.get(f"{base}/v1/runs/r1/events", headers={"Last-Event-ID": "3"}) as r:
            async for line in r.content:
                if line.startswith(b"data:"):
                    seqs.append(json.loads(line[5:])["seq"])
        check("Last-Event-ID resume forwarded", seqs[:1] == [4], str(seqs))

        # 6. caller hangs up -> device stops reading the local stream
        cancelled.clear()
        r = await http.get(f"{base}/slow")
        await r.content.readline()
        r.close()
        try:
            await asyncio.wait_for(cancelled.wait(), 10)
            check("caller hang-up cancels the local read", True)
        except asyncio.TimeoutError:
            check("caller hang-up cancels the local read", False, "local stream still running after 10s")

        # 7. concurrency: 20 parallel requests multiplexed on one socket
        async def one(i):
            async with http.post(f"{base}/echo", json={"i": i}) as rr:
                return (await rr.json())["got"]["i"]
        got = await asyncio.gather(*[one(i) for i in range(20)])
        check("20 concurrent requests multiplexed correctly", sorted(got) == list(range(20)))

        # 8. device drop mid-flight -> caller gets an error promptly, and reconnect works
        await tun.stop()
        task.cancel()
        await asyncio.sleep(1.5)
        async with http.get(f"{base}/health") as r:
            check("after device disconnect: 503", r.status == 503, str(r.status))
        tun2 = EdgeTunnel(EDGE, token, "http://127.0.0.1:18642")
        task2 = asyncio.create_task(tun2.run_forever())
        await asyncio.wait_for(tun2.connected.wait(), 15)
        await asyncio.sleep(0.5)
        async with http.get(f"{base}/health") as r:
            check("reconnect restores service", r.status == 200, str(r.status))

        # 9. hibernation: idle > 15s, then a request must still work
        await asyncio.sleep(20)
        t0 = time.time()
        async with http.get(f"{base}/health") as r:
            check("request after 20s idle (hibernated) still works", r.status == 200,
                  f"{(time.time()-t0)*1000:.0f}ms")
        await tun2.stop()
        task2.cancel()

        # 10. auth: wrong token cannot reach another device
        async with http.get(f"{EDGE}/d/dev_{secrets.token_hex(16)}/health") as r:
            check("unknown token isolated (503, not someone else's device)", r.status == 503)

    await runner.cleanup()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results)-len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
