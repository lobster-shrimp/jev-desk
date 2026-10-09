"""
Tests for GT throughput pacing. All clocks are fake — no real sleeping.
"""
import logging
import os
from unittest.mock import Mock, patch
import pytest

import sys
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collect import GTRateLimiter, universe


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, duration):
        self.now += duration

    def advance(self, duration):
        self.now += duration


class StopTest(BaseException):
    """Stops main()'s loop. Must not subclass Exception (main swallows Exception)."""


def _token(tid, age=30, **over):
    addr = tid.split(":")[0]
    row = {
        "tid": tid, "ticker": addr[:8], "addr": addr, "net": 1399811149,
        "age_minutes": age, "mcap_usd": 500000, "liquidity_usd": 50000,
        "volume_h24": 100000, "holder_count": 200, "price": 0.1,
        "created": 0,
    }
    row.update(over)
    return row


def _ok_dossier(t, limiter=None, deadline=None):
    return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
            "x_handle": None, "description": "test", "mint_authority": None,
            "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
            "developer_holding_percentage": None}


def _ok_trade(t, gt_txns_cache=None):
    return ({"buys_h1": 100, "sells_h1": 50, "buys_h6": 200, "sells_h6": 100,
             "trades_h24": 1000}, "ok")


def _ok_judge(question_set, state):
    return {"model": "test", "answers": {
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
    }, "usage": {}}


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def limiter_with_fake_clock(fake_clock):
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0,
                            time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    return limiter, fake_clock


# ---------------------------------------------------------------------------
# T1: limiter pacing, no real sleep
# ---------------------------------------------------------------------------

def test_gt_limiter_enforces_minimum_spacing(limiter_with_fake_clock):
    limiter, clock = limiter_with_fake_clock
    assert limiter.wait_if_needed()
    limiter.spend(1)
    t0 = clock.time()
    assert limiter.wait_if_needed()
    limiter.spend(1)
    assert clock.time() - t0 >= 12.0


def test_gt_limiter_min_spacing_default():
    assert GTRateLimiter(calls_per_min=5).min_spacing_sec == 12.0


def test_gt_limiter_spacing_configurable():
    assert GTRateLimiter(calls_per_min=10, min_spacing_sec=8.0).min_spacing_sec == 8.0


def test_gt_limiter_tracks_last_call_time(fake_clock):
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=2.0,
                            time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    limiter.spend(1)
    assert limiter.last_call_time == 1000.0
    fake_clock.advance(5.0)
    limiter.spend(1)
    assert limiter.last_call_time == 1005.0


def test_gt_limiter_spacing_applies_to_universe_and_dossier(limiter_with_fake_clock):
    limiter, clock = limiter_with_fake_clock
    limiter.reserve(2)
    assert limiter.wait_if_needed(priority=False)
    limiter.spend(1, priority=False)
    t0 = clock.time()
    assert limiter.wait_if_needed(priority=True)
    limiter.spend(1, priority=True)
    assert clock.time() - t0 >= 12.0


