"""Pre-GT wallet-concentration checks and the folded log/label/chain-id fixes.

Deterministic: fake clocks, no network.
"""
import logging
from unittest.mock import Mock, patch

import pytest

import book
import collect
import evm_holders
import main as shift
from collect import GTRateLimiter
from filter import chain_kill, concentration_kill


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def _sol_token(**over):
    row = {
        "tid": "solmint:1399811149",
        "ticker": "SOLT",
        "addr": "solmint",
        "net": 1399811149,
        "age_minutes": 30,
        "holder_count": 200,
        "mcap_usd": 300_000,
        "liquidity_usd": 48_000,
        "volume_h24": 100_000,
    }
    row.update(over)
    return row


def _evm_token(net=56, **over):
    row = {
        "tid": f"0xcccccccccccccccccccccccccccccccccccccccc:{net}",
        "ticker": "EVMT",
        "addr": "0xcccccccccccccccccccccccccccccccccccccccc",
        "net": net,
        "age_minutes": 30,
        "holder_count": 200,
        "pair_address": None,
    }
    row.update(over)
    return row


def _pass_sol_wallet(mint):
    return {"top_wallet": 0.02, "top_10": 0.30, "pools_excluded": True}, True, None


def _whale_sol_wallet(mint):
    return {"top_wallet": 0.20, "top_10": 0.40, "pools_excluded": True}, True, None


def _top10_sol_wallet(mint):
    return {"top_wallet": 0.03, "top_10": 0.70, "pools_excluded": True}, True, None


def _honeypot_whale(_url=None, **kwargs):
    return Mock(status_code=200, json=lambda: {
        "totalSupply": 1_000_000_000,
        "holders": [
            {"address": "0x2222222222222222222222222222222222222222",
             "balance": 400_000_000, "isContract": False},
        ],
    })


def _honeypot_pass(_url=None, **kwargs):
    return Mock(status_code=200, json=lambda: {
        "totalSupply": 1_000_000_000,
        "holders": [
            {"address": "0x2222222222222222222222222222222222222222",
             "balance": 20_000_000, "isContract": False},
        ],
    })


def _gt_info_ok(holder_count=200):
    return Mock(status_code=200, json=lambda: {
        "data": {"attributes": {
            "holders": {"count": holder_count},
            "mint_authority": "no",
            "freeze_authority": "no",
            "is_honeypot": False,
        }}
    })


def _is_gt_info(url) -> bool:
    u = url if isinstance(url, str) else ""
    return "geckoterminal.com" in u and "/tokens/" in u and "/info" in u


def _route_get(gt_info_calls, evm_handler, gt_handler=None):
    def fake_get(url, **kwargs):
        if _is_gt_info(url):
            gt_info_calls.append(url)
            if gt_handler:
                return gt_handler(url, **kwargs)
            return _gt_info_ok()
        return evm_handler(url, **kwargs)
    return fake_get


# ---------------------------------------------------------------------------
# Core: concentration kill must not spend GT /info
# ---------------------------------------------------------------------------

def test_evm_top_wallet_kill_skips_gt_info(isolate_evm_state):
    """A top_wallet chain kill must not call GT /tokens/{addr}/info or spend a slot."""
    gt_info = []
    limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0, sleep_fn=lambda d: None)

    with patch("requests.get", side_effect=_route_get(gt_info, _honeypot_whale)):
        d = collect.dossier(_evm_token(), limiter=limiter)

    assert chain_kill(d) == "top_wallet"
    assert d["top_wallet_percent"] > 0.05
    assert gt_info == []
    assert len(limiter.calls) == 0


def test_solana_top_wallet_kill_skips_gt_info(monkeypatch):
    gt_info = []
    limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0, sleep_fn=lambda d: None)
    monkeypatch.setattr(collect, "sol_top_wallet", _whale_sol_wallet)

    def boom(url, **kwargs):
        gt_info.append(url)
        raise AssertionError("GT /info must not run after a Solana top_wallet kill")

    with patch("collect.requests.get", side_effect=boom):
        d = collect.dossier(_sol_token(), limiter=limiter)

    assert chain_kill(d) == "top_wallet"
    assert gt_info == []
    assert len(limiter.calls) == 0


