"""One viewing of this profile's Bot Screen from the Herald app.

Run by the tunnel client with HERMES' OWN interpreter (not the tunnel's), as a
script, one process per viewing:

    HERMES_HOME=<profile home>  <hermes python>  screen_pipe.py   < job.json

so that the two things that decide who may touch the screen are Hermes' own
code, not a copy of it: `tools.bot_desktop.lease` (who holds control; the
agent's screen tools refuse while a person does) and
`tools.bot_desktop.rfb_filter` (drops mouse, keyboard and clipboard messages
from anyone who doesn't hold it).

It joins the screen's private socket (`<HERMES_HOME>/bot-desktop/rfb.sock`) to
an OUTBOUND WebSocket on Herald's relay, where the phone picks it up. Nothing
listens on this computer.

The job arrives on stdin, never argv (argv is visible to every local user):
    {"op": "pipe", "relay_ws": "wss://…/tunnel/screen/<sid>", "device_token": "…",
     "viewer_id": "…", "mode": "watch"|"control", "release": ["<older viewer id>", …]}
    {"op": "status"}     -> one JSON line: supported / installed / running
    {"op": "start"}      -> starts the screen (never installs), then as status

`pipe` prints exactly one line: `ready` once the relay socket is open, or
`error <code>`. Nothing secret is ever printed or logged.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

READ_CHUNK = 64 * 1024
LEASE_REFRESH_S = 0.25
ACTIVITY_STAMP_S = 60.0
# A phone drops its link far more often than a desk does (lift, tunnel, lock
# screen). The app reconnects by itself within seconds, as a NEW viewer that
# takes control again; only when nobody came back is control returned, so the
# agent is never locked out by a phone that went away.
RELEASE_GRACE_S = 45.0


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def status(start: bool = False) -> int:
    try:
        from tools.bot_desktop import runtime
    except Exception:  # noqa: BLE001
        say(json.dumps({"error": "hermes_too_old"}))
        return 0
    out: dict = {}
    if start:
        try:
            runtime.start()
        except Exception as exc:  # noqa: BLE001  the message names the blocker in plain words
            out["start_error"] = str(exc)[:300]
    st = runtime.status()
    out.update(supported=bool(st.supported), installed=bool(st.installed),
               running=bool(st.running), blocker=st.blocker)
    say(json.dumps(out))
    return 0


async def run(job: dict) -> int:
    try:
        import aiohttp
        from hermes_constants import get_hermes_home
        from tools.bot_desktop import lease
        from tools.bot_desktop.rfb_filter import RfbClientFilter
    except Exception:  # noqa: BLE001  a Hermes without Bot Screen
        say("error hermes_too_old")
        return 2

    home = Path(get_hermes_home())
    sock = home / "bot-desktop" / "rfb.sock"
    viewer = str(job["viewer_id"])
    # Control taken by an earlier link from the same phone: hand it back now
    # (the user tapped Hand back, or is re-taking with this link).
    for old in job.get("release") or []:
        lease.release(str(old))
    control = job.get("mode") == "control"
    if not sock.exists():
        say("error not_running")
        return 3
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except OSError:
        say("error not_running")
        return 3

    loop = asyncio.get_running_loop()
    allowed = {"input": False, "at": 0.0}

    def may_send_input() -> bool:
        if loop.time() - allowed["at"] > LEASE_REFRESH_S:
            allowed["input"] = lease.viewer_may_send_input(viewer)
            allowed["at"] = loop.time()
        return allowed["input"]

    rfb = RfbClientFilter(may_send_input)
    activity = home / "bot-desktop" / "activity"
    stamped = {"at": 0.0}
    took = False

    def stamp() -> None:
        if loop.time() - stamped["at"] < ACTIVITY_STAMP_S:
            return
        stamped["at"] = loop.time()
        try:
            activity.touch()      # someone watching is use: the idle auto-stop must wait
        except OSError:
            pass

    session = aiohttp.ClientSession()
    try:
        try:
            ws = await session.ws_connect(
                job["relay_ws"],
                headers={"Authorization": f"Bearer {job['device_token']}"},
                heartbeat=20, max_msg_size=4 * 1024 * 1024)
        except Exception:  # noqa: BLE001
            say("error relay_unreachable")
            return 4
        if control:
            lease.acquire(viewer, reason="Herald app")
            took = True
        say("ready")

        async def screen_to_phone() -> None:
            while True:
                chunk = await reader.read(READ_CHUNK)
                if not chunk:
                    return
                stamp()
                await ws.send_bytes(chunk)

        async def phone_to_screen() -> None:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.BINARY:
                    return
                try:
                    out = rfb.feed(msg.data)
                except ValueError:
                    return            # a message we can't frame: end the viewing
                if out:
                    writer.write(out)
                    await writer.drain()

        async def evicted() -> None:
            # Someone else took control (Hermes Desktop, another phone), or the
            # user handed back from elsewhere: this viewing is over.
            while True:
                await asyncio.sleep(1.0)
                if control and not lease.viewer_may_send_input(viewer):
                    return

        tasks = [asyncio.create_task(c()) for c in (screen_to_phone, phone_to_screen, evicted)]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clean = ws.close_code in (1000, 1001)
        await ws.close()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        await session.close()

    if took:
        if not clean:
            deadline = time.monotonic() + RELEASE_GRACE_S
            while time.monotonic() < deadline and lease.viewer_may_send_input(viewer):
                await asyncio.sleep(1.0)
        # Only if it is still ours: a viewer that took over after us keeps it.
        lease.release(viewer)
    return 0


def main() -> int:
    try:
        job = json.loads(sys.stdin.read())
    except ValueError:
        say("error bad_job")
        return 1
    op = job.get("op") or "pipe"
    if op in ("status", "start"):
        return status(start=op == "start")
    return asyncio.run(run(job))


if __name__ == "__main__":
    raise SystemExit(main())
