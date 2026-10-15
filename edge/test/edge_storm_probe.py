"""Reconnect-storm probe: N real EdgeTunnel devices, then redeploy the Worker
(which drops every socket at once) and measure how fast the fleet recovers.

Usage: python edge_storm_probe.py <edge_url> <N>
Run `wrangler deploy` while it prints "REDEPLOY NOW".
"""
import asyncio
import logging
import random
import secrets
import sys
import time

import aiohttp
from aiohttp import web

sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")
from herald_relay.edge_tunnel import EdgeTunnel  # noqa: E402

logging.basicConfig(level=logging.ERROR)
EDGE = sys.argv[1].rstrip("/")
N = int(sys.argv[2])
TRIGGER = "/home/mohit/.hermes/cache/scratch/storm_redeploy.flag"


async def main():
    app = web.Application()
    async def health(_req):
        return web.json_response({"ok": True})

    app.add_routes([web.get("/health", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18643).start()

    tuns = [EdgeTunnel(EDGE, "dev_" + secrets.token_hex(16), "http://127.0.0.1:18643") for _ in range(N)]
    tasks = []
    t0 = time.time()
    for i, t in enumerate(tuns):
        tasks.append(asyncio.create_task(t.run_forever()))
        if i % 50 == 49:
            await asyncio.sleep(0.2)
    while sum(t.connected.is_set() for t in tuns) < N and time.time() - t0 < 120:
        await asyncio.sleep(0.5)
    up = sum(t.connected.is_set() for t in tuns)
    print(f"initial: {up}/{N} connected in {time.time()-t0:.1f}s", flush=True)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as http:
        async def sample(label, k=50):
            lat, bad = [], []
            async def hit(t):
                s = time.time()
                try:
                    async with http.get(f"{EDGE}/d/{t.device_token}/health") as r:
                        await r.read()
                        (lat if r.status == 200 else bad).append((time.time() - s) * 1000 if r.status == 200 else r.status)
                except Exception as e:  # noqa: BLE001
                    bad.append(type(e).__name__)
            await asyncio.gather(*[hit(t) for t in random.sample(tuns, k)])
            lat.sort()
            p50 = lat[len(lat) // 2] if lat else 0
            p95 = lat[int(len(lat) * .95) - 1] if lat else 0
            print(f"{label}: {len(lat)}/{k} ok p50={p50:.0f}ms p95={p95:.0f}ms bad={bad[:5]}", flush=True)

        await sample("before storm")
        open(TRIGGER, "w").close()
        print("REDEPLOY NOW", flush=True)
        # watch connections drop and recover
        t_drop, saw_drop, timeline = None, False, []
        start = time.time()
        while time.time() - start < 180:
            c = sum(t.connected.is_set() for t in tuns)
            timeline.append((round(time.time() - start, 1), c))
            if c < N and not saw_drop:
                saw_drop, t_drop = True, time.time()
                print(f"drop detected at +{time.time()-start:.1f}s ({c}/{N})", flush=True)
            if saw_drop and c == N:
                print(f"fleet fully reconnected {time.time()-t_drop:.1f}s after the drop", flush=True)
                break
            await asyncio.sleep(0.5)
        low = min(c for _, c in timeline)
        print(f"lowest connected: {low}/{N}; timeline sample: {timeline[::max(1, len(timeline)//12)]}", flush=True)
        await asyncio.sleep(2)
        await sample("after storm")

    for t in tuns:
        await t.stop()
    for t in tasks:
        t.cancel()
    await runner.cleanup()


asyncio.run(main())
