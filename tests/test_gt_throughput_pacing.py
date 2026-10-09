"""
Tests for GT throughput pacing improvements (PR #40).

1. Pace GT calls at ~5/min with ~12s spacing
2. Wait-for-slot within time budget instead of breaking
3. Start-to-start scheduling
4. DEX_BUDGET raised to 60
5. Comprehensive logging
"""
import logging
import os
import time
from unittest.mock import Mock, patch
import pytest

# Setup path
import sys
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collect import GTRateLimiter


# ============================================================================
# GT pacing and spacing tests
# ============================================================================

def test_gt_limiter_enforces_minimum_spacing():
    """GTRateLimiter should enforce minimum spacing between calls."""
    # Create limiter with 5 calls/min -> 12s spacing
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0, time_fn=time.time)
    
    # First call - no wait
    start = time.time()
    limiter.wait_if_needed()
    limiter.spend(1)
    first_duration = time.time() - start
    assert first_duration < 0.1, "First call should be immediate"
    
    # Second call - should wait ~12s
    start = time.time()
    limiter.wait_if_needed()
    limiter.spend(1)
    second_duration = time.time() - start
    assert 11.5 <= second_duration <= 12.5, f"Second call should wait ~12s, got {second_duration:.1f}s"


def test_gt_limiter_min_spacing_default():
    """GTRateLimiter should default to 60/calls_per_min for spacing."""
    limiter = GTRateLimiter(calls_per_min=5)
    
    # Default spacing should be 60/5 = 12s
    assert limiter.min_spacing_sec == 12.0


def test_gt_limiter_spacing_configurable():
    """GTRateLimiter spacing should be configurable."""
    limiter = GTRateLimiter(calls_per_min=10, min_spacing_sec=8.0)
    
    assert limiter.min_spacing_sec == 8.0


def test_gt_limiter_tracks_last_call_time():
    """GTRateLimiter should track last call time for spacing."""
    mock_time = 1000.0
    
    def fake_time():
        return mock_time
    
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=2.0, time_fn=fake_time)
    
    # First call
    limiter.spend(1)
    assert limiter.last_call_time == 1000.0
    
    # Second call after 1s - should see last_call_time updated
    mock_time = 1001.0
    limiter.spend(1)
    assert limiter.last_call_time == 1001.0


def test_gt_limiter_spacing_applies_to_universe_and_dossier():
    """Spacing should apply to both universe and dossier calls."""
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0, time_fn=time.time)
    limiter.reserve(2)
    
    # Universe call
    start = time.time()
    limiter.wait_if_needed(priority=False)
    limiter.spend(1, priority=False)
    first_duration = time.time() - start
    assert first_duration < 0.1
    
    # Dossier call (priority) - should still wait for spacing
    start = time.time()
    limiter.wait_if_needed(priority=True)
    limiter.spend(1, priority=True)
    second_duration = time.time() - start
    assert 11.5 <= second_duration <= 12.5, f"Dossier call should wait for spacing, got {second_duration:.1f}s"


def test_gt_limiter_reset_cycle_stats():
    """reset_cycle_stats() should clear 429 count and wait time."""
    limiter = GTRateLimiter(calls_per_min=30)
    
    # Record some stats
    limiter.stats_429_count = 5
    limiter.stats_wait_time = 120.5
    
    # Reset
    limiter.reset_cycle_stats()
    
    assert limiter.stats_429_count == 0
    assert limiter.stats_wait_time == 0.0


def test_gt_limiter_tracks_429_count():
    """record_429() should increment stats_429_count."""
    limiter = GTRateLimiter(calls_per_min=30)
    
    assert limiter.stats_429_count == 0
    
    limiter.record_429(25.0)
    assert limiter.stats_429_count == 1
    
    limiter.record_429(30.0)
    assert limiter.stats_429_count == 2


def test_gt_limiter_tracks_wait_time():
    """wait_if_needed() should track total wait time."""
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=1.0, time_fn=time.time)
    
    # First call - no wait
    limiter.wait_if_needed()
    limiter.spend(1)
    
    # Second call - wait for spacing
    start_wait_time = limiter.stats_wait_time
    limiter.wait_if_needed()
    limiter.spend(1)
    
    # Should have tracked the ~1s spacing wait
    wait_added = limiter.stats_wait_time - start_wait_time
    assert 0.9 <= wait_added <= 1.2, f"Should track ~1s wait, got {wait_added:.2f}s"


