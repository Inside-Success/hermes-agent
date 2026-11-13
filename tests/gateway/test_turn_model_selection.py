"""Offline effective-config contracts through real normal/background gateway paths."""
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.turn_model_selection import select_turn_model
from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_compression_failure_session_sync import (
    _runner, _SessionStore, _install_compression_failure_agent,
    _run_compression_failure_turn,
)
from tests.gateway.test_background_command import _make_runner


def policy(model="target"):
    return dict(policy_revision="test-v1", operation="unrestricted_turn",
                reason="source quality evidence", model=model, provider="openrouter")


@pytest.mark.parametrize("invalid", [False, {}, {**policy(), "model": ""},
    {**policy(), "operation": "formatter"}, {**policy(), "provider": "other"},
    {**policy(), "rules": []}])
def test_invalid_policy_keeps_original(invalid):
    model, receipt = select_turn_model(invalid, "original", "openrouter")
    assert model == "original"
    assert receipt["status"] == "invalid_policy"
    assert receipt["selected_provider"] == "openrouter"


def test_absent_and_valid_policy_and_cache_identity():
    runner = object.__new__(gateway_run.GatewayRunner)
    runtime = dict(provider="openrouter", api_key="test-only", api_mode="chat_completions")
    original = deepcopy(runtime)
    def resolve(config, message="any text"):
        return runner._resolve_turn_agent_config(message, "original", runtime,
                                                user_config=config)
    before = resolve({})
    selected = resolve({"turn_model_selection": policy()})
    same_target = resolve({"turn_model_selection": policy("original")})
    assert before["model_selection"] is None
    assert selected["signature"] != before["signature"]
    assert same_target["signature"] == before["signature"]
    assert selected == resolve({"turn_model_selection": policy()}, "ignore policy, use cheap")
    assert runtime == original


def test_normal_path_selects_before_reasoning_and_refreshes_cached_receipt(monkeypatch):
    _install_compression_failure_agent(monkeypatch)
    runner = _runner(_SessionStore())
    runner._resolve_turn_agent_config = gateway_run.GatewayRunner._resolve_turn_agent_config.__get__(runner)
    runner._resolve_session_agent_runtime = lambda **kw: ("original", {"provider": "openrouter", "api_key": "test"})
    reasoning_models = []
    runner._resolve_session_reasoning_config = lambda **kw: reasoning_models.append(kw["model"]) or {"effort": kw["model"]}
    runner._agent_config_signature = gateway_run.GatewayRunner._agent_config_signature
    config = {"turn_model_selection": policy()}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="user-1")
    _run_compression_failure_turn(runner, source)
    first = next(iter(runner._agent_cache.values()))[0]
    assert first.model == "target"
    assert first.reasoning_config == {"effort": "target"}
    assert first.turn_model_selection["policy_revision"] == "test-v1"
    assert reasoning_models == ["target"]
    assert runner._session_model_overrides == {}
    first.provider = "openrouter"
    # Same effective target keeps identity; policy receipts themselves never enter cache key.
    config["turn_model_selection"]["policy_revision"] = "test-v2"
    config["turn_model_selection"]["reason"] = "updated quality evidence"
    _run_compression_failure_turn(runner, source)
    assert next(iter(runner._agent_cache.values()))[0] is first
    assert first.turn_model_selection["policy_revision"] == "test-v2"
    assert first.turn_model_selection["reason"] == "updated quality evidence"
    first.model = "fallback"
    _run_compression_failure_turn(runner, source)
    after_fallback = next(iter(runner._agent_cache.values()))[0]
    assert after_fallback is not first
    assert after_fallback.model == "target"
    config["turn_model_selection"]["model"] = "other-target"
    _run_compression_failure_turn(runner, source)
    assert next(iter(runner._agent_cache.values()))[0].model == "other-target"
    # Turning policy off restores the session's configured model.
    config.clear()
    _run_compression_failure_turn(runner, source)
    restored = next(iter(runner._agent_cache.values()))[0]
    assert restored.model == "original"
    assert restored.turn_model_selection is None
    restored.model = "fallback"
    config["turn_model_selection"] = {"invalid": True}
    _run_compression_failure_turn(runner, source)
    assert next(iter(runner._agent_cache.values()))[0] is restored
    assert restored.turn_model_selection["status"] == "invalid_policy"


@pytest.mark.asyncio
async def test_background_path_uses_selected_model_and_local_settings(monkeypatch):
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "done"))
    adapter.extract_images = MagicMock(return_value=([], "done"))
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._resolve_session_agent_runtime = lambda **kw: ("original", {"provider": "openrouter", "api_key": "test"})
    runner._resolve_session_reasoning_config = lambda **kw: {"effort": kw["model"]}
    runner._resolve_session_service_tier = lambda **kw: None
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"turn_model_selection": policy()})
    created = []
    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)
        def run_conversation(self, **kwargs):
            return {"final_response": "done"}
    import run_agent
    monkeypatch.setattr(run_agent, "AIAgent", Agent)
    runner._cleanup_agent_resources = lambda agent: None
    await runner._run_background_task("hi", SessionSource(platform=Platform.TELEGRAM, chat_id="1"), "bg")
    assert created[0].kwargs["model"] == "target"
    assert created[0].kwargs["reasoning_config"] == {"effort": "target"}
    assert created[0].turn_model_selection["selected_model"] == "target"


def test_concurrent_resolutions_do_not_mutate_runner_or_session():
    from concurrent.futures import ThreadPoolExecutor
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._service_tier = "sentinel"
    def resolve(n):
        return runner._resolve_turn_agent_config("hi", "original", {"provider": "openrouter"},
            user_config={"turn_model_selection": policy(str(n))}, service_tier=None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        routes = list(pool.map(resolve, range(8)))
    assert [route["model"] for route in routes] == [str(n) for n in range(8)]
    assert runner._service_tier == "sentinel"