def test_solana_top_10_kill_skips_gt_info(monkeypatch):
    gt_info = []
    monkeypatch.setattr(collect, "sol_top_wallet", _top10_sol_wallet)
    with patch("collect.requests.get", side_effect=lambda *a, **k: gt_info.append(a) or _gt_info_ok()):
        d = collect.dossier(_sol_token())
    assert chain_kill(d) == "top_10"
    assert gt_info == []


def test_evm_holders_pending_skips_gt_info(isolate_evm_state):
    gt_info = []

    def evm_429(url, **kwargs):
        return Mock(status_code=429, headers={"Retry-After": "60"})

    limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0, sleep_fn=lambda d: None)
    with patch("requests.get", side_effect=_route_get(gt_info, evm_429)):
        d = collect.dossier(_evm_token(), limiter=limiter)

    assert chain_kill(d) == "holders_pending"
    assert d.get("evm_holder_transient") is True
    assert gt_info == []
    assert len(limiter.calls) == 0


def test_evm_unverified_skips_gt_info(isolate_evm_state):
    gt_info = []

    def evm_empty(url, **kwargs):
        return Mock(status_code=200, json=lambda: {"totalSupply": 0, "holders": []})

    with patch("requests.get", side_effect=_route_get(gt_info, evm_empty)):
        d = collect.dossier(_evm_token())

    assert chain_kill(d) == "top_wallet_unverified"
    assert gt_info == []


def test_holders_kill_still_calls_gt_info(monkeypatch):
    """holder_count stays after GT: a holders kill must still spend /info."""
    gt_info = []
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)

    with patch("requests.get", side_effect=_route_get(gt_info, lambda *a, **k: Mock(status_code=404),
                                                     gt_handler=lambda *a, **k: _gt_info_ok(holder_count=5))):
        d = collect.dossier(_sol_token())

    assert chain_kill(d) == "holders"
    assert len(gt_info) == 1
    assert "/info" in gt_info[0]


def test_concentration_kill_same_reasons_as_chain_kill():
    """Pre-GT subset uses the exact same thresholds and reasons as chain_kill."""
    assert concentration_kill({"chain": "bsc", "top_wallet_percent": 0.20}) == "top_wallet"
    assert chain_kill({"chain": "bsc", "top_wallet_percent": 0.20}) == "top_wallet"
    assert concentration_kill({"chain": "solana", "top_10_percent": 85}) == "top_10"
    assert chain_kill({"chain": "solana", "top_10_percent": 85}) == "top_10"
    pending = {"chain": "bsc", "top_wallet_percent": None, "evm_holder_transient": True}
    assert concentration_kill(pending) == chain_kill(pending) == "holders_pending"
    unverified = {"chain": "base", "top_wallet_percent": None, "evm_holder_transient": False}
    assert concentration_kill(unverified) == chain_kill(unverified) == "top_wallet_unverified"
    # holder_count is NOT a concentration kill
    holders_only = {"chain": "solana", "top_wallet_percent": 0.02, "top_10_percent": 30, "holder_count": 5}
    assert concentration_kill(holders_only) is None
    assert chain_kill(holders_only) == "holders"


def test_wallet_concentration_is_idempotent(monkeypatch):
    calls = []

    def once(mint):
        calls.append(mint)
        return _pass_sol_wallet(mint)

    monkeypatch.setattr(collect, "sol_top_wallet", once)
    t = _sol_token()
    collect.apply_wallet_concentration(t)
    collect.apply_wallet_concentration(t)
    assert calls == ["solmint"]


# ---------------------------------------------------------------------------
# (a) evm_holders 429 log does not claim a sleep
# ---------------------------------------------------------------------------

def test_evm_429_log_says_no_sleep(isolate_evm_state, caplog):
    caplog.set_level(logging.WARNING, logger="evm_holders")
    with patch("evm_holders.requests.get", return_value=Mock(status_code=429, headers={"Retry-After": "60"})):
        result = evm_holders.evm_holder_concentration(56, "0xRATELIM", [], 30, book.DB)
    assert not result.ok
    assert result.error == "rate_limited"
    text = caplog.text
    assert "skipping source for 60s (no sleep)" in text
    assert "backing off" not in text