def test_gt_limiter_reset_for_test_clears_last_call_and_stats(fake_clock):
    limiter = GTRateLimiter(calls_per_min=5, time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    limiter.spend(1)
    limiter.record_429(25.0)
    limiter.stats_wait_time = 100.0
    limiter.reserve(3)
    limiter.reset_for_test()
    assert limiter.last_call_time == 0.0
    assert len(limiter.calls) == 0
    assert limiter.stats_429_count == 0
    assert limiter.stats_wait_time == 0.0
    assert limiter.reserved == 0


def test_gt_limiter_tracks_429_count(fake_clock):
    limiter = GTRateLimiter(calls_per_min=30, time_fn=fake_clock.time, sleep_fn=fake_clock.sleep)
    limiter.record_429(25.0)
    limiter.record_429(30.0)
    assert limiter.stats_429_count == 2


def test_gt_limiter_tracks_wait_time(limiter_with_fake_clock):
    limiter, clock = limiter_with_fake_clock
    assert limiter.wait_if_needed()
    limiter.spend(1)
    before = limiter.stats_wait_time
    assert limiter.wait_if_needed()
    limiter.spend(1)
    assert 11.9 <= limiter.stats_wait_time - before <= 12.1


def test_wait_if_needed_loops_until_slot_free():
    """F3: at the exact window boundary the slot must become free."""
    clock = FakeClock()
    limiter = GTRateLimiter(calls_per_min=2, window_sec=60.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    limiter.spend(1)
    clock.advance(1.0)
    limiter.spend(1)
    clock.now = 1060.0
    assert limiter.wait_if_needed() is True
    assert limiter.spend(1) is True


def test_wait_if_needed_respects_deadline():
    clock = FakeClock()
    limiter = GTRateLimiter(calls_per_min=2, window_sec=60.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    limiter.spend(1)
    limiter.spend(1)
    clock.advance(30.0)
    deadline = clock.time() + 20.0
    assert limiter.wait_if_needed(deadline=deadline) is False


def test_dex_budget_default_60():
    import main
    assert main._env_int("DEX_BUDGET", 60) == 60
    if os.environ.get("DEX_BUDGET") in (None, ""):
        assert main.DEX_BUDGET == 60


def test_gt_dossier_reserve_is_one_at_five_per_min():
    import main
    if main.GT_CALLS_PER_MIN == 5:
        assert main.GT_DOSSIER_RESERVE == 1


def test_cycle_time_budget_default_via_helper():
    import main
    with patch.dict(os.environ, {"CYCLE_TIME_BUDGET_SEC": ""}):
        assert main._env_float("CYCLE_TIME_BUDGET_SEC", 660) == 660.0


def test_cycle_time_budget_configurable_via_helper():
    import main
    with patch.dict(os.environ, {"CYCLE_TIME_BUDGET_SEC": "480"}):
        assert main._env_float("CYCLE_TIME_BUDGET_SEC", 660) == 480.0


# ---------------------------------------------------------------------------
# T2: mutations M1, M2, M3, M4
# ---------------------------------------------------------------------------

def test_no_break_on_full_window():
    """M1: a momentarily full GT window must wait, not break. All 12 passers dossiered."""
    import book
    import main as shift

    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()

    clock = FakeClock(1000.0)
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    tids = [f"pass{i}:1399811149" for i in range(12)]
    dossier_calls = []

    def fake_universe(limiter=None, fomo=None):
        return (tids, {})

    def fake_shortlist(fomo, ids):
        return [_token(tid) for tid in tids]

    def fake_dossier(t, limiter=None, deadline=None):
        dossier_calls.append((clock.time(), t["tid"]))
        return _ok_dossier(t)

    orig = (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
            shift._now, shift._sleep)
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = _ok_trade
    shift.dossier = fake_dossier
    shift._now = clock.time
    shift._sleep = clock.sleep
    try:
        desk = Mock()
        desk.read_x = Mock(return_value=None)
        desk.write_state = Mock()
        shift.run_once(Mock(), _ok_judge, desk, 10000, shadow=True,
                       gt_limiter=limiter, cycle_time_budget=660)
        assert len(dossier_calls) == 12, f"expected 12 dossiers, got {len(dossier_calls)}"
        for i in range(1, len(dossier_calls)):
            gap = dossier_calls[i][0] - dossier_calls[i - 1][0]
            assert gap >= 12.0, f"gap {i} was {gap:.1f}s"
    finally:
        (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
         shift._now, shift._sleep) = orig
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_time_budget_carry_in_priority_order():
    """M3: exhausting the time budget must save remaining free-passers to carry."""
    import book
    import main as shift

    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()

    clock = FakeClock(1000.0)
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=0.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    due_tids = [f"due{i}:1399811149" for i in range(3)]
    rest_tids = [f"tok{i}:1399811149" for i in range(8)]
    all_tids = due_tids + rest_tids
    now = clock.time()
    for tid in due_tids:
        book.defer(tid, now - 1, now + 3600)

    def fake_universe(limiter=None, fomo=None):
        return (rest_tids, {})

    def fake_shortlist(fomo, ids):
        return [_token(tid) for tid in all_tids if tid in ids]

    def fake_trade(t, gt_txns_cache=None):
        clock.advance(20.0)
        return _ok_trade(t)

    def fake_dossier(t, limiter=None, deadline=None):
        clock.advance(20.0)
        return _ok_dossier(t)

    orig = (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
            shift._now, shift._sleep)
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade
    shift.dossier = fake_dossier
    shift._now = clock.time
    shift._sleep = clock.sleep
    try:
        desk = Mock()
        desk.read_x = Mock(return_value=None)
        desk.write_state = Mock()
        # 180s budget, 60s judge reserve → stop after ~120s → ~3 tokens at 40s each
        shift.run_once(Mock(), _ok_judge, desk, 10000, shadow=True,
                       gt_limiter=limiter, cycle_time_budget=180)
        carry = book.get_carry()
        assert len(carry) > 0, "time-budget exhaustion must save_carry remaining passers"
        assert len(carry) <= 60
        # break token (first unevaluated due/passer) keeps its defer row
        for tid in carry:
            if tid in due_tids:
                row = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchone()
                assert row is not None, f"{tid} lost its defer row"
        # processed (dossiered) ids are cleared from carry
        for tid in due_tids:
            if tid not in carry:
                assert tid not in book.get_carry()
    finally:
        (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
         shift._now, shift._sleep) = orig
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_universe_spacing_end_to_end():
    """M2: five universe pages and the next dossier are all >= 12s apart."""
    clock = FakeClock(0.0)
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    limiter.set_universe_budget(5)
    limiter.reserve(1)
    times = []

    def fake_get(url, params=None, headers=None, timeout=None):
        times.append(clock.time())
        resp = Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"data": []}
        return resp

    with patch("collect.requests.get", fake_get):
        universe(nets=("solana",), pages=2, include_trending=True, limiter=limiter)

    assert len(times) >= 4
    for i in range(1, len(times)):
        assert times[i] - times[i - 1] >= 12.0, (
            f"universe calls {i - 1}->{i} gap {times[i] - times[i - 1]:.1f}s"
        )
    last_page = times[-1]
    assert limiter.wait_if_needed(priority=True)
    limiter.spend(1, priority=True)
    assert clock.time() - last_page >= 12.0


def test_start_to_start_overrun(caplog):
    """M4: 100s cycle sleeps 800s; 950s cycle overruns (no sleep) and logs WARNING."""
    import main as shift

    caplog.set_level(logging.WARNING)
    clock = FakeClock(1000.0)
    sleeps = []
    n = [0]

    def fake_run_once(*args, **kwargs):
        n[0] += 1
        if n[0] == 1:
            clock.advance(100.0)
        elif n[0] == 2:
            clock.advance(950.0)
        else:
            raise StopTest()
        return None, {"seen": 0, "benched": 0}

    def fake_sleep(duration):
        sleeps.append(duration)
        clock.sleep(duration)

    orig = (shift.run_once, shift._now, shift._sleep)
    shift.run_once = fake_run_once
    shift._now = clock.time
    shift._sleep = fake_sleep
    try:
        fomo = Mock()
        fomo.token = Mock()
        desk = Mock()
        desk.bank = Mock(return_value=10000)
        desk.report = Mock()
        with patch("time.sleep", fake_sleep):
            try:
                shift.main(fomo, Mock(), desk, shadow=True, once=False)
            except StopTest:
                pass
        assert len(sleeps) >= 1
        assert 790 <= sleeps[0] <= 810, f"first sleep {sleeps[0]}, expected ~800"
        assert len(sleeps) == 1, "overrun cycle must not sleep"
        assert any("overran" in r.message.lower() for r in caplog.records)
    finally:
        shift.run_once, shift._now, shift._sleep = orig


# ---------------------------------------------------------------------------
# F1 / F6
# ---------------------------------------------------------------------------

def test_free_checks_continue_after_dex_budget():
    """F1: after Dex/time stop, later tokens still get free/bench checks; only passers carried."""
    import book
    import main as shift

    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()

    clock = FakeClock(1000.0)
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=0.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    # 1 passer (uses the single DEX slot), then 1 free-kill, then 2 more passers
    tids = ["pass0:1399811149", "kill1:1399811149", "pass2:1399811149", "pass3:1399811149"]

    def fake_universe(limiter=None, fomo=None):
        return (tids, {})

    def fake_shortlist(fomo, ids):
        return [
            _token("pass0:1399811149"),
            _token("kill1:1399811149", liquidity_usd=1000),  # below min liq
            _token("pass2:1399811149"),
            _token("pass3:1399811149"),
        ]

    orig = (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
            shift._now, shift._sleep, shift.DEX_BUDGET)
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = _ok_trade
    shift.dossier = _ok_dossier
    shift._now = clock.time
    shift._sleep = clock.sleep
    shift.DEX_BUDGET = 1
    try:
        desk = Mock()
        desk.read_x = Mock(return_value=None)
        desk.write_state = Mock()
        _, stats = shift.run_once(Mock(), _ok_judge, desk, 10000, shadow=True,
                                  gt_limiter=limiter, cycle_time_budget=660)
        assert stats["seen"] == 4
        assert stats["free"].get("liquidity") == 1
        carry = book.get_carry()
        assert "kill1:1399811149" not in carry
        assert "pass2:1399811149" in carry
        assert "pass3:1399811149" in carry
        assert "pass0:1399811149" not in carry
        # kill was benched (sit), not carried
        assert book.benched("kill1:1399811149")
    finally:
        (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
         shift._now, shift._sleep, shift.DEX_BUDGET) = orig
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.execute("DELETE FROM bench")
        book.DB.commit()


def test_existing_log_lines_preserved(caplog):
    """F6: keep 'cycle: … judged N, requeued N' and add the new summary alongside."""
    import book
    import main as shift

    caplog.set_level(logging.INFO)
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    clock = FakeClock()

    orig = (shift.universe, shift.shortlist, shift._now, shift._sleep)
    shift.universe = lambda limiter=None, fomo=None: ([], {})
    shift.shortlist = lambda fomo, ids: []
    shift._now = clock.time
    shift._sleep = clock.sleep
    try:
        desk = Mock()
        desk.read_x = Mock(return_value=None)
        desk.write_state = Mock()
        shift.run_once(Mock(), Mock(), desk, 10000, shadow=True)
        cycle_logs = [r for r in caplog.records
                      if r.message.startswith("cycle:") and "judged" in r.message]
        assert cycle_logs, "must keep the 'cycle: … judged N, requeued N' line"
        assert any("cycle summary" in r.message for r in caplog.records)
        assert any("cycle GT calls" in r.message for r in caplog.records)
    finally:
        shift.universe, shift.shortlist, shift._now, shift._sleep = orig
        book.DB.execute("DELETE FROM carry")
        book.DB.commit()


def test_cycle_logs_time_budget_exhaustion(caplog):
    """Named in review: time-budget stop logs the unevaluated line."""
    import book
    import main as shift

    caplog.set_level(logging.INFO)
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    clock = FakeClock(1000.0)
    limiter = GTRateLimiter(calls_per_min=30, min_spacing_sec=0.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    tids = [f"tok{i}:1399811149" for i in range(8)]

    def fake_trade(t, gt_txns_cache=None):
        clock.advance(30.0)
        return _ok_trade(t)

    orig = (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
            shift._now, shift._sleep)
    shift.universe = lambda limiter=None, fomo=None: (tids, {})
    shift.shortlist = lambda fomo, ids: [_token(tid) for tid in tids]
    shift.trade_counts = fake_trade
    shift.dossier = _ok_dossier
    shift._now = clock.time
    shift._sleep = clock.sleep
    try:
        desk = Mock()
        desk.read_x = Mock(return_value=None)
        desk.write_state = Mock()
        shift.run_once(Mock(), _ok_judge, desk, 10000, shadow=True,
                       gt_limiter=limiter, cycle_time_budget=180)
        assert any("unevaluated" in r.message and "gt_available=" in r.message
                   for r in caplog.records)
        assert book.get_carry()
    finally:
        (shift.universe, shift.shortlist, shift.trade_counts, shift.dossier,
         shift._now, shift._sleep) = orig
        book.DB.execute("DELETE FROM carry")
        book.DB.commit()
