"""Repro: N devices, redeploy, then hit EVERY device and report per-device status + body."""
import asyncio, logging, secrets, subprocess, sys, time, os
from collections import Counter
import aiohttp
from aiohttp import web
sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")
from herald_relay.edge_tunnel import EdgeTunnel  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
EDGE = sys.argv[1].rstrip("/"); N = int(sys.argv[2])

async def main():
    app = web.Application()
    async def health(_r): return web.json_response({"ok": True})
    app.add_routes([web.get("/health", health)])
    run = web.AppRunner(app); await run.setup(); await web.TCPSite(run, "127.0.0.1", 18644).start()
    tuns = [EdgeTunnel(EDGE, "dev_" + secrets.token_hex(16), "http://127.0.0.1:18644") for _ in range(N)]
    tasks = [asyncio.create_task(t.run_forever()) for t in tuns]
    t0 = time.time()
    while sum(t.connected.is_set() for t in tuns) < N and time.time() - t0 < 60:
        await asyncio.sleep(0.5)
    print(f"connected {sum(t.connected.is_set() for t in tuns)}/{N} in {time.time()-t0:.1f}s", flush=True)
    env = dict(os.environ)
    p = await asyncio.create_subprocess_shell("cd /home/mohit/projects/herald-relay/edge && npx -y wrangler@latest deploy 2>&1 | grep -E 'Version ID'", env=env)
    await p.wait()
    print("redeployed", flush=True)
    for wait in (5, 60):
        await asyncio.sleep(wait)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as http:
            async def hit(t):
                try:
                    async with http.get(f"{EDGE}/d/{t.device_token}/health") as r:
                        return r.status, (await r.text())[:60], t.connected.is_set()
                except Exception as e:  # noqa: BLE001
                    return type(e).__name__, "", t.connected.is_set()
            res = await asyncio.gather(*[hit(t) for t in tuns])
        print(f"+{wait}s after deploy:", Counter((s, b if s != 200 else "", c) for s, b, c in res).most_common(6), flush=True)
    for t in tuns: await t.stop()
    for t in tasks: t.cancel()
    await run.cleanup()

asyncio.run(main())