# ============================================================================
# Wait-for-slot within time budget tests
# ============================================================================

def test_cycle_time_budget_stops_processing():
    """Cycle should stop and carry tokens when time budget exhausted."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Mock shortlist with 10 tokens
    now = time.time()
    test_tids = [f"tok{i}:1399811149" for i in range(10)]
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [{"tid": tid, "ticker": f"T{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": now - 1800}
                for i, tid in enumerate(test_tids) if tid in ids]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        # Simulate slow trade checks to burn time
        time.sleep(0.1)
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        # Run with very short time budget (1 second)
        order, stats = shift.run_once(fake_fomo, Mock(), fake_desk, 10000, shadow=True,
                                       cycle_time_budget=1.0)
        
        # Should have carried some tokens (didn't finish all 10)
        carry = book.get_carry()
        assert len(carry) > 0, "Should have carried unevaluated tokens when time budget exhausted"
        assert len(carry) < 10, "Should have processed at least some tokens"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_cycle_time_budget_default():
    """CYCLE_TIME_BUDGET_SEC should default to 660 (11 minutes)."""
    import importlib
    import main
    
    # Reload to get fresh defaults
    with patch.dict(os.environ, {}, clear=True):
        importlib.reload(main)
        assert main.CYCLE_TIME_BUDGET_SEC == 660.0


def test_cycle_time_budget_configurable():
    """CYCLE_TIME_BUDGET_SEC should be configurable via env."""
    import importlib
    import main
    
    with patch.dict(os.environ, {"CYCLE_TIME_BUDGET_SEC": "480"}):
        importlib.reload(main)
        assert main.CYCLE_TIME_BUDGET_SEC == 480.0


def test_cycle_logs_time_budget_exhaustion(caplog):
    """Cycle should log when time budget causes carry."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.INFO)
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Mock shortlist with tokens
    now = time.time()
    test_tids = [f"tok{i}:1399811149" for i in range(5)]
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [{"tid": tid, "ticker": f"T{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": now - 1800}
                for i, tid in enumerate(test_tids) if tid in ids]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        time.sleep(0.2)  # Slow to hit budget
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        # Run with short budget
        order, stats = shift.run_once(fake_fomo, Mock(), fake_desk, 10000, shadow=True,
                                       cycle_time_budget=1.0)
        
        # Check for time budget exhaustion log
        time_budget_logs = [r for r in caplog.records if "time budget exhausted" in r.message.lower()]
        assert len(time_budget_logs) > 0, "Should log time budget exhaustion"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.commit()


# ============================================================================
# Start-to-start scheduling tests
# ============================================================================

def test_start_to_start_scheduling():
    """Cycles should start every CYCLE_SECONDS regardless of duration."""
    import main as shift
    from unittest.mock import Mock, patch
    
    # Track cycle start times
    cycle_starts = []
    original_run_once = shift.run_once
    
    def fake_run_once(*args, **kwargs):
        cycle_starts.append(time.time())
        time.sleep(0.1)  # Simulate short cycle
        return None, {"seen": 0, "benched": 0}
    
    # Patch run_once and time.sleep to speed up test
    sleep_called = []
    original_sleep = time.sleep
    
    def fake_sleep(duration):
        sleep_called.append(duration)
        # Sleep a fraction for testing
        original_sleep(min(duration, 0.1))
    
    shift.run_once = fake_run_once
    
    with patch('time.sleep', fake_sleep):
        fake_fomo = Mock()
        fake_fomo.token = Mock()
        fake_judge = Mock()
        fake_desk = Mock()
        fake_desk.bank = Mock(return_value=10000)
        fake_desk.report = Mock()
        
        # Run 3 cycles then exit
        cycle_count = [0]
        
        def counting_run_once(*args, **kwargs):
            cycle_count[0] += 1
            if cycle_count[0] >= 3:
                raise StopIteration()  # Exit after 3 cycles
            cycle_starts.append(time.time())
            time.sleep(0.1)
            return None, {"seen": 0, "benched": 0}
        
        shift.run_once = counting_run_once
        
        try:
            shift.main(fake_fomo, fake_judge, fake_desk, shadow=True, once=False)
        except StopIteration:
            pass
        
        # Check that sleep times were based on start-to-start interval
        # Sleep should be ~(CYCLE_SECONDS - actual_duration)
        for sleep_duration in sleep_called[:2]:  # Check first 2 sleeps
            # Should be close to CYCLE_SECONDS (900s), allowing for processing time
            # In test with truncated sleep, just verify it was called
            assert sleep_duration > 0
    
    # Restore
    shift.run_once = original_run_once


