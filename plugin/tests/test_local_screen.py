"""The app asking for this computer's screen: who may ask, and what comes back.

The real pieces (Hermes' Bot Screen, the relay pipe) are exercised by the live
check; here Hermes' interpreter is a stand-in script, and local Hermes is a
tiny real HTTP server that knows one key per profile.
"""
import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import edge_tunnel, local_screen  # noqa: E402

KEYS = {"": "default-key", "/p/work": "work-key"}


@pytest.fixture
async def hermes():
    async def models(request):
        prefix = "/p/" + request.match_info["p"] if "p" in request.match_info else ""
        ok = request.headers.get("Authorization") == f"Bearer {KEYS.get(prefix)}"
        return web.json_response({"data": []}, status=200 if ok else 401)
    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_get("/p/{p}/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.fixture
async def fake_python(tmp_path, monkeypatch):
    """Stands in for screen_pipe.py: records each job, answers from a state file."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"supported": True, "installed": True, "running": True, "pipe": "ready"}))
    jobs = tmp_path / "jobs.jsonl"
    script = tmp_path / "pipe.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        job = json.loads(sys.stdin.read())
        job["home"] = os.environ.get("HERMES_HOME")
        open({str(jobs)!r}, "a").write(json.dumps(job) + "\\n")
        st = json.loads(open({str(state)!r}).read())
        if job["op"] == "pipe":
            print(st["pipe"], flush=True)
            if st["pipe"] == "ready":
                time.sleep(30)
        else:
            if job["op"] == "start":
                st["running"] = True
                open({str(state)!r}, "w").write(json.dumps(st))
            print(json.dumps({{k: st.get(k) for k in ("supported", "installed", "running", "error") if k in st}}), flush=True)
    """))
    monkeypatch.setattr(local_screen, "_PIPE", script)
    for d in (local_screen._pipes, local_screen._control, local_screen._mints):
        d.clear()
    yield state, jobs
    for procs in local_screen._pipes.values():
        for p in procs:
            if p.returncode is None:
                p.kill()
            await p.wait()


def call(hermes, tmp_path, method, path, key="default-key", body=None):
    (tmp_path / "home/profiles/work").mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return local_screen.handle(method, path, headers, body, hermes_url=hermes,
                               relay_url="https://relay.example", device_token="tok-0123456789abcdef",
                               hermes_home=tmp_path / "home", hermes_python=sys.executable)


def jobs_of(jobs):
    return [json.loads(l) for l in jobs.read_text().splitlines()] if jobs.exists() else []


def test_only_our_paths_are_taken():
    assert local_screen.matches("/_herald/screen")
    assert local_screen.matches("/p/work/_herald/screen/enable")
    assert not local_screen.matches("/v1/runs")
    assert not local_screen.matches("/p/work/v1/_herald/screen")
    assert not local_screen.matches("/p/../_herald/screen")


async def test_status_for_the_key_holder(hermes, tmp_path, fake_python):
    status, body = await call(hermes, tmp_path, "GET", "/_herald/screen")
    assert (status, body) == (200, {"available": True, "enabled": True, "enabling": False, "failed": False})


@pytest.mark.parametrize("key", [None, "", "wrong", "work-key"])
async def test_no_key_or_another_profiles_key_gets_nothing(hermes, tmp_path, fake_python, key):
    for method, path in (("GET", "/_herald/screen"), ("POST", "/_herald/screen"),
                         ("POST", "/_herald/screen/enable")):
        status, body = await call(hermes, tmp_path, method, path, key=key, body={"mode": "control"})
        assert status == 401 and body["error"] == "agent_key_required"
    assert jobs_of(fake_python[1]) == []          # nothing was even looked at


async def test_a_named_profile_uses_its_own_key_and_home(hermes, tmp_path, fake_python):
    status, _ = await call(hermes, tmp_path, "GET", "/p/work/_herald/screen", key="work-key")
    assert status == 200
    assert jobs_of(fake_python[1])[-1]["home"] == str(tmp_path / "home/profiles/work")
    status, _ = await call(hermes, tmp_path, "GET", "/p/work/_herald/screen", key="default-key")
    assert status == 401


