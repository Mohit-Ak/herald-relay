import asyncio, json, secrets, sys, time
import aiohttp
EDGE = sys.argv[1]; HB = float(sys.argv[2]) if sys.argv[2] != "none" else None; APP = sys.argv[3] == "app"
async def main():
    tok = "dev_" + secrets.token_hex(16)
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(EDGE.replace("https", "wss") + "/agent/connect",
                                   headers={"Authorization": f"Bearer {tok}"}, heartbeat=HB)
        async def reader():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT and m.data != "pong":
                    d = json.loads(m.data)
                    if d.get("t") == "req":
                        for f in ({"t": "res", "id": d["id"], "status": 200, "headers": {}},
                                  {"t": "chunk", "id": d["id"], "data": "ok"}, {"t": "end", "id": d["id"]}):
                            await ws.send_str(json.dumps(f))
            print(f"  socket closed code={ws.close_code} exc={ws.exception()}", flush=True)
        async def pinger():
            while True:
                await asyncio.sleep(50); await ws.send_str("ping")
        tasks = [asyncio.create_task(reader())] + ([asyncio.create_task(pinger())] if APP else [])
        t0 = time.time()
        for _ in range(6):
            await asyncio.sleep(30)
            async with http.get(f"{EDGE}/d/{tok}/x") as r:
                print(f"t={time.time()-t0:.0f}s status={r.status} {(await r.text())[:60]} closed={ws.closed}", flush=True)
        for t in tasks: t.cancel()
asyncio.run(main())
