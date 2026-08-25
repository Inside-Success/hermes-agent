"""The circuit breaker counts unreachable servers, not unhappy tools.

A server that answers with a refusal is a working server. Before this fix the
tool handler parsed every result and bumped the breaker on any top-level
``error`` key, so three ordinary refusals in a row — an unconnected provider, a
channel the agent is not in, a resource the caller may not read — opened the
breaker and made the model tell the user the MCP server was "unreachable after 3
consecutive failures". It was reachable. It had answered three times.
"""
import json
import threading
from unittest.mock import MagicMock

import pytest


def _stub_server(name: str):
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()
    server = MagicMock()
    server.name = name

    ready = threading.Event()
    ready.set()

    class _Ready:
        def is_set(self):
            return ready.is_set()

        def clear(self):
            ready.clear()

        def set(self):
            ready.set()

    server._ready = _Ready()
    server._reconnect_event = None
    return server


def _result(*, is_error: bool, text: str):
    result = MagicMock()
    result.isError = is_error
    result.content = [MagicMock(type="text", text=text)]
    result.structuredContent = None
    return result


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import mcp_tool

    server = _stub_server("brain")
    mcp_tool._servers["brain"] = server
    mcp_tool._server_error_counts.pop("brain", None)
    mcp_tool._server_breaker_opened_at.pop("brain", None)
    try:
        yield mcp_tool, server
    finally:
        mcp_tool._servers.pop("brain", None)
        mcp_tool._server_error_counts.pop("brain", None)
        mcp_tool._server_breaker_opened_at.pop("brain", None)


REFUSALS = [
    "gmail read failed: brian has not connected google yet",
    "channel 'C_PRIVATE' not found — is your brain invited to it?",
    "not authorized to read this meeting: meeting_not_accessible",
]


def test_repeated_refusals_do_not_open_the_breaker(srv):
    """The reported failure: three refusals used to make the model announce
    that a perfectly healthy server was unreachable."""
    mcp_tool, server = srv
    from tools.mcp_tool import _make_tool_handler

    calls = {"n": 0}

    async def _refuse(*a, **kw):
        calls["n"] += 1
        return _result(is_error=True, text=json.dumps({"error": REFUSALS[calls["n"] % 3]}))

    server.session = MagicMock()
    server.session.call_tool = _refuse

    handler = _make_tool_handler("brain", "gmail_recent", 10.0)
    for _ in range(5):
        parsed = json.loads(handler({}))
        # The refusal still reaches the model, unchanged.
        assert "error" in parsed, parsed
        assert "unreachable" not in parsed["error"], (
            "the breaker short-circuited a server that answered every call"
        )

    assert calls["n"] == 5, "a call was short-circuited instead of reaching the server"
    assert mcp_tool._server_error_counts.get("brain", 0) == 0
    assert "brain" not in mcp_tool._server_breaker_opened_at


def test_a_refusal_closes_a_breaker_a_real_outage_opened(srv):
    """A server that starts answering again is healthy, even if what it
    answers with is a refusal."""
    mcp_tool, server = srv
    from tools.mcp_tool import _make_tool_handler

    mcp_tool._server_error_counts["brain"] = 2  # one short of the threshold

    async def _refuse(*a, **kw):
        return _result(is_error=True, text=json.dumps({"error": REFUSALS[0]}))

    server.session = MagicMock()
    server.session.call_tool = _refuse

    handler = _make_tool_handler("brain", "gmail_recent", 10.0)
    json.loads(handler({}))
    assert mcp_tool._server_error_counts.get("brain", 0) == 0


def test_transport_failures_still_open_the_breaker(srv):
    """Preserved-behaviour canary. The breaker exists for #10447's burn loop;
    a server that does not answer must still trip it."""
    mcp_tool, server = srv
    from tools.mcp_tool import _make_tool_handler

    async def _die(*a, **kw):
        raise RuntimeError("connection reset by peer")

    server.session = MagicMock()
    server.session.call_tool = _die

    handler = _make_tool_handler("brain", "gmail_recent", 10.0)
    for _ in range(mcp_tool._CIRCUIT_BREAKER_THRESHOLD):
        parsed = json.loads(handler({}))
        assert "error" in parsed

    assert mcp_tool._server_error_counts["brain"] >= mcp_tool._CIRCUIT_BREAKER_THRESHOLD
    assert "brain" in mcp_tool._server_breaker_opened_at

    # And the open breaker short-circuits the next call, as designed.
    parsed = json.loads(handler({}))
    assert "unreachable" in parsed["error"], parsed


def test_success_still_closes_the_breaker(srv):
    """Unchanged path, asserted so the fix cannot silently break it."""
    mcp_tool, server = srv
    from tools.mcp_tool import _make_tool_handler

    mcp_tool._server_error_counts["brain"] = 2

    async def _ok(*a, **kw):
        return _result(is_error=False, text="real data")

    server.session = MagicMock()
    server.session.call_tool = _ok

    handler = _make_tool_handler("brain", "who_can_i_ask", 10.0)
    parsed = json.loads(handler({}))
    assert parsed.get("result") == "real data", parsed
    assert mcp_tool._server_error_counts.get("brain", 0) == 0
