"""Agent name and skills survive `hermes update` (herald#490).

Local Hermes is a tiny real HTTP server that can behave like STOCK Hermes
(no /api/soul -> 404, /v1/skills -> 500 TypeError) or like a patched one
(both 200). Hermes' interpreter is either a stand-in script or none at all,
so the disk fallback is exercised too.
"""
import json
import sys
import textwrap
import types
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import agent_info, agent_info_helper, edge_tunnel  # noqa: E402

KEYS = {"": "default-key", "/p/work": "work-key"}
SOUL = "You are **Diablo**, Harman's agent.\nBe brief."
WORK_SOUL = "You are Asla."


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "skills/github/github-pr").mkdir(parents=True)
    (h / "skills/github/github-pr/SKILL.md").write_text(
        "---\nname: github-pr\ndescription: |\n  Open and merge PRs.\n---\nbody\n")
    (h / "skills/plan").mkdir(parents=True)
    (h / "skills/plan/SKILL.md").write_text("---\nname: plan\ndescription: Write a plan.\n---\n")
    (h / "skills/.archive/old").mkdir(parents=True)
    (h / "skills/.archive/old/SKILL.md").write_text("---\nname: old\n---\n")
    (h / "SOUL.md").write_text(SOUL)
    w = h / "profiles/work"
    (w / "skills/notes").mkdir(parents=True)
    (w / "skills/notes/SKILL.md").write_text("---\nname: notes\ndescription: Take notes.\n---\n")
    (w / "SOUL.md").write_text(WORK_SOUL)
    return h


def make_hermes(mode):
    """mode: 'stock' | 'patched' | 'garbage'."""
    async def ok(request):
        prefix = "/p/" + request.match_info["p"] if "p" in request.match_info else ""
        return request.headers.get("Authorization") == f"Bearer {KEYS.get(prefix)}"

    async def models(request):
        return web.json_response({"data": []}, status=200 if await ok(request) else 401)

    async def skills(request):
        if not await ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if mode == "patched":
            return web.json_response({"object": "list", "data": [{"name": "from-hermes"}]})
        if mode == "garbage":
            return web.Response(text="<html>oops</html>", status=200)
        return web.json_response({"error": {"message": "Failed to enumerate skills"}}, status=500)

    async def soul(request):
        if not await ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"object": "hermes.soul", "soul": "You are Patched."})

    app = web.Application()
    for prefix in ("", "/p/{p}"):
        app.router.add_get(f"{prefix}/v1/models", models)
        app.router.add_get(f"{prefix}/v1/skills", skills)
        if mode == "patched":
            app.router.add_get(f"{prefix}/api/soul", soul)
    return app


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@pytest.fixture
async def stock():
    runner, url = await serve(make_hermes("stock"))
    yield url
    await runner.cleanup()


@pytest.fixture
async def patched():
    runner, url = await serve(make_hermes("patched"))
    yield url
    await runner.cleanup()


@pytest.fixture
def fake_hermes_python(tmp_path, monkeypatch):
    """Stands in for agent_info_helper.py run by Hermes: answers from HERMES_HOME."""
    script = tmp_path / "helper.py"
    script.write_text(textwrap.dedent("""
        import json, os, sys
        job = json.loads(sys.stdin.read())
        home = os.environ["HERMES_HOME"]
        if job["op"] == "soul":
            print(json.dumps({"soul": open(os.path.join(home, "SOUL.md")).read() + " (via hermes)"}))
        else:
            print(json.dumps({"data": [{"name": "via-hermes", "description": home, "category": None}]}))
    """))
    monkeypatch.setattr(agent_info, "_HELPER", script)
    return sys.executable


def call(url, home, path, key="default-key", py=""):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return agent_info.handle("GET", path, headers, None, hermes_url=url,
                             hermes_home=home, hermes_python=py)


def test_only_our_paths_are_taken():
    for p in ("/api/soul", "/v1/skills", "/p/work/api/soul", "/p/work/v1/skills/", "/v1/skills?x=1"):
        assert agent_info.matches(p), p
    for p in ("/v1/skills/foo", "/api/soul/x", "/v1/models", "/p/../api/soul", "/p//v1/skills", ""):
        assert not agent_info.matches(p), p


