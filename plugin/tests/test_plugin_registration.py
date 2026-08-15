"""Tests for the Hermes plugin registration contract.

These lock down the bug that made herald-relay silently never load: Hermes
imports the entry-point target and calls a module-level ``register(ctx)``. A
plugin without one is discovered, listed as "enabled", and then dropped with
``error="no register() function"``.
"""
import asyncio

import pytest

from herald_relay import plugin as plugin_mod
from herald_relay.plugin import HeraldRelayPlugin, register


class FakeCtx:
    """Minimal stand-in for Hermes' PluginContext."""

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)


@pytest.fixture(autouse=True)
def _reset_plugin_singleton():
    plugin_mod._PLUGIN = None
    yield
    plugin_mod._PLUGIN = None


def test_register_is_module_level_callable():
    """Hermes does getattr(module, 'register') — it must exist and be callable."""
    assert callable(register)


def test_register_binds_session_lifecycle_hooks():
    ctx = FakeCtx()
    register(ctx)
    assert "on_session_start" in ctx.hooks
    assert "on_session_end" in ctx.hooks


def test_register_uses_only_valid_hermes_hooks():
    """Guard against typos — Hermes warns and ignores unknown hook names."""
    valid = {
        "pre_tool_call", "post_tool_call", "transform_terminal_output",
        "transform_tool_result", "transform_llm_output", "pre_llm_call",
        "post_llm_call", "pre_verify", "pre_api_request", "post_api_request",
        "api_request_error", "on_session_start", "on_session_end",
        "on_session_finalize", "on_session_reset", "subagent_start",
        "subagent_stop", "pre_gateway_dispatch", "pre_approval_request",
        "post_approval_response", "kanban_task_claimed",
        "kanban_task_completed", "kanban_task_blocked",
    }
    ctx = FakeCtx()
    register(ctx)
    assert set(ctx.hooks) <= valid


def test_register_constructs_plugin_singleton():
    ctx = FakeCtx()
    register(ctx)
    assert isinstance(plugin_mod._PLUGIN, HeraldRelayPlugin)


def test_register_does_not_start_tunnel_eagerly():
    """register() is sync and may run with no event loop — must not spawn a task."""
    ctx = FakeCtx()
    register(ctx)
    assert plugin_mod._PLUGIN._task is None


def test_session_start_without_token_is_a_noop(monkeypatch, caplog):
    """No device token yet (user hasn't registered the device) -> warn, don't crash."""
    monkeypatch.setattr(plugin_mod, "_load_plugin_config", lambda: {"relay_url": "http://x"})
    monkeypatch.delenv("HERALD_DEVICE_TOKEN", raising=False)
    ctx = FakeCtx()
    register(ctx)
    assert plugin_mod._PLUGIN.device_token is None

    asyncio.run(ctx.hooks["on_session_start"][0]())
    assert plugin_mod._PLUGIN._task is None


def test_session_start_starts_tunnel_when_token_present(monkeypatch):
    monkeypatch.setattr(
        plugin_mod,
        "_load_plugin_config",
        lambda: {"relay_url": "http://relay.test", "device_token": "tok-123"},
    )
    ctx = FakeCtx()
    register(ctx)

    started = {}

    async def fake_start(self):
        started["yes"] = True
        self._task = asyncio.current_task()

    monkeypatch.setattr(HeraldRelayPlugin, "start", fake_start)
    asyncio.run(ctx.hooks["on_session_start"][0]())
    assert started.get("yes") is True


def test_session_start_is_idempotent(monkeypatch):
    """Two sessions must not spawn two tunnels."""
    monkeypatch.setattr(
        plugin_mod,
        "_load_plugin_config",
        lambda: {"relay_url": "http://relay.test", "device_token": "tok-123"},
    )
    ctx = FakeCtx()
    register(ctx)

    calls = []

    async def fake_start(self):
        calls.append(1)

    monkeypatch.setattr(HeraldRelayPlugin, "start", fake_start)

    async def drive():
        hook = ctx.hooks["on_session_start"][0]
        await hook()
        # Simulate a live tunnel task so the guard trips.
        fut = asyncio.get_running_loop().create_future()
        plugin_mod._PLUGIN._task = asyncio.ensure_future(asyncio.sleep(0.05))
        await hook()
        plugin_mod._PLUGIN._task.cancel()
        fut.cancel()

    asyncio.run(drive())
    assert len(calls) == 1


def test_config_reader_tolerates_missing_config(monkeypatch):
    """No hermes_cli importable (standalone tests) -> empty dict, not an exception."""
    assert isinstance(plugin_mod._load_plugin_config(), dict)
