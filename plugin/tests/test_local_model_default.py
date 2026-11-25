"""Only the paired profile can read or write its Hermes default model."""

from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from herald_relay import is_local_door, local_model_default as door, model_default_helper, tunnel_cli
from herald_relay.relay_client import HeraldRelayClient
from .test_local_screen import hermes  # noqa: F401


@pytest.fixture
def app(tmp_path, monkeypatch, hermes):  # noqa: F811
    home = tmp_path / "hermes"
    (home / "profiles/work").mkdir(parents=True)
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", home)
    monkeypatch.setattr(tunnel_cli, "_hermes_command", lambda: ["python"])
    relay = HeraldRelayClient("https://relay.example", "token", hermes)
    return relay, home


@pytest.mark.asyncio
async def test_model_default_door_authenticates_and_pins_the_named_profile(app, monkeypatch):
    relay, home = app
    mock = AsyncMock(return_value={"can_set_default": True, "model": "m2", "provider": "cp"})
    monkeypatch.setattr(door, "_run", mock)
    path = "/p/work/_herald/model-default"
    assert is_local_door(path)
    assert (await relay.screen_request("GET", path, {"Authorization": "Bearer default-key"}, None))[0] == 401
    mock.assert_not_awaited()
    auth = {"Authorization": "Bearer work-key"}
    assert (await relay.screen_request("GET", path, auth, None))[1]["supported"] is True
    mock.assert_awaited_with(["python"], home / "profiles/work", {"action": "get"})
    assert await relay.screen_request("PUT", path, auth, {"model": "m2", "provider": "cp"}) == (
        200, {"model": "m2", "provider": "cp"})
    mock.assert_awaited_with(["python"], home / "profiles/work", {
        "action": "set", "model": "m2", "provider": "cp",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"model": "m2", "base_url": "new-url"}, {"model": "m2", "api_key": "user-input"},
    {"model": "m2", "profile": "another"}, {"model": "m2", "model_options": {"service_tier": "priority"}},
    {"model": "not valid"}, {"model": "m2", "provider": None},
])
async def test_model_default_door_refuses_every_non_model_input(app, monkeypatch, payload):
    relay, _ = app
    mock = AsyncMock()
    monkeypatch.setattr(door, "_run", mock)
    status, _ = await relay.screen_request(
        "PUT", "/p/work/_herald/model-default", {"Authorization": "Bearer work-key"}, payload)
    assert status == 422
    mock.assert_not_awaited()


@pytest.fixture
def hermes_runtime(monkeypatch):
    import sys

    # The helper runs in Hermes's interpreter, not Bridge's. Model-setting
    # tests exercise its contract without installing or mutating a real agent.
    package = ModuleType("hermes_cli")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    ctx = SimpleNamespace(current_model="m1", current_provider="cp")
    recorded = []

    def persist(model_selection):
        recorded.append(model_selection)
        ctx.current_provider, ctx.current_model = model_selection

    modules = {
        "config": {"load_config": lambda: {"model": {"provider": "cp", "default": "m1"}}},
        "config_backend": {"ConfigLockedError": type("Locked", (Exception,), {})},
        "inventory": {
            "load_picker_context": lambda: ctx,
            "build_model_options_payload": lambda _, *, include_unconfigured: {
                "providers": [{"slug": "cp", "models": ["m1", "m2"]}],
            },
        },
        "model_assignment": {
            "ModelAssignmentRejected": type("Rejected", (Exception,), {}),
            "BARE_CUSTOM_SELECTORS": frozenset({"custom", "local"}),
            "validated_main_model_selection": lambda cfg, provider, model: (provider, model),
        },
        "model_switch": {"persist_model_selection": persist},
    }
    for name, values in modules.items():
        m = ModuleType(f"hermes_cli.{name}")
        m.__dict__.update(values)
        monkeypatch.setitem(sys.modules, m.__name__, m)
    return ctx, recorded


def test_helper_only_writes_a_configured_choice_through_hermes(hermes_runtime):
    ctx, recorded = hermes_runtime
    assert model_default_helper.run({"action": "get"}) == {
        "can_set_default": True, "model": "m1", "provider": "cp",
    }
    assert model_default_helper.run({"action": "set", "model": "unadvertised", "provider": "cp"}) == {
        "error": "invalid_model",
    }
    assert model_default_helper.run({"action": "set", "model": "m2", "provider": "other"}) == {
        "error": "invalid_model",
    }
    assert not recorded and ctx.current_model == "m1"
    assert model_default_helper.run({"action": "set", "model": "m2", "provider": "cp"}) == {
        "can_set_default": True, "model": "m2", "provider": "cp",
    }
    assert recorded == [("cp", "m2")]


def test_helper_keeps_the_installed_bare_custom_endpoint(hermes_runtime, monkeypatch):
    import sys

    ctx, recorded = hermes_runtime
    ctx.current_provider = "custom"
    inventory = sys.modules["hermes_cli.inventory"]
    inventory.build_model_options_payload = lambda _, *, include_unconfigured: {
        "providers": [{"slug": "custom", "models": ["m1", "m2"]}],
    }
    cfg = {"model": {"provider": "custom", "default": "m1",
                     "base_url": "https://configured.example/v1", "api_key": "fixture"}}
    sys.modules["hermes_cli.config"].load_config = lambda: cfg
    validator = sys.modules["hermes_cli.model_assignment"]

    def validate(config, provider, model, **trusted):
        assert config is cfg
        assert trusted == {
            "base_url": "https://configured.example/v1", "api_key": "fixture",
            "explicit_endpoint": False,
        }
        return provider, model

    monkeypatch.setattr(validator, "validated_main_model_selection", validate)
    result = model_default_helper.run({"action": "set", "model": "m2", "provider": "custom"})
    assert result == {"can_set_default": True, "model": "m2", "provider": "custom"}
    assert recorded == [("custom", "m2")]