async def test_a_link_goes_out_through_the_relay_and_is_never_reused(hermes, tmp_path, fake_python):
    s1, a = await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"})
    s2, b = await call(hermes, tmp_path, "POST", "/_herald/screen", body='{"mode": "watch"}')
    assert s1 == s2 == 200 and a["mode"] == "watch" and a["expires_in"] == 60
    assert a["ws_url"].startswith("wss://relay.example/hermes/d/tok-0123456789abcdef/_screen/")
    assert a["ws_url"] != b["ws_url"]
    job = [j for j in jobs_of(fake_python[1]) if j["op"] == "pipe"][0]
    sid = a["ws_url"].rsplit("/", 1)[1]
    assert len(sid) >= 43
    assert job["relay_ws"] == f"wss://relay.example/tunnel/screen/{sid}"
    assert job["mode"] == "watch" and job["release"] == []


async def test_hand_back_releases_the_control_the_phone_took(hermes, tmp_path, fake_python):
    await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "control"})
    await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"})
    await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"})
    pipes = [j for j in jobs_of(fake_python[1]) if j["op"] == "pipe"]
    assert pipes[1]["release"] == [pipes[0]["viewer_id"]]
    assert pipes[2]["release"] == []


async def test_screen_off_then_turned_on(hermes, tmp_path, fake_python):
    state, _ = fake_python
    state.write_text(json.dumps({"supported": True, "installed": True, "running": False, "pipe": "ready"}))
    status, body = await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"})
    assert status == 409 and body["error"] == "screen_not_enabled"
    status, body = await call(hermes, tmp_path, "POST", "/_herald/screen/enable")
    assert status == 200 and body["enabled"] is True


@pytest.mark.parametrize("st,reason", [
    ({"error": "hermes_too_old"}, "hermes_too_old"),
    ({"supported": False, "installed": False, "running": False}, "unsupported_os"),
    ({"supported": True, "installed": False, "running": False}, "not_installed"),
])
async def test_why_a_computer_cannot_show_a_screen(hermes, tmp_path, fake_python, st, reason):
    fake_python[0].write_text(json.dumps(st))
    status, body = await call(hermes, tmp_path, "GET", "/_herald/screen")
    assert status == 200 and body["available"] is False and body["reason"] == reason


async def test_bad_mode_and_rate_limit(hermes, tmp_path, fake_python, monkeypatch):
    status, body = await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "admin"})
    assert status == 422
    monkeypatch.setattr(local_screen, "MINTS_PER_10_MIN", 2)
    for _ in range(2):
        assert (await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"}))[0] == 200
    assert (await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"}))[0] == 429


async def test_old_helpers_are_stopped_not_piled_up(hermes, tmp_path, fake_python):
    for _ in range(6):
        await call(hermes, tmp_path, "POST", "/_herald/screen", body={"mode": "watch"})
    await asyncio.sleep(0.2)
    live = [p for p in local_screen._pipes[str(tmp_path / "home")] if p.returncode is None]
    assert len(live) == local_screen.MAX_PIPES_PER_PROFILE


async def test_the_edge_transport_never_lends_the_machines_key(hermes, tmp_path, fake_python):
    """A request with no key must reach the handler with no key, even though
    the tunnel knows this machine's own."""
    seen, sent = [], []

    async def on_screen(method, path, headers, body):
        seen.append(headers)
        return 401, {"error": "agent_key_required"}

    class Ws:
        closed = False
        async def send_str(self, s):
            sent.append(json.loads(s))

    t = edge_tunnel.EdgeTunnel("https://edge.example", "tok", hermes, hermes_key="default-key")
    t.on_screen = on_screen
    await t._serve(Ws(), {"id": "1", "method": "GET", "path": "/_herald/screen", "headers": {}})
    assert seen == [{}]
    assert sent[0]["status"] == 401 and sent[-1] == {"t": "end", "id": "1"}