async def test_stock_hermes_still_gets_the_agents_name(stock, home):
    """The bug: stock Hermes 404s /api/soul and the app showed 'default'."""
    status, body = await call(stock, home, "/api/soul")
    assert status == 200
    assert body["soul"] == SOUL and body["source"] == "herald-relay"
    assert body["chars"] == len(SOUL)


async def test_stock_hermes_still_lists_skills(stock, home):
    """The bug: stock Hermes 500s /v1/skills (include_editorial TypeError)."""
    status, body = await call(stock, home, "/v1/skills")
    assert status == 200 and body["object"] == "list"
    assert [(s["name"], s["category"]) for s in body["data"]] == [("plan", None), ("github-pr", "github")]
    assert body["data"][1]["description"] == "Open and merge PRs."


async def test_named_profile_uses_its_own_home_and_key(stock, home):
    status, body = await call(stock, home, "/p/work/api/soul", key="work-key")
    assert status == 200 and body["soul"] == WORK_SOUL and body["profile"] == "work"
    status, body = await call(stock, home, "/p/work/v1/skills", key="work-key")
    assert [s["name"] for s in body["data"]] == ["notes"]


@pytest.mark.parametrize("path,key", [
    ("/api/soul", None), ("/api/soul", "wrong"), ("/v1/skills", "work-key"),
    ("/p/work/api/soul", "default-key"), ("/p/work/v1/skills", None)])
async def test_no_key_or_another_profiles_key_gets_nothing(stock, home, path, key):
    status, body = await call(stock, home, path, key=key)
    assert status == 401
    assert "soul" not in body and "data" not in body


async def test_unknown_profile_is_not_found(stock, home):
    KEYS["/p/ghost"] = "ghost-key"
    try:
        status, _ = await call(stock, home, "/p/ghost/api/soul", key="ghost-key")
    finally:
        KEYS.pop("/p/ghost")
    assert status == 404


async def test_a_working_hermes_is_the_authority(patched, home, fake_hermes_python):
    """Patched (or a future fixed) Hermes: pass its answer through untouched."""
    status, body = await call(patched, home, "/api/soul", py=fake_hermes_python)
    assert (status, body) == (200, {"object": "hermes.soul", "soul": "You are Patched."})
    status, body = await call(patched, home, "/v1/skills", py=fake_hermes_python)
    assert body == {"object": "list", "data": [{"name": "from-hermes"}]}


async def test_hermes_401_is_passed_through_not_papered_over(patched, home):
    status, body = await call(patched, home, "/v1/skills", key="wrong")
    assert status == 401 and "data" not in body


async def test_hermes_own_code_is_preferred_over_reading_files(stock, home, fake_hermes_python):
    status, body = await call(stock, home, "/api/soul", py=fake_hermes_python)
    assert body["soul"] == SOUL + " (via hermes)"
    status, body = await call(stock, home, "/p/work/v1/skills", key="work-key", py=fake_hermes_python)
    assert body["data"] == [{"name": "via-hermes", "description": str(home / "profiles/work"),
                             "category": None}]


async def test_a_broken_helper_falls_back_to_the_files(stock, home, tmp_path, monkeypatch):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys; print('{\"error\": \"hermes_too_old\"}')")
    monkeypatch.setattr(agent_info, "_HELPER", bad)
    status, body = await call(stock, home, "/v1/skills", py=sys.executable)
    assert status == 200 and {s["name"] for s in body["data"]} == {"plan", "github-pr"}


async def test_garbage_from_hermes_is_not_passed_on(home):
    runner, url = await serve(make_hermes("garbage"))
    try:
        status, body = await call(url, home, "/v1/skills")
    finally:
        await runner.cleanup()
    assert status == 200 and body["source"] == "herald-relay"


async def test_hermes_down_is_502_not_401(home):
    status, body = await call("http://127.0.0.1:9", home, "/api/soul")
    assert status == 502 and body["error"] == "hermes_unreachable"


