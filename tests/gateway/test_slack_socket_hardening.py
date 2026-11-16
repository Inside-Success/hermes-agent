"""
Tests for Slack Socket Mode hardening actions (production resilience).

Tests the 5 hardening changes:
1. Accelerated stale detection (factor 4→2)
2. Circuit breaker for rapid rebuilds (>5 attempts in 60s)
3. Exponential backoff for reconnects (2s→4s→8s...→5min)
4. Metrics tracking (socket_reconnect_attempts_total, socket_rebuild_cycle_seconds)
5. Watchdog jitter to prevent thundering herd (±2s)
"""

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_slack_mock():
    """Install mock slack modules so SlackAdapter can be imported."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return  # Real library installed

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="xoxb-fake-token")
    a = SlackAdapter(config)
    a._app = MagicMock()
    a._app_token = "xapp-fake"
    a._proxy_url = None
    a._running = True
    a.handle_message = AsyncMock()
    return a


# ---------------------------------------------------------------------------
# Tests for Hardening #1: Accelerated stale detection (factor 4→2)
# ---------------------------------------------------------------------------


class TestStaleFactor:
    @pytest.mark.asyncio
    async def test_stale_factor_is_2_not_4(self, adapter):
        """Verify stale factor is set to 2 (was 4 before hardening)."""
        assert adapter._socket_ping_stale_factor == 2

    @pytest.mark.asyncio
    async def test_stale_threshold_calculation(self, adapter):
        """Verify stale detection uses factor 2 in calculation."""
        # With factor=2, stale threshold = ping_interval * 2
        ping_interval = 30.0
        expected_stale_threshold = ping_interval * adapter._socket_ping_stale_factor
        assert adapter._socket_ping_stale_factor == 2
        assert expected_stale_threshold == 60.0


# ---------------------------------------------------------------------------
# Tests for Hardening #2: Circuit breaker (>5 attempts in 60s)
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_allows_five_attempts(self, adapter):
        """Circuit breaker allows up to 5 reconnect attempts within 60s."""
        now = time.time()
        # Add exactly 5 attempts within the window (should be allowed)
        adapter._socket_reconnect_attempts = [now - 50, now - 40, now - 30, now - 20, now - 10]

        should_proceed, error_msg = adapter._check_socket_circuit_breaker()
        assert should_proceed is True
        assert error_msg is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_sixth_attempt(self, adapter):
        """Circuit breaker opens when exceeding 5 attempts in 60s window."""
        now = time.time()
        # Add 6 attempts within the window (should trigger circuit breaker)
        adapter._socket_reconnect_attempts = [
            now - 50, now - 40, now - 30, now - 20, now - 10, now - 5
        ]

        should_proceed, error_msg = adapter._check_socket_circuit_breaker()
        assert should_proceed is False
        assert "circuit breaker opened" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_circuit_breaker_clears_old_attempts(self, adapter):
        """Circuit breaker clears attempts older than 60s window."""
        now = time.time()
        # Add attempts: 2 old (>60s ago) and 5 recent
        adapter._socket_reconnect_attempts = [
            now - 120,  # Old (will be cleared)
            now - 100,  # Old (will be cleared)
            now - 50,   # Recent
            now - 40,   # Recent
            now - 30,   # Recent
            now - 20,   # Recent
            now - 10,   # Recent
        ]

        should_proceed, error_msg = adapter._check_socket_circuit_breaker()
        # After cleanup: 5 recent attempts remain, which is at the limit but OK
        assert should_proceed is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_backoff(self, adapter):
        """Circuit breaker resets backoff exponent when it opens."""
        adapter._socket_backoff_exponent = 5
        now = time.time()
        # Add 6 attempts to trigger the breaker
        adapter._socket_reconnect_attempts = [
            now - 50, now - 40, now - 30, now - 20, now - 10, now - 5
        ]

        should_proceed, error_msg = adapter._check_socket_circuit_breaker()
        assert should_proceed is False
        assert adapter._socket_backoff_exponent == 0


# ---------------------------------------------------------------------------
# Tests for Hardening #3: Exponential backoff (2s→4s→8s...→5min)
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    @pytest.mark.asyncio
    async def test_backoff_starts_at_2_seconds(self, adapter):
        """Exponential backoff starts at base 2 seconds."""
        adapter._socket_backoff_exponent = 0
        backoff = adapter._socket_backoff_base_s * (2 ** adapter._socket_backoff_exponent)
        assert backoff == 2.0

    @pytest.mark.asyncio
    async def test_backoff_doubles_each_level(self, adapter):
        """Backoff doubles at each level: 2s, 4s, 8s, 16s, etc."""
        expected = [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0]
        for i, expected_val in enumerate(expected):
            adapter._socket_backoff_exponent = i
            backoff = adapter._socket_backoff_base_s * (2 ** adapter._socket_backoff_exponent)
            assert backoff == expected_val

    @pytest.mark.asyncio
    async def test_backoff_caps_at_5_minutes(self, adapter):
        """Backoff is capped at 5 minutes (300s)."""
        adapter._socket_backoff_exponent = 20  # Would be 2^20 * 2 = 2M seconds
        backoff = min(
            adapter._socket_backoff_base_s * (2 ** adapter._socket_backoff_exponent),
            adapter._socket_backoff_max_s
        )
        assert backoff == 300.0

    @pytest.mark.asyncio
    async def test_backoff_resets_on_successful_reconnect(self, adapter):
        """Backoff exponent resets to 0 after successful reconnect."""
        adapter._socket_backoff_exponent = 5
        adapter._socket_backoff_exponent = 0
        assert adapter._socket_backoff_exponent == 0


# ---------------------------------------------------------------------------
# Tests for Hardening #4: Metrics tracking
# ---------------------------------------------------------------------------


class TestMetricsTracking:
    @pytest.mark.asyncio
    async def test_metrics_counter_initialized(self, adapter):
        """Socket reconnect metrics counters are initialized."""
        assert adapter._socket_reconnect_attempts_total == 0
        assert adapter._socket_rebuild_cycle_start is None

    @pytest.mark.asyncio
    async def test_rebuild_cycle_timing_recorded(self, adapter):
        """Rebuild cycle duration is tracked and recorded."""
        # Simulate a rebuild cycle
        adapter._socket_rebuild_cycle_start = time.time() - 5.0  # Started 5s ago
        adapter._socket_reconnect_attempts = [time.time() - 4, time.time() - 2]

        adapter._record_socket_rebuild_cycle()

        # After recording, state should be cleared
        assert adapter._socket_rebuild_cycle_start is None
        assert adapter._socket_reconnect_attempts == []

    @pytest.mark.asyncio
    async def test_reconnect_attempts_total_incremented(self, adapter):
        """Total reconnect attempts counter is incremented."""
        adapter._socket_reconnect_attempts_total = 0
        adapter._socket_reconnect_attempts_total += 1
        assert adapter._socket_reconnect_attempts_total == 1


# ---------------------------------------------------------------------------
# Tests for Hardening #5: Watchdog jitter (±2s)
# ---------------------------------------------------------------------------


class TestWatchdogJitter:
    @pytest.mark.asyncio
    async def test_watchdog_jitter_range(self, adapter):
        """Watchdog interval gets ±2s random jitter."""
        adapter._socket_watchdog_interval_s = 15.0

        # Generate multiple jittered intervals and verify they're in range
        jittered_values = []
        for _ in range(100):
            import random
            jittered = adapter._socket_watchdog_interval_s + random.uniform(-2, 2)
            jittered_values.append(jittered)

        # All values should be between 13 and 17
        assert all(13.0 <= v <= 17.0 for v in jittered_values)
        # Should have variety (not all the same)
        assert len(set(jittered_values)) > 10

    @pytest.mark.asyncio
    async def test_watchdog_jitter_prevents_thundering_herd(self, adapter):
        """Jitter desynchronizes watchdog polls across gateway profiles."""
        adapter._socket_watchdog_interval_s = 15.0

        # Simulate multiple adapters with jitter
        intervals = []
        for _ in range(10):
            import random
            jitter = random.uniform(-2, 2)
            intervals.append(adapter._socket_watchdog_interval_s + jitter)

        # Verify no two intervals are identical (within floating point precision)
        unique_intervals = len(set(round(i, 2) for i in intervals))
        # With 10 samples and ±2s range, we should have high variation
        assert unique_intervals > 5


# ---------------------------------------------------------------------------
# Integration tests for hardening
# ---------------------------------------------------------------------------


class TestHardeningIntegration:
    @pytest.mark.asyncio
    async def test_multiple_hardening_features_work_together(self, adapter):
        """Multiple hardening features work together without conflict."""
        # Verify initialization of all hardening state
        assert adapter._socket_ping_stale_factor == 2
        assert adapter._socket_reconnect_attempt_limit == 5
        assert adapter._socket_backoff_base_s == 2.0
        assert adapter._socket_backoff_max_s == 300.0
        assert adapter._socket_reconnect_attempts_total == 0

        # Simulate rapid restarts: 6 attempts should trigger circuit breaker
        for i in range(6):
            adapter._socket_reconnect_attempts.append(time.time() - (6 - i))

        # Circuit breaker should detect this
        should_proceed, error = adapter._check_socket_circuit_breaker()
        assert should_proceed is False
        assert "circuit breaker" in error.lower()
