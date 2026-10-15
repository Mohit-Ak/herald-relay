"""Real Hermes through the edge relay: start a run, stream it, drop mid-run, resume."""
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")
from herald_relay.edge_tunnel import EdgeTunnel  # noqa: E402

EDGE = sys.argv[1].rstrip("/")
KEY = next(l.split("=", 1)[1].strip().strip('"') for l in Path.home().joinpath(".hermes/.env").read_text().splitlines()
           if l.startswith("API_SERVER_KEY="))


async def main():
    token = "dev_" + secrets.token_hex(16)
    tun = EdgeTunnel(EDGE, token, "http://127.0.0.1:8642")
    t = asyncio.create_task(tun.run_forever())
    await asyncio.wait_for(tun.connected.wait(), 15)
    base = f"{EDGE}/d/{token}"
    H = {"Authorization": f"Bearer {KEY}"}
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base}/health", headers=H) as r:
            print("health via edge:", r.status, (await r.text())[:120])
        prompt = ("Run `hostname` and `free -h` in the terminal, then reply with one line: "
                  "the hostname and total RAM. Before running them, say 'checking'.")
        async with http.post(f"{base}/v1/runs", headers=H, json={"input": prompt}) as r:
            body = await r.json()
            print("start run:", r.status, json.dumps(body)[:200])
        run_id = body.get("run_id") or body.get("id")
        # read part of the stream, then hang up (simulates the call dropping)
        last_id, n1, t0 = None, 0, time.time()
        r = await http.get(f"{base}/v1/runs/{run_id}/events", headers=H)
        async for line in r.content:
            s = line.decode().strip()
            if s.startswith("id:"):
                last_id = s[3:].strip()
            if s.startswith("data:"):
                n1 += 1
                if n1 >= 3:
                    break
        r.close()
        print(f"dropped after {n1} events, last id={last_id}, {time.time()-t0:.1f}s")
        await asyncio.sleep(8)  # phone is offline for a bit
        n2, final, types = 0, "", []
        hdr = dict(H)
        if last_id:
            hdr["Last-Event-ID"] = last_id
        async with http.get(f"{base}/v1/runs/{run_id}/events", headers=hdr,
                            timeout=aiohttp.ClientTimeout(total=300)) as r:
            print("resume:", r.status)
            async for line in r.content:
                s = line.decode().strip()
                if s.startswith("data:"):
                    n2 += 1
                    try:
                        ev = json.loads(s[5:])
                    except ValueError:
                        continue
                    types.append(ev.get("event") or ev.get("type"))
                    if (ev.get("event") or ev.get("type")) in ("run.completed", "run.failed"):
                        final = json.dumps(ev)[:400]
        print(f"resumed: {n2} more events; types tail={types[-4:]}")
        print("final:", final)
    await tun.stop()
    t.cancel()

asyncio.run(main())