async def test_no_soul_file_is_null_not_an_error(stock, home):
    (home / "SOUL.md").unlink()
    status, body = await call(stock, home, "/api/soul")
    assert status == 200 and body["soul"] is None and body["chars"] == 0


async def test_post_is_refused(stock, home):
    status, _ = await agent_info.handle("POST", "/v1/skills", {}, None, hermes_url=stock,
                                        hermes_home=home, hermes_python="")
    assert status == 405


async def test_edge_transport_answers_from_the_tunnel(stock, home):
    """Over the edge socket the request reaches the app-request hook, never raw Hermes."""
    sent = []

    class Ws:
        closed = False
        async def send_str(self, s):
            sent.append(json.loads(s))

    async def on_screen(method, path, headers, body):
        return await agent_info.handle(method, path, headers, body, hermes_url=stock,
                                       hermes_home=home, hermes_python="")

    t = edge_tunnel.EdgeTunnel("https://edge.example", "tok", stock, hermes_key="default-key")
    t.on_screen = on_screen
    await t._serve(Ws(), {"id": "1", "method": "GET", "path": "/api/soul",
                          "headers": {"Authorization": "Bearer default-key"}})
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["data"])["soul"] == SOUL
    assert sent[-1] == {"t": "end", "id": "1"}


# ── the helper, against stand-ins for Hermes' modules ──────────────────────

def _fake_hermes(monkeypatch, find_all_skills, soul=None):
    tools = types.ModuleType("tools")
    st = types.ModuleType("tools.skills_tool")
    st.find_all_skills = find_all_skills
    st.sort_skills = lambda s: sorted(s, key=lambda x: x["name"])
    tools.skills_tool = st
    agent = types.ModuleType("agent")
    pb = types.ModuleType("agent.prompt_builder")
    pb.load_soul_md = lambda: soul
    agent.prompt_builder = pb
    for name, mod in (("tools", tools), ("tools.skills_tool", st),
                      ("agent", agent), ("agent.prompt_builder", pb)):
        monkeypatch.setitem(sys.modules, name, mod)


def test_helper_calls_current_hermes_without_include_editorial(monkeypatch):
    """Current Hermes: find_all_skills(*, skip_disabled) — passing
    include_editorial is exactly the TypeError that breaks /v1/skills."""
    def find_all_skills(*, skip_disabled=False):
        return [{"name": "b", "description": "B", "category": "x"}, {"name": "a", "category": None}]
    _fake_hermes(monkeypatch, find_all_skills)
    assert [s["name"] for s in agent_info_helper.skills()["data"]] == ["a", "b"]


def test_helper_calls_older_hermes_with_include_editorial(monkeypatch):
    def find_all_skills(*, skip_disabled, include_editorial):
        assert include_editorial is True and skip_disabled is False
        return [{"name": "a"}]
    _fake_hermes(monkeypatch, find_all_skills)
    assert agent_info_helper.skills()["data"] == [{"name": "a", "description": "", "category": None}]


def test_helper_soul(monkeypatch):
    _fake_hermes(monkeypatch, lambda: [], soul="You are Tai.")
    assert agent_info_helper.soul() == {"soul": "You are Tai."}
    _fake_hermes(monkeypatch, lambda: [], soul="  ")
    assert agent_info_helper.soul() == {"soul": None}


async def test_sse_transport_answers_from_the_tunnel(stock, home, monkeypatch):
    """The older SSE tunnel routes the same paths through the tunnel, not raw Hermes."""
    from herald_relay import relay_client, tunnel_cli
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", home)
    monkeypatch.setattr(tunnel_cli, "_hermes_python", lambda: "")
    posted = []

    class Client:
        async def post(self, url, json=None, **kw):
            posted.append((url, json))

    c = relay_client.HeraldRelayClient("https://relay.example", "tok", stock, hermes_key="default-key")
    await c._handle_forward_request(Client(), {
        "request_id": "r1", "method": "GET", "path": "/p/work/v1/skills",
        "headers": {"Authorization": "Bearer work-key"}})
    url, reply = posted[0]
    assert url.endswith("/tunnel/http_response") and reply["status"] == 200
    assert [s["name"] for s in reply["body"]["data"]] == ["notes"]
