"""Independent offline falsifying controls; review worktree only."""
from unittest.mock import patch
import sys
import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli.model_normalize import normalize_model_for_provider
from tests.gateway.test_compression_failure_session_sync import (
    _runner, _SessionStore, _install_compression_failure_agent,
    _run_compression_failure_turn, _CompressionThenFailureAgent,
)


def test_real_agent_normalizes_accepted_policy_model():
    from run_agent import AIAgent
    with (
        patch('run_agent.get_tool_definitions', return_value=[]),
        patch('run_agent.check_toolset_requirements', return_value={}),
        patch('run_agent.OpenAI'),
    ):
        agent = AIAgent(model='deepseek/deepseek-v4-pro', provider='deepseek',
            api_key='test-only', base_url='https://api.deepseek.com/v1',
            quiet_mode=True, skip_context_files=True, skip_memory=True)
    assert agent.model == 'deepseek-v4-pro'


@pytest.mark.parametrize('policy_model', ['deepseek-v4-pro', 'deepseek/deepseek-v4-pro'])
def test_unchanged_policy_reuses_native_model_agent(monkeypatch, policy_model):
    _install_compression_failure_agent(monkeypatch)
    class NormalizingAgent(_CompressionThenFailureAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.provider = kwargs['provider']
            self.model = normalize_model_for_provider(self.model, self.provider)
    monkeypatch.setattr(sys.modules['run_agent'], 'AIAgent', NormalizingAgent)
    runner = _runner(_SessionStore())
    runner._resolve_turn_agent_config = gateway_run.GatewayRunner._resolve_turn_agent_config.__get__(runner)
    runner._resolve_session_agent_runtime = lambda **kw: ('deepseek-chat', {'provider': 'deepseek', 'api_key': 'test-only'})
    runner._agent_config_signature = gateway_run.GatewayRunner._agent_config_signature
    config = {'turn_model_selection': dict(policy_revision='v1', operation='unrestricted_turn',
        reason='test', model=policy_model, provider='deepseek')}
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', user_id='user-1')
    _run_compression_failure_turn(runner, source)
    first = next(iter(runner._agent_cache.values()))[0]
    assert first.turn_model_selection['status'] == 'selected'
    assert first.model == 'deepseek-v4-pro'
    _run_compression_failure_turn(runner, source)
    assert next(iter(runner._agent_cache.values()))[0] is first
