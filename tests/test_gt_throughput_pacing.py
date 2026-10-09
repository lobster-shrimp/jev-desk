"""
Tests for GT throughput pacing improvements (PR #40).

1. Pace GT calls at ~5/min with ~12s spacing
2. Wait-for-slot within time budget instead of breaking
3. Start-to-start scheduling
4. DEX_BUDGET raised to 60
5. Comprehensive logging

All tests use fake clocks for determinism and speed.
"""
import logging
import os
from unittest.mock import Mock, patch
import pytest

# Setup path
import sys
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collect import GTRateLimiter


# ============================================================================
# Fake clock utilities
# ============================================================================

class FakeClock:
    """Deterministic clock for testing."""
    def __init__(self, start=1000.0):
        self.now = start
    
    def time(self):
        return self.now
    
    def sleep(self, duration):
        self.now += duration
    
    def advance(self, duration):
        self.now += duration


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def fake_clock():
    """Provide a fresh fake clock for each test."""
    return FakeClock()


@pytest.fixture
def limiter_with_fake_clock(fake_clock):
    """Provide a GTRateLimiter with fake clock."""
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0, 
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    return limiter, fake_clock


# ============================================================================
# GT pacing and spacing tests
# ============================================================================

def test_gt_limiter_enforces_minimum_spacing(limiter_with_fake_clock):
    """GTRateLimiter should enforce minimum spacing between calls."""
    limiter, clock = limiter_with_fake_clock
    
    # First call - no wait
    assert limiter.wait_if_needed()
    limiter.spend(1)
    first_time = clock.time()
    
    # Second call - should wait ~12s
    assert limiter.wait_if_needed()
    limiter.spend(1)
    second_time = clock.time()
    
    assert second_time - first_time >= 12.0


def test_gt_limiter_min_spacing_default():
    """GTRateLimiter should default to 60/calls_per_min for spacing."""
    limiter = GTRateLimiter(calls_per_min=5)
    assert limiter.min_spacing_sec == 12.0


def test_gt_limiter_spacing_configurable():
    """GTRateLimiter spacing should be configurable."""
    limiter = GTRateLimiter(calls_per_min=10, min_spacing_sec=8.0)
    assert limiter.min_spacing_sec == 8.0


def test_gt_limiter_tracks_last_call_time(fake_clock):
    """GTRateLimiter should track last call time for spacing."""
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=2.0, 
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    # First call
    limiter.spend(1)
    assert limiter.last_call_time == 1000.0
    
    # Advance time
    fake_clock.advance(5.0)
    
    # Second call
    limiter.spend(1)
    assert limiter.last_call_time == 1005.0


def test_gt_limiter_spacing_applies_to_universe_and_dossier(limiter_with_fake_clock):
    """Spacing should apply to both universe and dossier calls."""
    limiter, clock = limiter_with_fake_clock
    limiter.reserve(2)
    
    # Universe call
    assert limiter.wait_if_needed(priority=False)
    limiter.spend(1, priority=False)
    first_time = clock.time()
    
    # Dossier call (priority) - should still wait for spacing
    assert limiter.wait_if_needed(priority=True)
    limiter.spend(1, priority=True)
    second_time = clock.time()
    
    assert second_time - first_time >= 12.0