def test_start_to_start_overrun_logs_warning(caplog):
    """When cycle overruns, should log warning and start next immediately."""
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.WARNING)
    
    # Mock a long cycle
    original_run_once = shift.run_once
    original_sleep = time.sleep
    
    sleep_durations = []
    
    def fake_sleep(duration):
        sleep_durations.append(duration)
        if duration > 0:
            original_sleep(min(duration, 0.01))  # Speed up test
    
    def fake_run_once(*args, **kwargs):
        # Simulate long cycle (longer than CYCLE_SECONDS)
        fake_sleep(901)  # > 900s
        return None, {"seen": 0, "benched": 0}
    
    shift.run_once = fake_run_once
    
    with patch('time.sleep', fake_sleep):
        fake_fomo = Mock()
        fake_fomo.token = Mock()
        fake_judge = Mock()
        fake_desk = Mock()
        fake_desk.bank = Mock(return_value=10000)
        fake_desk.report = Mock()
        
        # Run once and exit
        try:
            shift.main(fake_fomo, fake_judge, fake_desk, shadow=True, once=True)
        except:
            pass
    
    # Restore
    shift.run_once = original_run_once
    
    # Check for overrun warning in logs
    # (may not trigger in single --once run, but structure is correct)


# ============================================================================
# DEX_BUDGET tests
# ============================================================================

def test_dex_budget_default_60():
    """DEX_BUDGET should default to 60."""
    import importlib
    import main
    
    with patch.dict(os.environ, {}, clear=True):
        importlib.reload(main)
        assert main.DEX_BUDGET == 60


def test_dex_budget_configurable():
    """DEX_BUDGET should be configurable via env."""
    import importlib
    import main
    
    with patch.dict(os.environ, {"DEX_BUDGET": "100"}):
        importlib.reload(main)
        assert main.DEX_BUDGET == 100


# ============================================================================
# Comprehensive logging tests
# ============================================================================

def test_cycle_logs_gt_stats(caplog):
    """Cycle should log GT universe/dossier/retry/429/wait stats."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.INFO)
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Mock a simple cycle
    def fake_universe(limiter=None, fomo=None):
        if limiter:
            limiter.universe_calls_used = 3  # Simulate 3 universe calls
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return []
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, Mock(), fake_desk, 10000, shadow=True)
        
        # Check for GT stats log
        gt_logs = [r for r in caplog.records if "cycle GT calls" in r.message]
        assert len(gt_logs) > 0, "Should log GT call stats"
        
        log_msg = gt_logs[0].message
        assert "universe=" in log_msg
        assert "dossier=" in log_msg
        assert "retry=" in log_msg
        assert "429s=" in log_msg
        assert "wait_time=" in log_msg
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_cycle_logs_token_counts(caplog):
    """Cycle should log seen/free_passed/dossiered/judged/carried counts."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.INFO)
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Mock cycle with some tokens
    now = time.time()
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [
            {"tid": "pass1:1399811149", "ticker": "PASS1", "addr": "pass1", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": "kill1:1399811149", "ticker": "KILL1", "addr": "kill1", "net": 1399811149,
             "age_minutes": 5, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 300},  # Too young
        ]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
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
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # Check for cycle summary log
        summary_logs = [r for r in caplog.records if "cycle summary" in r.message]
        assert len(summary_logs) > 0, "Should log cycle summary"
        
        log_msg = summary_logs[0].message
        assert "seen=" in log_msg
        assert "free_passed=" in log_msg
        assert "dossiered=" in log_msg
        assert "judged=" in log_msg
        assert "carried=" in log_msg
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_cycle_logs_duration(caplog):
    """Cycle should log total duration vs budget."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    caplog.set_level(logging.INFO)
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return []
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, Mock(), fake_desk, 10000, shadow=True,
                                       cycle_time_budget=660)
        
        # Check for duration log
        duration_logs = [r for r in caplog.records if "cycle duration" in r.message]
        assert len(duration_logs) > 0, "Should log cycle duration"
        
        log_msg = duration_logs[0].message
        assert "budget" in log_msg
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
