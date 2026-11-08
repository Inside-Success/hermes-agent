"""Post-budget tool input must survive persistence and replay."""

from types import SimpleNamespace
import pytest

from agent.turn_context import substitute_api_content
from hermes_state import SessionDB


@pytest.mark.parametrize("payload", [
    {"messages": [{"role": "tool", "tool_call_id": "call", "content": "sent"}]},
    {"input": [{"type": "function_call_output", "call_id": "call", "output": "sent"}]},
    {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": "sent"}]}]},
])
def test_request_capture_uses_provider_tool_envelope(tmp_path, payload):
    from agent.tool_executor import _persist_tool_api_content_from_request
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "test")
    db.append_message("s", "tool", "raw", tool_call_id="call")
    agent = SimpleNamespace(_session_db=db, session_id="s")
    _persist_tool_api_content_from_request(agent, payload, set())
    assert db.get_messages("s")[0]["api_content"] is None
    _persist_tool_api_content_from_request(agent, payload, {"call"})
    row = db.get_messages("s")[0]
    assert row["content"] == "raw"
    assert row["api_content"] == "sent"
    db.close()


def test_tool_sidecar_is_used_by_api_message_builder():
    msg = {"role": "tool", "content": "raw", "api_content": "sent", "tool_call_id": "call"}
    substitute_api_content(msg)
    assert msg == {"role": "tool", "content": "sent", "tool_call_id": "call"}


def test_final_tool_context_preserves_raw_and_replays_sent(tmp_path):
    from agent.tool_executor import _persist_tool_api_content

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "test")
    db.append_message("s", "tool", "raw evidence", tool_call_id="call")
    agent = SimpleNamespace(_session_db=db, session_id="s")
    msg = {"role": "tool", "content": "preview and readable artifact", "tool_call_id": "call"}
    _persist_tool_api_content(agent, [msg])
    row = db.get_messages("s")[0]
    assert row["content"] == "raw evidence"
    assert row["api_content"] == msg["content"]
    replay = db.get_messages_as_conversation("s", repair_alternation=False)[0]
    substitute_api_content(replay)
    assert replay["content"] == msg["content"]
    assert replay["tool_call_id"] == "call"
    db.close()


def test_segmented_finalize_records_budget_and_steer(tmp_path, monkeypatch):
    from agent.tool_executor import execute_tool_calls_segmented
    from tools.budget_config import BudgetConfig

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "test")
    raw = "evidence\n" * 1000
    db.append_message("s", "tool", raw, tool_call_id="call")
    messages = [{"role": "tool", "content": raw, "tool_call_id": "call"}]
    def steer(messages, count):
        messages[-1]["content"] += "\nUser steer"
    agent = SimpleNamespace(_session_db=db, session_id="s",
                            _apply_pending_steer_to_tool_results=steer)
    monkeypatch.setattr("agent.tool_executor._budget_for_agent", lambda a: BudgetConfig(turn_budget=2000))
    monkeypatch.setattr("agent.tool_executor.get_active_env", lambda task: None)
    execute_tool_calls_segmented(agent, SimpleNamespace(tool_calls=[object()]), messages, "task", segments=[])
    assert messages[0]["content"] != raw
    assert messages[0]["content"].endswith("User steer")
    row = db.get_messages("s")[0]
    assert row["content"] == raw
    assert row["api_content"] == messages[0]["content"]
    db.close()


def test_tool_sidecar_update_is_session_scoped_and_explicit_when_identical(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    for session in ("a", "b"):
        db.create_session(session, "test")
        db.append_message(session, "tool", "raw", tool_call_id="call")
    assert db.set_tool_api_content("a", "call", "sent") == 1
    assert db.get_messages("b")[0]["api_content"] is None
    assert db.set_tool_api_content("a", "call", "raw") == 1
    assert db.get_messages("a")[0]["api_content"] == "raw"
    assert db.set_tool_api_content("a", "call", "") == 1
    replay = db.get_messages_as_conversation("a", repair_alternation=False)[0]
    substitute_api_content(replay)
    assert replay["content"] == ""
    assert db.set_tool_api_content("a", "missing", "sent") == 0
    db.close()


def test_progress_flush_keeps_pre_result_budget_evidence(tmp_path):
    from agent.tool_executor import _flush_session_db_after_tool_progress
    from run_agent import AIAgent

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "test")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent.session_id = "s"
    agent._last_flushed_db_idx = 0
    agent._session_db_created = True
    msg = {"role": "tool", "content": "short preview", "tool_call_id": "call"}
    _flush_session_db_after_tool_progress(agent, [msg], stage="result", raw_tool_content="complete raw evidence")
    assert msg["content"] == "short preview"
    assert "api_content" not in msg
    agent._flush_messages_to_session_db([msg])
    rows = db.get_messages("s")
    assert len(rows) == 1
    assert rows[0]["content"] == "complete raw evidence"
    assert rows[0]["api_content"] == "short preview"
    db.close()
