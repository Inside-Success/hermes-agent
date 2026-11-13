"""Native memory identity and recovery contracts, using only temporary profiles."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools.memory_tool import MemoryStore, memory_tool
from agent.learning_graph import _memory_cards
from agent.learning_mutations import delete_node, edit_node, node_detail, revert_node

@pytest.fixture
def store():
    value = MemoryStore()
    value.load_from_disk()
    return value

def cards():
    return _memory_cards()

@pytest.mark.req("native-memory/earlier-entry-deleted")
def test_stale_id_cannot_shift_onto_next_entry(store):
    store.add('memory', 'alpha')
    store.add('memory', 'beta')
    a, b = cards()
    assert delete_node(a['id'])['ok']
    assert not delete_node(a['id'])['ok']
    assert cards()[0]['item_id'] == b['item_id']
    assert node_detail(b['id'])['content'] == 'beta'

@pytest.mark.req("native-memory/revert-after-unrelated-write")
def test_revision_cas_and_compensating_revert(store):
    store.add('memory', 'alpha')
    a = cards()[0]
    assert edit_node(a['id'], 'alpha edited')['ok']
    store.add('memory', 'unrelated')
    edited = cards()[0]
    assert edited['item_id'] == a['item_id']
    assert edited['revision'] != a['revision']
    assert not edit_node(a['id'], 'stale')['ok']
    assert revert_node(edited['id'], a['revision'])['ok']
    assert [c['body'] for c in cards()] == ['alpha', 'unrelated']
    assert cards()[0]['revision'] not in {a['revision'], edited['revision']}

@pytest.mark.req("native-memory/historical-duplicate-entries")
def test_historical_and_duplicate_manual_entries_have_unknown_origins(store):
    path = get_hermes_home() / 'memories' / 'MEMORY.md'
    path.write_text('manual\n§\nmanual\n§\nother', encoding='utf-8')
    old = cards()
    assert len({c['item_id'] for c in old}) == 3
    assert all(c['timestamp'] is None and c['provenance'] is None for c in old)
    assert all(c['observed_at'] for c in old)
    path.write_text('manual\n§\nother', encoding='utf-8')
    assert not edit_node(old[0]['id'], 'wrong duplicate')['ok']
    assert [c['body'] for c in cards()] == ['manual', 'other']

@pytest.mark.parametrize('origin,context', [('assistant_tool', 'foreground'), ('background_review', 'background_review')])
@pytest.mark.req("native-memory/foreground-and-background-writes")
def test_new_writes_record_trusted_session_origin(store, origin, context):
    db = SessionDB(get_hermes_home() / 'state.db')
    db.create_session('s', 'slack', chat_id='chat', thread_id='thread')
    db.close()
    metadata = {'session_id': 's', 'platform': 'forged', 'thread_id': 'forged', 'write_origin': origin, 'execution_context': context, 'tool_call_id': 'call'}
    result = json.loads(memory_tool(action='add', content='new', store=store, write_metadata=metadata))
    assert result['success']
    item = cards()[0]
    assert item['timestamp'] is not None
    assert item['provenance']['source'] == 'slack'
    assert item['provenance']['thread_id'] == 'thread'
    assert item['provenance']['write_origin'] == origin
    assert item['provenance']['execution_context'] == context
    assert item['provenance']['tool_call_id'] == 'call'

@pytest.mark.req("native-memory/foreign-profile")
def test_foreign_profile_id_rejected(store, monkeypatch, tmp_path):
    store.add('memory', 'same body')
    old = cards()[0]
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'foreign'))
    other = MemoryStore()
    other.load_from_disk()
    other.add('memory', 'same body')
    assert not edit_node(old['id'], 'wrong profile')['ok']
    assert cards()[0]['body'] == 'same body'

@pytest.mark.req("native-memory/concurrent-edit")
def test_concurrent_edit_has_one_winner(store):
    store.add('memory', 'alpha')
    old = cards()[0]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda body: edit_node(old['id'], body), ['one', 'two']))
    assert sum(r['ok'] for r in results) == 1

@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.req("native-memory/failure-around-materialization")
def test_failed_materialization_recovers_only_exact_hash(store, monkeypatch, phase):
    store.add('memory', 'alpha')
    original = MemoryStore._write_file
    def fail(path, entries):
        if phase == 'after':
            original(path, entries)
        raise RuntimeError('simulated materialization failure')
    with monkeypatch.context() as patch:
        patch.setattr(MemoryStore, '_write_file', staticmethod(fail))
        with pytest.raises(RuntimeError):
            store.add('memory', 'beta')
    assert [c['body'] for c in cards()] == (['alpha', 'beta'] if phase == 'after' else ['alpha'])
    assert store.add('memory', 'gamma')['success']

@pytest.mark.req("native-memory/conflicting-external-content")
def test_conflicting_recovery_blocks(store, monkeypatch):
    store.add('memory', 'alpha')
    def fail(path, entries):
        path.write_text('external conflict', encoding='utf-8')
        raise RuntimeError('failure')
    with monkeypatch.context() as patch:
        patch.setattr(MemoryStore, '_write_file', staticmethod(fail))
        with pytest.raises(RuntimeError):
            store.add('memory', 'beta')
    with pytest.raises(RuntimeError, match='conflict'):
        store.add('memory', 'gamma')
    assert (get_hermes_home() / 'memories' / 'MEMORY.md').read_text() == 'external conflict'

@pytest.mark.req("native-memory/unavailable-journal")
def test_unavailable_journal_prevents_file_write(store, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('journal unavailable')
    monkeypatch.setattr(SessionDB, 'memory_prepare', fail)
    with pytest.raises(RuntimeError, match='journal unavailable'):
        store.add('memory', 'new')
    assert not (get_hermes_home() / 'memories' / 'MEMORY.md').exists()

@pytest.mark.req("native-memory/preserve-frozen-prompt")
def test_snapshot_frozen_after_write(store):
    before = store.format_for_system_prompt('memory')
    store.add('memory', 'new')
    assert store.format_for_system_prompt('memory') == before

@pytest.mark.req("native-memory/revert-after-unrelated-write")
def test_delete_revert_restores_only_deleted_item(store):
    store.add('memory', 'alpha')
    original = cards()[0]
    deleted = delete_node(original['id'])
    assert deleted['ok']
    store.add('memory', 'beta newer')
    assert revert_node(deleted['id'], original['revision'])['ok']
    assert {c['body'] for c in cards()} == {'alpha', 'beta newer'}
    assert not revert_node(deleted['id'], original['revision'])['ok']


@pytest.mark.req("native-memory/revert-after-unrelated-write")
def test_cli_dispatch_history_and_revert(store, capsys):
    import argparse
    from hermes_cli.journey import register_cli
    store.add('memory', 'alpha')
    original = cards()[0]
    assert edit_node(original['id'], 'edited')['ok']
    store.add('memory', 'beta newer')
    current = cards()[0]
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(['history', current['id']])
    assert args.func(args) == 0
    history = json.loads(capsys.readouterr().out)
    assert original['revision'] in {item['revision'] for item in history['history']}
    args = parser.parse_args(['revert', current['id'], original['revision']])
    assert args.func(args) == 0
    assert [c['body'] for c in cards()] == ['alpha', 'beta newer']


@pytest.mark.req("native-memory/foreground-and-background-writes")
def test_missing_native_session_blocks_materialization(store):
    with pytest.raises(RuntimeError, match='active profile'):
        memory_tool(action='add', content='unknown cause', store=store, write_metadata={'session_id': 'foreign'})
    assert cards() == []


@pytest.mark.req("native-memory/foreign-profile")
def test_existing_store_cannot_follow_profile_switch(store, monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other'))
    with pytest.raises(RuntimeError, match='different profile'):
        store.add('memory', 'wrong profile')


@pytest.mark.req("native-memory/earlier-entry-deleted")
def test_legacy_id_cannot_authorize_mutation(store):
    store.add('memory', 'alpha')
    assert not delete_node('memory:memory:0')['ok']
    assert not edit_node('memory:memory:0', 'wrong')['ok']
    assert cards()[0]['body'] == 'alpha'

@pytest.mark.parametrize('dispatch', ['sequential', 'worker'])
@pytest.mark.parametrize('origin', ['assistant_tool', 'background_review'])
@pytest.mark.req("native-memory/foreground-and-background-writes")
def test_real_dispatch_records_causal_context_without_provider_or_notices(store, dispatch, origin):
    from types import SimpleNamespace
    from agent.background_review import build_memory_write_metadata
    from agent.agent_runtime_helpers import invoke_tool
    from agent.tool_executor import execute_tool_calls_sequential
    db = SessionDB(get_hermes_home() / 'state.db')
    db.create_session('parent', 'slack', chat_id='chat', thread_id='thread')
    db.create_session('child', 'slack', parent_session_id='parent', chat_id='chat', thread_id='thread')
    db.close()
    notices = []
    agent = SimpleNamespace(
        session_id='child', _parent_session_id='parent', platform='slack',
        _memory_write_origin=origin, _memory_write_context=('background_review' if origin == 'background_review' else 'foreground'),
        _memory_store=store, _memory_manager=None, quiet_mode=True, verbose_logging=False,
        _interrupt_requested=False, _current_turn_id='', _current_api_request_id='',
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)),
        _touch_activity=lambda *a: None, _should_emit_quiet_tool_messages=lambda: False,
        tool_start_callback=None, tool_progress_callback=None, tool_complete_callback=None,
        _append_guardrail_observation=lambda name, args, result, **kw: result,
        _record_file_mutation_result=lambda *a: None,
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda *a: None),
        _tool_result_content_for_active_model=lambda name, result: result,
        _flush_messages_to_session_db=lambda *a: None,
        _apply_pending_steer_to_tool_results=lambda *a: None,
        _safe_print=lambda *a: notices.append(a), tool_delay=0,
    )
    agent._build_memory_write_metadata = lambda **kw: build_memory_write_metadata(agent, **kw)
    args = {'action': 'add', 'content': 'dispatch entry'}
    if dispatch == 'worker':
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(invoke_tool, agent, 'memory', args, 'task', 'tool-call').result()
    else:
        call = SimpleNamespace(id='tool-call', function=SimpleNamespace(name='memory', arguments=json.dumps(args)))
        messages = []
        execute_tool_calls_sequential(agent, SimpleNamespace(tool_calls=[call]), messages, 'task')
        result = messages[-1]['content']
    assert json.loads(result)['success']
    metadata = cards()[0]['provenance']
    assert metadata['session_id'] == 'child'
    assert metadata['parent_session_id'] == 'parent'
    assert metadata['thread_id'] == 'thread'
    assert metadata['tool_call_id'] == 'tool-call'
    assert metadata['task_id'] == 'task'
    assert metadata['write_origin'] == origin
    assert notices == []


@pytest.mark.req("native-memory/failure-around-materialization")
def test_commit_failure_never_reports_success_and_recovers(store, monkeypatch):
    store.add('memory', 'alpha')
    def fail(*a, **kw):
        raise RuntimeError('commit unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(SessionDB, 'memory_finish', fail)
        with pytest.raises(RuntimeError, match='commit unavailable'):
            store.add('memory', 'beta')
    db = SessionDB(get_hermes_home() / 'state.db')
    assert db.memory_pending('memory')['status'] == 'materialized'
    db.close()
    assert [c['body'] for c in cards()] == ['alpha', 'beta']
    assert all(c['timestamp'] for c in cards())


@pytest.mark.req("native-memory/foreign-profile")
def test_copied_profile_cannot_reuse_source_mutation_handle(store, monkeypatch, tmp_path):
    import shutil
    store.add('memory', 'source entry')
    original = cards()[0]
    source = get_hermes_home()
    other = tmp_path / 'cloned-profile'
    (other / 'memories').mkdir(parents=True)
    shutil.copyfile(source / 'state.db', other / 'state.db')
    shutil.copyfile(source / 'memories' / 'MEMORY.md', other / 'memories' / 'MEMORY.md')
    monkeypatch.setenv('HERMES_HOME', str(other))
    assert not edit_node(original['id'], 'wrong profile')['ok']
    assert cards()[0]['body'] == 'source entry'


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.req("native-memory/deferred-approval")
def test_approved_native_write_retains_original_host_context(store, monkeypatch, batch):
    from types import SimpleNamespace
    from tools import write_approval as approval
    from tools.memory_tool import apply_memory_pending
    db = SessionDB(get_hermes_home() / 'state.db')
    db.create_session('original', 'slack', chat_id='chat', thread_id='thread')
    db.close()
    monkeypatch.setattr(approval, 'evaluate_gate', lambda *a, **kw: SimpleNamespace(allow=False, blocked=False, message='staged'))
    kwargs = {'operations': [{'action': 'add', 'content': 'approved native fact'}]} if batch else {'action': 'add', 'content': 'approved native fact'}
    result = json.loads(memory_tool(store=store, write_metadata={'session_id': 'original', 'tool_call_id': 'original-call'}, **kwargs))
    assert result['staged']
    assert cards() == []
    record = approval.get_pending('memory', result['pending_id'])
    assert apply_memory_pending(record['payload'], store)['success']
    assert cards()[0]['provenance']['session_id'] == 'original'
    assert cards()[0]['provenance']['tool_call_id'] == 'original-call'


@pytest.mark.req('native-memory/concurrent-edit')
def test_shared_store_keeps_memory_and_user_transactions_independent(store, monkeypatch):
    import threading
    store.add('memory', 'memory original')
    store.add('user', 'user original')
    original = MemoryStore.save_to_disk
    ready = threading.Barrier(2)
    def interleave(self, target, **kwargs):
        ready.wait(timeout=10)
        return original(self, target, **kwargs)
    monkeypatch.setattr(MemoryStore, 'save_to_disk', interleave)
    with ThreadPoolExecutor(2) as pool:
        memory = pool.submit(store.replace, 'memory', 'memory original', 'memory updated')
        user = pool.submit(store.replace, 'user', 'user original', 'user updated')
        assert memory.result()['success']
        assert user.result()['success']
    assert [c['body'] for c in cards()] == ['memory updated', 'user updated']