# ---------------------------------------------------------------------------
# (b) evm_source logged on pass
# ---------------------------------------------------------------------------

def test_evm_pass_logs_evm_source(isolate_evm_state, caplog):
    caplog.set_level(logging.INFO)
    gt_info = []
    with patch("requests.get", side_effect=_route_get(gt_info, _honeypot_pass)):
        d = collect.dossier(_evm_token())
    assert chain_kill(d) is None
    assert d["evm_holder_source"] == "honeypot"
    assert "evm_source=honeypot" in caplog.text
    assert len(gt_info) == 1


def test_run_once_logs_evm_source_on_chain_pass(monkeypatch, caplog):
    book.release()
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()

    tid = "0xpass:56"
    token = {
        "tid": tid, "ticker": "PASS", "addr": "0xpass", "net": 56,
        "age_minutes": 30, "mcap_usd": 300_000, "liquidity_usd": 50_000,
        "volume_h24": 100_000, "holder_count": 200, "price": 0.1, "created": 0,
    }

    def fake_universe(limiter=None, fomo=None):
        return ([tid], {})

    def fake_shortlist(fomo, ids):
        return [dict(token)]

    def fake_trade(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "buys_h6": 200, "sells_h6": 100,
                 "trades_h24": 1000}, "ok")

    def fake_dossier(t, limiter=None, deadline=None):
        return {**t, "chain": "bsc", "top_10_percent": 20, "top_wallet_percent": 0.02,
                "holder_count": 200, "evm_holder_source": "honeypot",
                "x_handle": None, "description": None, "mint_authority": None,
                "freeze_authority": None, "is_honeypot": False,
                "gt_score_details": None, "developer_holding_percentage": None}

    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3},
            "shape": {"type": "choice", "choice": "crowd",
                      "probabilities": {"crowd": 0.8, "one_buyer": 0.1, "fading": 0.05, "too_early": 0.05}},
        }, "usage": {}}

    monkeypatch.setattr(shift, "universe", fake_universe)
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", fake_trade)
    monkeypatch.setattr(shift, "dossier", fake_dossier)
    desk = Mock()
    desk.read_x = Mock(return_value=None)
    desk.write_state = Mock()
    caplog.set_level(logging.INFO)
    shift.run_once(Mock(), fake_judge, desk, 10000, shadow=True)
    assert "reason=pass" in caplog.text
    assert "evm_source=honeypot" in caplog.text


# ---------------------------------------------------------------------------
# (c) Base 8453 must not query BSC 56
# ---------------------------------------------------------------------------

def test_chain_set_base_is_judge_seat_only():
    """CHAIN_SET[8453]='bsc' is the judge question set, not the holder-API chain id."""
    assert shift.CHAIN_SET[8453] == "bsc"
    assert collect.GT_NET[8453] == "base"


def test_base_honeypot_uses_chain_8453_not_56(isolate_evm_state):
    chain_ids = []

    def fake_get(url, **kwargs):
        params = kwargs.get("params") or {}
        if "honeypot" in str(url) or "TopHolders" in str(url):
            chain_ids.append(params.get("chainID"))
            return _honeypot_pass()
        return Mock(status_code=404)

    with patch("evm_holders.requests.get", side_effect=fake_get):
        evm_holders.evm_holder_concentration(
            8453, "0xcccccccccccccccccccccccccccccccccccccccc", [], 30, book.DB
        )
    assert chain_ids == [8453]
    assert 56 not in chain_ids


def test_base_goplus_uses_chain_8453_not_56(isolate_evm_state):
    goplus_urls = []

    def fake_get(url, **kwargs):
        u = str(url)
        if "honeypot" in u:
            return Mock(status_code=500)
        if "gopluslabs" in u:
            goplus_urls.append(u)
            token = "0xcccccccccccccccccccccccccccccccccccccccc"
            return Mock(status_code=200, json=lambda: {
                "code": 1,
                "result": {token: {
                    "holders": [
                        {"address": "0x2222222222222222222222222222222222222222",
                         "percent": "0.02", "is_contract": 0},
                    ],
                    "dex": [],
                }}
            })
        return Mock(status_code=404)

    with patch("evm_holders.requests.get", side_effect=fake_get), \
         patch("evm_holders.requests.post", return_value=Mock(status_code=200, json=lambda: {"result": None})):
        evm_holders.evm_holder_concentration(
            8453, "0xcccccccccccccccccccccccccccccccccccccccc", [], 150, book.DB
        )
    assert goplus_urls, "GoPlus should have been called after Honeypot 500"
    assert all("/8453" in u for u in goplus_urls)
    assert all("/56" not in u.split("token_security")[-1] for u in goplus_urls)


