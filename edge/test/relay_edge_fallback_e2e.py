"""Relay -> edge fallback, real: local relay app (edge enabled) + real Cloudflare edge
+ device on the edge only + the REAL local Hermes. Exercises the exact URLs the
Herald backend uses (/hermes/d/<token>/...)."""
import asyncio, json, os, secrets, sys, time
from pathlib import Path
import httpx

EDGE = sys.argv[1].rstrip("/")
os.environ["HERALD_EDGE_URL"] = EDGE
os.environ["HERALD_EDGE_SERVICE_KEY"] = os.environ.get("HERALD_EDGE_SERVICE_KEY", "")
sys.path.insert(0, "/home/mohit/projects/herald-relay/backend")
sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")
from herald_relay.edge_tunnel import EdgeTunnel  # noqa: E402
from main import app  # noqa: E402

KEY = next(l.split("=", 1)[1].strip().strip('"') for l in Path.home().joinpath(".hermes/.env").read_text().splitlines()
           if l.startswith("API_SERVER_KEY="))
res = []
def check(n, ok, d=""):
    res.append(bool(ok)); print(("PASS " if ok else "FAIL ") + n + (f"  [{d}]" if d else ""), flush=True)

async def main():
    token = "edgetest-" + secrets.token_hex(16)
    tun = EdgeTunnel(EDGE, token, "http://127.0.0.1:8642")
    t = asyncio.create_task(tun.run_forever())
    await asyncio.wait_for(tun.connected.wait(), 15)
    H = {"Authorization": f"Bearer {KEY}"}
    base = f"/hermes/d/{token}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay", timeout=300) as c:
        r = await c.get(f"{base}/health", headers=H)
        check("health via relay->edge->Hermes", r.status_code == 200 and "hermes-agent" in r.text, r.text[:100])
        r = await c.get(f"{base}/v1/models", headers=H)
        check("/v1/models via edge", r.status_code == 200 and "data" in r.json(), str(r.status_code))
        r = await c.get(f"{base}/api/soul", headers=H)
        check("/api/soul via edge", r.status_code in (200, 404), f"{r.status_code} {r.text[:80]}")
        r = await c.get(f"{base}/v1/models", headers={"Authorization": "Bearer wrong"})
        check("bad key still rejected by Hermes (401)", r.status_code == 401, str(r.status_code))
        r = await c.get("/hermes/health/detailed", headers={**H, "X-Device-Token": token})
        check("health/detailed reports edge transport", r.json().get("transport") == "edge", r.text[:120])

        t0 = time.time()
        r = await c.post(f"{base}/v1/runs", headers=H, json={"input": "Run `hostname` in the terminal and reply with just the hostname."})
        body = r.json(); run_id = body.get("run_id")
        check("start run via edge", r.status_code in (200, 202) and run_id, json.dumps(body)[:120])
        last_id, n1 = None, 0
        async with c.stream("GET", f"{base}/v1/runs/{run_id}/events", headers=H) as s:
            async for line in s.aiter_lines():
                if line.startswith("id:"): last_id = line[3:].strip()
                if line.startswith("data:"):
                    n1 += 1
                    if n1 >= 2: break
        check("SSE keeps Hermes id: lines (needed for resume)", last_id is not None, f"last_id={last_id}")
        await asyncio.sleep(5)
        final, n2 = None, 0
        async with c.stream("GET", f"{base}/v1/runs/{run_id}/events", headers={**H, "Last-Event-ID": last_id or "0"}) as s:
            async for line in s.aiter_lines():
                if line.startswith("data:"):
                    n2 += 1
                    try: ev = json.loads(line[5:])
                    except ValueError: continue
                    if ev.get("event") in ("run.completed", "run.failed"): final = ev
        check("dropped + resumed run completes", bool(final) and final.get("event") == "run.completed",
              f"{n2} events after resume; output={(final or {}).get('output', '')[:60]!r}; {time.time()-t0:.1f}s")

    await tun.stop(); t.cancel()
    await asyncio.sleep(1.5)
    from services import edge_client; edge_client.forget(token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as c:
        r = await c.get(f"{base}/health", headers=H)
        check("device gone -> 503 device_offline", r.status_code == 503, f"{r.status_code} {r.text[:80]}")
    print(f"\n{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)

asyncio.run(main())
