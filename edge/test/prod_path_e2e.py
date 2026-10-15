"""Production path: https://herald-api.../hermes/d/<token> -> GCE relay -> Cloudflare edge -> latitude Hermes."""
import asyncio, json, sys, time
from pathlib import Path
import httpx

T = sys.argv[1]
BASE = f"https://herald-api.eventhorizonlabs.xyz/hermes/d/{T}"
KEY = next(l.split("=", 1)[1].strip().strip('"') for l in Path.home().joinpath(".hermes/.env").read_text().splitlines()
           if l.startswith("API_SERVER_KEY="))
H = {"Authorization": f"Bearer {KEY}"}

async def main():
    async with httpx.AsyncClient(timeout=300) as c:
        t0 = time.time()
        r = await c.post(f"{BASE}/v1/runs", headers=H, json={"input": "Run `hostname` and `free -h` in the terminal and reply with one line: hostname and total RAM."})
        run_id = r.json().get("run_id"); print("start:", r.status_code, run_id, f"{(time.time()-t0)*1000:.0f}ms")
        last, n = None, 0
        async with c.stream("GET", f"{BASE}/v1/runs/{run_id}/events", headers=H) as s:
            async for line in s.aiter_lines():
                if line.startswith("id:"): last = line[3:].strip()
                if line.startswith("data:"):
                    n += 1
                    if n == 1: print(f"first event after {(time.time()-t0)*1000:.0f}ms")
                    if n >= 3: break
        print(f"dropped after {n} events (last id {last}); offline 10s")
        await asyncio.sleep(10)
        final = None
        async with c.stream("GET", f"{BASE}/v1/runs/{run_id}/events", headers={**H, "Last-Event-ID": last or "0"}) as s:
            async for line in s.aiter_lines():
                if line.startswith("data:"):
                    try: ev = json.loads(line[5:])
                    except ValueError: continue
                    if ev.get("event") in ("run.completed", "run.failed"): final = ev
        print("final:", (final or {}).get("event"), repr((final or {}).get("output")), f"total {time.time()-t0:.1f}s")

asyncio.run(main())