def test_base_multicall_uses_base_rpc_not_bsc(isolate_evm_state):
    posts = []

    def fake_post(url, **kwargs):
        posts.append(url)
        return Mock(status_code=200, json=lambda: {"result": None})

    holders = [("0x2222222222222222222222222222222222222222", 1_000, 1.0)]
    with patch("evm_holders.requests.post", side_effect=fake_post):
        evm_holders._check_pools_via_multicall(
            holders, "0xcccccccccccccccccccccccccccccccccccccccc", 8453, deadline=1e18
        )
    assert posts
    assert all("bsc-dataseed" not in (u or "") and "binance.org" not in (u or "") for u in posts)
    assert any("base.org" in (u or "") for u in posts)


def test_base_dossier_honeypot_chain_id_is_8453(isolate_evm_state):
    """Dossier path for a Base token must query Honeypot with chainID=8453."""
    chain_ids = []
    gt_info = []

    def evm_get(url, **kwargs):
        params = kwargs.get("params") or {}
        if "honeypot" in str(url):
            chain_ids.append(params.get("chainID"))
            return _honeypot_whale()
        return Mock(status_code=404)

    with patch("requests.get", side_effect=_route_get(gt_info, evm_get)):
        d = collect.dossier(_evm_token(net=8453, ticker="BASEW"))

    assert d["chain"] == "base"
    assert chain_ids == [8453]
    assert chain_kill(d) == "top_wallet"
    assert gt_info == []


# ---------------------------------------------------------------------------
# (d) pacing wait log vs 429 backoff
# ---------------------------------------------------------------------------

def test_dossier_wait_log_pacing_vs_429_backoff(monkeypatch, caplog):
    clock = FakeClock(1000.0)
    limiter = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0,
                            time_fn=clock.time, sleep_fn=clock.sleep)
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    caplog.set_level(logging.INFO, logger="collect")

    def fake_get(url, **kwargs):
        return _gt_info_ok()

    with patch("collect.requests.get", side_effect=fake_get):
        collect.dossier(_sol_token(addr="a1", tid="a1:1399811149", ticker="A1"), limiter=limiter)
        caplog.clear()
        collect.dossier(_sol_token(addr="a2", tid="a2:1399811149", ticker="A2"), limiter=limiter)

    pacing_lines = [r.message for r in caplog.records if "waited" in r.message and "dossier for" in r.message]
    assert pacing_lines, caplog.text
    assert any("GT pacing" in m for m in pacing_lines)
    assert all("429 backoff" not in m for m in pacing_lines)
    assert all("rate limit/429" not in m for m in pacing_lines)

    limiter.record_429(8.0)
    caplog.clear()
    with patch("collect.requests.get", side_effect=fake_get):
        collect.dossier(_sol_token(addr="a3", tid="a3:1399811149", ticker="A3"), limiter=limiter)
    backoff_lines = [r.message for r in caplog.records if "waited" in r.message and "dossier for" in r.message]
    assert backoff_lines, caplog.text
    assert any("GT 429 backoff" in m for m in backoff_lines)
    assert all("rate limit/429" not in m for m in backoff_lines)


# ---------------------------------------------------------------------------
# (e) briefing labels
# ---------------------------------------------------------------------------

def test_briefing_labels_reached_vs_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import cycle_history
    cycle_history.reset()
    empty = cycle_history.build_briefing()
    assert "reached judge 0" in empty["markdown"]
    assert "No tokens passed judge/picks in the window." in empty["markdown"]
    assert "No tokens judged in the window." not in empty["markdown"]
    assert "## Passed judge/picks" in empty["markdown"]
    assert "| chain | reached judge |" in empty["markdown"]
    cycle_history.reset()
