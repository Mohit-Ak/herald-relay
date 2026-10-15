"""Scale probe: N idle devices hold sockets on the edge; then a few become active."""
import asyncio
import random
import secrets
import sys
import time

import aiohttp
from aiohttp import web

EDGE = sys.argv[1].rstrip("/")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
HOLD = int(sys.argv[3]) if len(sys.argv) > 3 else 90


async def device(http, token, ready, stop, stats):
    url = EDGE.replace("https://", "wss://") + "/agent/connect"
    try:
        async with http.ws_connect(url, headers={"Authorization": f"Bearer {token}"}, heartbeat=50) as ws:
            ready.append(token)
            p = asyncio.create_task(asyncio.sleep(0))
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT or msg.data == "pong":
                        stats["pongs"] += msg.data == "pong"
                        continue
                    import json
                    m = json.loads(msg.data)
                    if m.get("t") == "req":
                        await ws.send_str(json.dumps({"t": "res", "id": m["id"], "status": 200,
                                                      "headers": {"content-type": "application/json"}}))
                        await ws.send_str(json.dumps({"t": "chunk", "id": m["id"], "data": '{"ok":true}'}))
                        await ws.send_str(json.dumps({"t": "end", "id": m["id"]}))
                    if stop.is_set():
                        break
            finally:
                p.cancel()
    except Exception as exc:
        stats["errors"].append(type(exc).__name__)


async def main():
    tokens = ["dev_" + secrets.token_hex(16) for _ in range(N)]
    ready, stop = [], asyncio.Event()
    stats = {"errors": [], "pongs": 0}
    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn) as http:
        t0 = time.time()
        tasks = []
        for i, tok in enumerate(tokens):
            tasks.append(asyncio.create_task(device(http, tok, ready, stop, stats)))
            if i % 50 == 49:
                await asyncio.sleep(0.2)
        while len(ready) + len(stats["errors"]) < N and time.time() - t0 < 120:
            await asyncio.sleep(1)
        print(f"connected {len(ready)}/{N} in {time.time()-t0:.1f}s; errors={len(stats['errors'])} {set(stats['errors'])}", flush=True)
        print(f"holding {HOLD}s idle...", flush=True)
        await asyncio.sleep(HOLD)
        # 30 random devices become active
        lat, bad, fails = [], 0, []
        async def hit(tok):
            nonlocal bad
            t = time.time()
            async with http.get(f"{EDGE}/d/{tok}/health") as r:
                body = (await r.read())[:80]
                if r.status != 200:
                    bad += 1
                    fails.append((r.status, body))
            lat.append((time.time() - t) * 1000)
        await asyncio.gather(*[hit(t) for t in random.sample(ready, min(30, len(ready)))])
        lat.sort()
        print(f"active after idle: {len(lat)-bad}/{len(lat)} ok, p50={lat[len(lat)//2]:.0f}ms p95={lat[int(len(lat)*.95)-1]:.0f}ms")
        from collections import Counter
        print("failures:", Counter(fails).most_common(5))
        print(f"pongs received while idle: {stats['pongs']}; still connected: {sum(not t.done() for t in tasks)}")
        stop.set()
        for t in tasks:
            t.cancel()


asyncio.run(main())