def test_gt_limiter_reset_cycle_stats(fake_clock):
    """reset_cycle_stats() should clear 429 count and wait time."""
    limiter = GTRateLimiter(calls_per_min=30, time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    # Record some stats
    limiter.stats_429_count = 5
    limiter.stats_wait_time = 120.5
    
    # Reset
    limiter.reset_cycle_stats()
    
    assert limiter.stats_429_count == 0
    assert limiter.stats_wait_time == 0.0


def test_gt_limiter_tracks_429_count(fake_clock):
    """record_429() should increment stats_429_count."""
    limiter = GTRateLimiter(calls_per_min=30, time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    assert limiter.stats_429_count == 0
    
    limiter.record_429(25.0)
    assert limiter.stats_429_count == 1
    
    limiter.record_429(30.0)
    assert limiter.stats_429_count == 2


def test_gt_limiter_tracks_wait_time(limiter_with_fake_clock):
    """wait_if_needed() should track total wait time."""
    limiter, clock = limiter_with_fake_clock
    
    # First call - no wait
    assert limiter.wait_if_needed()
    limiter.spend(1)
    
    # Second call - wait for spacing
    start_wait_time = limiter.stats_wait_time
    assert limiter.wait_if_needed()
    limiter.spend(1)
    
    # Should have tracked the ~12s spacing wait
    wait_added = limiter.stats_wait_time - start_wait_time
    assert 11.9 <= wait_added <= 12.1


def test_gt_limiter_reset_for_test(fake_clock):
    """reset_for_test() should clear all state including last_call_time."""
    limiter = GTRateLimiter(calls_per_min=5, time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    # Pollute state
    limiter.spend(1)
    limiter.record_429(25.0)
    limiter.stats_wait_time = 100.0
    limiter.reserve(3)
    
    assert limiter.last_call_time > 0
    assert len(limiter.calls) > 0
    
    # Reset
    limiter.reset_for_test()
    
    assert limiter.last_call_time == 0.0
    assert len(limiter.calls) == 0
    assert limiter.stats_429_count == 0
    assert limiter.stats_wait_time == 0.0
    assert limiter.reserved == 0


def test_wait_if_needed_loops_until_slot_free(fake_clock):
    """wait_if_needed should loop until slot is actually free (F3 boundary bug)."""
    limiter = GTRateLimiter(calls_per_min=2, window_sec=60.0,
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    # Fill window
    limiter.spend(1)  # at t=1000
    clock.advance(1.0)
    limiter.spend(1)  # at t=1001, window full
    
    # Advance to exactly boundary (first call expires)
    clock.now = 1060.0
    
    # This should succeed after loop (old code would fail at boundary)
    assert limiter.wait_if_needed()
    assert limiter.spend(1)


def test_wait_if_needed_respects_deadline(fake_clock):
    """wait_if_needed should return False if deadline would be exceeded."""
    limiter = GTRateLimiter(calls_per_min=2, window_sec=60.0,
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    
    # Fill window
    limiter.spend(1)
    limiter.spend(1)
    
    # Advance partway
    clock.advance(30.0)
    
    # Set deadline that would be exceeded by wait
    deadline = clock.time() + 20.0  # Need to wait ~30s more, but deadline is 20s away
    
    # Should return False
    assert not limiter.wait_if_needed(deadline=deadline)


# ============================================================================
# DEX_BUDGET tests
# ============================================================================

def test_dex_budget_default_60():
    """DEX_BUDGET should default to 60."""
    import main
    # Just check the constant value (no reload needed)
    assert main.DEX_BUDGET == 60 or os.environ.get("DEX_BUDGET") is not None


def test_gt_dossier_reserve_derived():
    """GT_DOSSIER_RESERVE should be 1 for 5/min rate."""
    import main
    # For 5/min, reserve should be 1 (5 - 4 = 1)
    if main.GT_CALLS_PER_MIN == 5:
        assert main.GT_DOSSIER_RESERVE == 1


# ============================================================================
# Mutation-killing tests (T2)
# ============================================================================

def test_no_break_on_full_window():
    """M1: Every free-passer should be dossiered even when window is full."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Fake clock for determinism
    fake_clock = FakeClock(start=1000.0)
    
    # Limiter with 5/min rate, 12s spacing
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0,
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    limiter.set_universe_budget(999)  # Unlimited for this test
    limiter.reserve(1)
    
    # Create 12 tokens that all pass free checks
    test_tids = [f"pass{i}:1399811149" for i in range(12)]
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [{"tid": tid, "ticker": f"P{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": fake_clock.time() - 1800}
                for i, tid in enumerate(test_tids) if tid in ids]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    dossier_calls = []
    
    def fake_dossier(t, limiter=None):
        dossier_calls.append((fake_clock.time(), t["tid"]))
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None,
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    original_now = shift._now
    original_sleep = shift._sleep
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    shift._now = fake_clock.time
    shift._sleep = fake_clock.sleep
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True,
                                       gt_limiter=limiter, cycle_time_budget=660)
        
        # All 12 tokens should have been dossiered
        assert len(dossier_calls) == 12, f"Expected 12 dossiers, got {len(dossier_calls)}"
        
        # Consecutive calls should be >= 12s apart
        for i in range(1, len(dossier_calls)):
            gap = dossier_calls[i][0] - dossier_calls[i-1][0]
            assert gap >= 12.0, f"Gap {i} was {gap:.1f}s, expected >= 12s"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        shift._now = original_now
        shift._sleep = original_sleep
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_time_budget_carry_in_priority_order():
    """M3: Time budget exhaustion should carry free-passers in priority order."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    fake_clock = FakeClock(start=1000.0)
    
    # Limiter with 30/min rate (no spacing bottleneck)
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=2.0,
                           time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    limiter.set_universe_budget(999)
    limiter.reserve(10)
    
    # 10 tokens, time budget allows only 5 dossiers
    test_tids = [f"tok{i}:1399811149" for i in range(10)]
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [{"tid": tid, "ticker": f"T{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": fake_clock.time() - 1800, "_tier": 2}
                for i, tid in enumerate(test_tids) if tid in ids]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        # Each trade check takes time
        fake_clock.advance(10.0)
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    dossier_count = [0]
    
    def fake_dossier(t, limiter=None):
        dossier_count[0] += 1
        fake_clock.advance(15.0)  # Each dossier takes time
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None,
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    original_now = shift._now
    original_sleep = shift._sleep
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    shift._now = fake_clock.time
    shift._sleep = fake_clock.sleep
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        # Short time budget: allow ~5 dossiers (5 * 15s dossier + 10 * 10s trade + overhead)
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True,
                                       gt_limiter=limiter, cycle_time_budget=180)
        
        # Should have carried the rest in priority order
        carry = book.get_carry())
        assert len(carry) > 0, "Should have carried some tokens when time budget exhausted"
        assert len(carry) <= 5, "Should have carried at most 5 tokens"
        
        # Carried tids should be from the tail of the list (in priority order)
        for carried_tid in carry:
            # Extract index from tid (tok0, tok1, etc)
            idx = int(carried_tid.split("tok")[1].split(":")[0])
            # Should be from second half
            assert idx >= 5, f"Carried token {carried_tid} should be from second half"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        shift._now = original_now
        shift._sleep = original_sleep
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_start_to_start_overrun(caplog):
    """M4: Cycle overrun should start next immediately with warning."""
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.WARNING)
    
    fake_clock = FakeClock(start=1000.0)
    
    # Track cycle starts
    cycle_starts = []
    sleep_calls = []
    
    original_run_once = shift.run_once
    original_now = shift._now
    original_sleep = shift._sleep
    
    def fake_run_once(*args, **kwargs):
        cycle_starts.append(fake_clock.time())
        # First cycle: 100s duration
        if len(cycle_starts) == 1:
            fake_clock.advance(100.0)
        # Second cycle: 950s duration (overrun)
        elif len(cycle_starts) == 2:
            fake_clock.advance(950.0)
        # Third cycle: exit
        else:
            raise StopTest()
        return None, {"seen": 0, "benched": 0}
    
    def fake_sleep(duration):
        sleep_calls.append(duration)
        fake_clock.sleep(duration)
    
    class StopTest(BaseException):
        """Use BaseException so main()'s except Exception doesn't catch it."""
        pass
    
    shift.run_once = fake_run_once
    shift._now = fake_clock.time
    shift._sleep = fake_sleep
    
    try:
        fake_fomo = Mock()
        fake_fomo.token = Mock()
        fake_judge = Mock()
        fake_desk = Mock()
        fake_desk.bank = Mock(return_value=10000)
        fake_desk.report = Mock()
        
        try:
            shift.main(fake_fomo, fake_judge, fake_desk, shadow=True, once=False)
        except StopTest:
            pass
        
        # After 100s cycle, should sleep 800s
        assert len(sleep_calls) >= 1
        assert 790 <= sleep_calls[0] <= 810, f"First sleep was {sleep_calls[0]}, expected ~800"
        
        # After 950s cycle (overrun by 50s), should sleep 0 and log warning
        assert len(sleep_calls) == 1, "Should not sleep after overrun"
        
        # Check for overrun warning
        overrun_logs = [r for r in caplog.records if "overran" in r.message.lower()]
        assert len(overrun_logs) > 0, "Should log overrun warning"
        
    finally:
        shift.run_once = original_run_once
        shift._now = original_now
        shift._sleep = original_sleep


# ============================================================================
# Comprehensive logging tests
# ============================================================================

def test_existing_log_lines_preserved(caplog):
    """F6: Existing 'cycle:' log line should be preserved."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.INFO)
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    fake_clock = FakeClock()
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return []
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_now = shift._now
    original_sleep = shift._sleep
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift._now = fake_clock.time
    shift._sleep = fake_clock.sleep
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, Mock(), fake_desk, 10000, shadow=True)
        
        # Check for existing log line format
        cycle_logs = [r for r in caplog.records if r.message.startswith("cycle:") and "seen" in r.message]
        assert len(cycle_logs) > 0, "Should preserve 'cycle: ...' log line"
        
        # Also check for new log lines
        summary_logs = [r for r in caplog.records if "cycle summary" in r.message]
        assert len(summary_logs) > 0, "Should add 'cycle summary' log line"
        
        gt_logs = [r for r in caplog.records if "cycle GT calls" in r.message]
        assert len(gt_logs) > 0, "Should add 'cycle GT calls' log line"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift._now = original_now
        shift._sleep = original_sleep
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
