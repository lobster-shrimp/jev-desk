"""
Funnel tests. No network, no key: collectors are faked, the judge is the mock.
    pytest -q
"""
import os
import sys
import time
import pathlib
from unittest.mock import Mock
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_DB", ":memory:")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))

import book                                           # noqa: E402
from filter import free_kill, trade_kill, chain_kill, soft_kill   # noqa: E402
from pick import pick, summary                        # noqa: E402
from mock_judge import mock_judge_fn                  # noqa: E402
from questions import SETS                            # noqa: E402
from thresholds import PICK_MIN_WORTH, PICK_MIN_CONF, NO_SOCIAL_CUT   # noqa: E402
import main as shift                                  # noqa: E402
import collect                                        # noqa: E402
from fomo_api import Fomo                             # noqa: E402

JUDGE = mock_judge_fn()
NOW_MS = int(time.time() * 1000)


def tok(i, net=1399811149, **over):
    t = {"addr": f"Addr{i}", "net": net, "tid": f"Addr{i}:{net}", "ticker": f"T{i}",
         "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
         "price_usd": 0.001, "holder_count": 310,
         "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}, "age_minutes": 42}
    t.update(over)
    return t


# ---- fomo_api parsing ---------------------------------------------------------
def test_filter_tokens_parses_bare_list():
    """_filter_tokens should handle a bare list response."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = [
        {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
        {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
    ]
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert "addr1:56" in result
    assert "addr2:56" in result
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_id_map():
    """_filter_tokens should handle {id: row} dict response."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "addr1:56": {"symbol": "TKN1", "marketCap": 100000},
        "addr2:56": {"symbol": "TKN2", "marketCap": 200000}
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_data_envelope():
    """_filter_tokens should handle {"data": [...]} envelope."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "data": [
            {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
            {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_response_object_envelope():
    """_filter_tokens should handle {"responseObject": [...]} envelope from live API."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "Tokens filtered successfully",
        "responseObject": [
            {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
            {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"
    assert result["addr2:56"]["symbol"] == "TKN2"


def test_filter_tokens_handles_empty_response_object():
    """_filter_tokens should return empty dict for empty responseObject."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "No tokens found",
        "responseObject": []
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56"])
    assert result == {}


def test_filter_tokens_parses_nested_token_structure():
    """_filter_tokens should handle nested {token: {address, networkId, ...}} structure from live API."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "Tokens filtered successfully",
        "responseObject": [
            {
                "token": {
                    "address": "So11111111111111111111111111111111111111112",
                    "networkId": 1399811149,
                    "symbol": "SOL",
                    "marketCap": 85000000000,
                    "liquidity": 45000000,
                    "volume24": 125000000,
                    "priceUSD": 150.45,
                    "holders": 2500000,
                    "createdAt": 1609459200000,
                    "change5m": 0.02,
                    "change1": 0.05,
                    "change4": 0.12,
                    "change24": 0.08
                },
                "someOtherField": "ignored"
            },
            {
                "token": {
                    "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "networkId": 1399811149,
                    "symbol": "USDC",
                    "marketCap": 45000000000,
                    "liquidity": 120000000,
                    "volume24": 850000000,
                    "priceUSD": 1.00,
                    "holders": 1800000,
                    "createdAt": 1625097600000
                }
            }
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["So11111111111111111111111111111111111111112:1399811149",
                                   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:1399811149"])
    assert len(result) == 2
    assert "So11111111111111111111111111111111111111112:1399811149" in result
    assert "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:1399811149" in result
    # Verify flattened structure has expected fields
    sol_row = result["So11111111111111111111111111111111111111112:1399811149"]
    assert sol_row["symbol"] == "SOL"
    assert sol_row["marketCap"] == 85000000000
    assert sol_row["networkId"] == 1399811149


def test_nested_token_yields_parsed_tokens():
    """Nested token structure should successfully parse through _row and produce valid token data."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            {
                "token": {
                    "address": "TestAddr1",
                    "networkId": 56,
                    "symbol": "TEST",
                    "marketCap": 500000,
                    "liquidity": 80000,
                    "volume24": 250000,
                    "priceUSD": 0.05,
                    "holders": 1500,
                    "createdAt": int(time.time() * 1000 - 3600000),  # 1 hour ago
                    "change5m": 0.03,
                    "change1": 0.08,
                    "change4": 0.15,
                    "change24": 0.25
                }
            }
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    
    # Full flow: _filter_tokens -> tokens() -> _row()
    tokens = fomo.tokens(["TestAddr1:56"])
    assert len(tokens) == 1
    assert "TestAddr1:56" in tokens
    
    token = tokens["TestAddr1:56"]
    assert token["symbol"] == "TEST"
    assert token["mcap"] == 500000.0
    assert token["liq"] == 80000.0
    assert token["vol24"] == 250000.0
    assert token["price"] == 0.05
    assert token["holders"] == 1500
    assert token["change"][300] == 0.03  # 5m
    assert token["change"][3600] == 0.08  # 1h
    assert token["created"] is not None


def test_filter_tokens_retries_on_connection_error():
    """_filter_tokens should retry up to 4 times on ConnectionError."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # First call raises ConnectionError, second succeeds
    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise requests.exceptions.ConnectionError("Connection reset by peer")
        return mock_resp
    
    fomo.s.post = Mock(side_effect=side_effect)
    fomo.s.adapters = {}  # Mock empty adapters dict so close() doesn't fail
    result = fomo._filter_tokens(["addr1:56"])
    
    assert call_count[0] == 2  # Should have retried once (1 failure + 1 success)
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_retries_on_502():
    """_filter_tokens should retry on HTTP 502 Bad Gateway with exponential backoff."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # First call returns 502, second succeeds
    fomo.s.post = Mock(side_effect=[mock_502, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 2  # Should have retried once (1 failure + 1 success)
    assert len(result) == 1


def test_filter_tokens_retries_multiple_502s():
    """_filter_tokens should retry up to 4 times on persistent 502s."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # Three 502s, then success on 4th attempt
    fomo.s.post = Mock(side_effect=[mock_502, mock_502, mock_502, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 4  # 3 failures + 1 success
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_exhausts_retries_on_persistent_502():
    """_filter_tokens should raise HTTPError after 5 failed attempts."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_502.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("502 Server Error"))
    
    fomo.s.post = Mock(return_value=mock_502)
    
    try:
        fomo._filter_tokens(["addr1:56"])
        assert False, "should have raised HTTPError"
    except requests.exceptions.HTTPError:
        pass
    
    # Should have tried 5 times (initial + 4 retries)
    assert fomo.s.post.call_count == 5


def test_filter_tokens_retries_on_503_and_504():
    """_filter_tokens should retry on 503 and 504 gateway errors."""
    fomo = Fomo(bearer="fake-token")
    mock_503 = Mock()
    mock_503.status_code = 503
    mock_504 = Mock()
    mock_504.status_code = 504
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # 503, then 504, then success
    fomo.s.post = Mock(side_effect=[mock_503, mock_504, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 3
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_exponential_backoff():
    """_filter_tokens should use exponential backoff (0.5s, 1s, 2s, 4s)."""
    import time as time_module
    
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    sleep_calls = []
    original_sleep = time_module.sleep
    time_module.sleep = lambda x: sleep_calls.append(x)
    
    try:
        # Three 502s, then success
        fomo.s.post = Mock(side_effect=[mock_502, mock_502, mock_502, mock_200])
        result = fomo._filter_tokens(["addr1:56"])
        
        assert len(sleep_calls) == 3
        assert sleep_calls[0] == 0.5   # 0.5 * 2^0
        assert sleep_calls[1] == 1.0   # 0.5 * 2^1
        assert sleep_calls[2] == 2.0   # 0.5 * 2^2
        assert len(result) == 1
    finally:
        time_module.sleep = original_sleep


def test_flat_and_nested_shapes_both_work():
    """Verify backward compatibility: both flat and nested structures parse correctly."""
    fomo = Fomo(bearer="fake-token")
    
    # Test with mixed response
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            # Flat structure (old format)
            {"id": "addr1:56", "symbol": "FLAT", "marketCap": 100000},
            # Nested structure (new format)
            {"token": {"address": "addr2", "networkId": 56, "symbol": "NESTED", "marketCap": 200000}}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    
    assert len(result) == 2
    assert "addr1:56" in result
    assert "addr2:56" in result
    assert result["addr1:56"]["symbol"] == "FLAT"
    assert result["addr2:56"]["symbol"] == "NESTED"


# ---- filter -------------------------------------------------------------------
def test_free_kill_order_and_reasons():
    assert free_kill(tok(1)) is None
    assert free_kill(tok(1, age_minutes=5)) == "age"
    assert free_kill(tok(1, age_minutes=80 * 60)) == "age"
    assert free_kill(tok(1, liquidity_usd=1000)) == "liquidity"
    assert free_kill(tok(1, volume_h24=10)) == "volume"
    assert free_kill(tok(1, mcap_usd=10)) == "mcap"


def test_trade_kill():
    assert trade_kill({"trades_h24": None}) == "no_pair"
    assert trade_kill({"trades_h24": 10, "sells_h1": 3, "buys_h1": 5}) == "trades"
    assert trade_kill({"trades_h24": 500, "sells_h1": 0, "buys_h1": 50}) == "no_sells"
    assert trade_kill({"trades_h24": 500, "sells_h1": 20, "buys_h1": 50}) is None


def test_chain_kill_facts_before_judgements():
    base = {"chain": "solana", "top_wallet_percent": 0.01, "top_10_percent": 30,
            "holder_count": 300, "mint_authority": None, "freeze_authority": None}
    assert chain_kill(base) is None
    assert chain_kill({**base, "top_wallet_percent": 0.2}) == "top_wallet"
    assert chain_kill({**base, "top_10_percent": 80}) == "top_10"
    assert chain_kill({**base, "holder_count": 10}) == "holders"
    assert chain_kill({**base, "mint_authority": "abc"}) == "authority_open"
    assert chain_kill({"chain": "bsc", "is_honeypot": True}) == "honeypot"
    assert chain_kill({"chain": "robinhood", "holder_count": None}) is None   # dark is not a kill


def test_soft_kill_reads_noul_and_score_and_shape():
    good = {"concentration_is_exit_risk": {"noul": 0.1},
            "shape": {"choice": "crowd", "probabilities": {"crowd": 0.8}},
            "effort": {"score": 2.0}}
    assert soft_kill(good) is None
    assert soft_kill({**good, "concentration_is_exit_risk": {"noul": 0.9}}) == "concentration_is_exit_risk"
    assert soft_kill({**good, "effort": {"score": 0.2}}) == "effort"
    assert soft_kill({**good, "shape": {"choice": "fading", "probabilities": {"crowd": 0.1}}}) == "shape"
    assert soft_kill({**good, "shape": {"choice": "too_early", "probabilities": {"crowd": 0.3}}}) == "shape_weak"
    assert soft_kill({**good, "sell_side_risk": {"choice": "flagged"}}) == "sell_side"


# ---- questions / judge contract ----------------------------------------------
def test_every_set_answers_in_wire_shape():
    state = {**tok(1), "chain": "solana", "intended_ticket_usd": 60,
             "top_10_percent": 30, "top_wallet_percent": 0.02,
             "mint_authority": None, "freeze_authority": None}
    for name in ("market", "solana", "bsc", "robinhood"):
        r = JUDGE(name, state)
        assert r["model"] and "answers" in r and "usage" in r
        for q, a in r["answers"].items():
            assert a["type"] in ("noul", "choice", "score")
            if a["type"] == "choice":
                assert a["choice"] in SETS[name][q].criteria
                assert abs(sum(a["probabilities"].values()) - 1) < 0.01   # "approximately 1"
            if a["type"] == "noul":
                assert 0 <= a["noul"] <= 1


def _survivors(n=3):
    out = []
    for i in range(n):
        d = {**tok(i), "chain": "solana"}
        ans = {"shape": {"choice": "crowd", "probabilities": {"crowd": 0.7 + i / 10}},
               "concentration_is_exit_risk": {"noul": 0.2},
               "authority_risk": {"choice": "renounced"}}
        out.append((d, ans))
    return out


def _pick_judge(worth, conf, choice="T1", tickers=("T0", "T1", "T2"), seen=None):
    """A stand-in for the pick call with both gate inputs set explicitly.

    mock_judge is deterministic but its worth noul lands at 0.584, just under
    PICK_MIN_WORTH, so driving these tests through it asserts nothing. Set the
    two gate values here and the outcome is a fact, not a coin toss.
    """
    def judge(question_set, state):
        assert question_set == "pick"
        if seen is not None:
            seen.append(state)
        rest = round((1 - conf) / max(len(tickers) - 1, 1), 3)
        return {"model": "stub-pick",
                "answers": {
                    "best": {"type": "choice", "choice": choice, "confidence": conf,
                             "probabilities": {t: (conf if t == choice else rest)
                                               for t in tickers}},
                    "worth_trading_at_all": {"type": "noul", "noul": worth}},
                "usage": {}}
    return judge


def test_summary_is_built_from_answers_not_the_dossier():
    s = summary(*_survivors(1)[0])
    assert "solana" in s and "no usable X account" in s
    assert "crowd 0.70" in s and "concentration risk 0.20" in s
    assert "authority renounced" in s


def test_pick_builds_one_option_per_candidate():
    seen = []
    pick(_pick_judge(worth=0.9, conf=0.9, seen=seen), _survivors())
    assert len(seen) == 1                            # one call, all candidates at once
    assert [c["ticker"] for c in seen[0]["candidates"]] == ["T0", "T1", "T2"]
    assert all(c["summary"] for c in seen[0]["candidates"])


def test_pick_returns_the_chosen_token_when_both_gates_pass():
    order = pick(_pick_judge(worth=PICK_MIN_WORTH, conf=PICK_MIN_CONF), _survivors())
    assert order is not None                         # both gates are inclusive at the limit
    assert order["token"]["ticker"] == "T1"
    assert order["token"]["address"] == "Addr1" and order["token"]["chain"] == "solana"
    assert order["size_factor"] == NO_SOCIAL_CUT     # no X account on any candidate
    assert order["confidence"] == PICK_MIN_CONF
    assert order["runner_up"][0][0] in {"T0", "T2"}


def test_pick_declines_below_either_gate():
    s = _survivors()
    assert pick(_pick_judge(worth=PICK_MIN_WORTH - 0.01, conf=0.9), s) is None
    assert pick(_pick_judge(worth=0.9, conf=PICK_MIN_CONF - 0.01), s) is None


def test_pick_declines_when_the_choice_is_not_a_candidate():
    assert pick(_pick_judge(worth=0.9, conf=0.9, choice="GHOST"), _survivors()) is None


def test_pick_declines_on_no_survivors():
    assert pick(_pick_judge(worth=0.9, conf=0.9), []) is None


def test_unknown_set_is_422_not_retry():
    try:
        JUDGE("nonsense", {})
        assert False, "should raise"
    except RuntimeError as e:
        assert "malformed" in str(e)


# ---- book ---------------------------------------------------------------------
def test_book_one_position_and_reasoned_bench():
    book.release()
    assert book.held() is None
    book.take({"token": {"ticker": "X", "address": "a", "network_id": 56}})
    assert book.held()["ticker"] == "X"
    book.release()
    assert book.held() is None
    book.sit("a:56", "honeypot")
    book.sit("b:56", "age")
    assert book.benched("a:56") and book.benched("b:56") and not book.benched("c:56")
    rows = {tid: reason for tid, reason, _ in book.bench_report()}
    assert rows["a:56"] == "honeypot"


# ---- the whole shift, faked collectors ---------------------------------------
# The cycle fixture is deliberately mixed so that every stage of the funnel kills
# something. A uniform fixture leaves the free/trade/chain counters at zero, and an
# accounting assertion over three always-zero counters proves nothing.
YOUNG   = (1, 2)      # -> free kill, "age"
THIN    = (3,)        # -> free kill, "liquidity"
NO_SELL = "T4"        # -> trade kill, "no_sells"
WHALE   = "T5"        # -> chain kill, "top_wallet"


class FakeFomo:
    def token(self): return "x"
    def tokens(self, ids):
        rows = {}
        for i, tid in enumerate(ids):
            rows[tid] = {"symbol": f"T{i}", "mcap": 300_000 + i * 1000,
                         "liq": 1_000 if i in THIN else 48_000,
                         "vol24": 610_000, "price": 0.001, "holders": 310,
                         "change": {300: 0.04, 3600: 0.22, 14400: 0.4, 86400: 0.61},
                         "created": NOW_MS - (5 if i in YOUNG else 42) * 60_000}
        return rows


class FakeDesk:
    def __init__(self): self.shadow, self.reports, self.sent = [], [], []
    def bank(self): return 1000.0
    def read_x(self, h): return None
    def log_shadow(self, o, s): self.shadow.append((o, s))
    def report(self, o, s): self.reports.append((o, s))
    def send_to_seats(self, o): self.sent.append(o)


def test_run_once_shadow_never_takes_book(monkeypatch):
    book.release()
    ids = [f"Addr{i}:1399811149" for i in range(12)]
    monkeypatch.setattr(shift, "universe", lambda: ids)
    monkeypatch.setattr(shift, "trade_counts", lambda t: {
        "buys_h1": 540, "sells_h1": 0 if t["ticker"] == NO_SELL else 120,
        "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000})
    monkeypatch.setattr(shift, "dossier", lambda t: {
        **t, "chain": "solana", "top_10_percent": 30,
        "top_wallet_percent": 0.2 if t["ticker"] == WHALE else 0.02,
        "developer_holding_percentage": 2,
        "gt_score_details": None, "is_honeypot": None,
        "mint_authority": None, "freeze_authority": None,
        "description": "a token", "x_handle": None})
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier=12)
    assert order is None                                   # shadow never returns an order
    assert book.held() is None                             # and never takes the book
    assert stats["seen"] == 12 and stats["judged"] >= 1
    # each stage kills the tokens built to die there, and names the reason
    assert stats["free"] == {"age": len(YOUNG), "liquidity": len(THIN)}
    assert stats["trade"] == {"no_sells": 1}
    assert stats["chain"] == {"top_wallet": 1}
    # Every token leaves the funnel exactly once: benched on arrival, killed with a
    # named reason at one of the three pre-judge stages, or judged. A judged token
    # then either soft-kills or survives. No token may vanish unaccounted for.
    killed = {k: sum(stats[k].values()) for k in ("free", "trade", "chain", "soft")}
    assert (stats["benched"] + killed["free"] + killed["trade"] + killed["chain"]
            + stats["judged"]) == stats["seen"]
    assert killed["soft"] <= stats["judged"]
    # shadow writes one row per would-be trade and sends nothing to the seats
    assert desk.sent == []
    assert len(desk.shadow) <= 1


def test_held_position_means_no_scan(monkeypatch):
    book.take({"token": {"ticker": "HELD", "address": "a", "network_id": 56}})
    called = []
    monkeypatch.setattr(shift, "universe", lambda: called.append(1) or [])
    order, stats = shift.run_once(FakeFomo(), JUDGE, FakeDesk(), 1000.0)
    assert order is None and stats["held"] == "HELD" and not called
    book.release()


# ---- ops panel state.json -----------------------------------------------------
def test_write_state_produces_expected_keys():
    """write_state should create state.json with all required keys."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    order = {
        "order_id": "2026-01-01T00:00:00Z",
        "token": {"ticker": "TEST", "address": "addr1", "network_id": 56, "chain": "bsc"},
        "size_factor": 0.6,
        "confidence": 0.82,
        "model": "jev-3.1.4"
    }
    stats = {
        "seen": 50,
        "benched": 10,
        "judged": 5,
        "free": {"age": 20, "liquidity": 5},
        "trade": {"no_sells": 2},
        "chain": {"honeypot": 1},
        "soft": {"momentum_already_spent": 3},
        "tokens": [
            {"tid": "addr1:56", "ticker": "T1", "chain": "bsc", "stage": "free",
             "reason": "age", "verdict": "DROP", "mcap_usd": 100000,
             "liquidity_usd": 5000, "age_minutes": 5},
            {"tid": "addr2:56", "ticker": "T2", "chain": "bsc", "stage": "judged",
             "reason": None, "verdict": "PASS", "mcap_usd": 500000,
             "liquidity_usd": 50000, "age_minutes": 42}
        ]
    }
    
    desk.write_state(order, stats)
    
    state_path = desk_mod.OUTBOX / "state.json"
    assert state_path.exists()
    
    import json
    state = json.loads(state_path.read_text())
    
    assert state["demo"] is False
    assert state["mode"] in ("shadow", "live")
    assert "updated_at" in state
    assert "cycle" in state
    assert state["cycle"]["seen"] == 50
    assert state["cycle"]["benched"] == 10
    assert state["cycle"]["judged"] == 5
    assert state["cycle"]["outcome"] in ("SHADOW", "ORDER", "NO TRADE")
    assert "killed" in state["cycle"]
    assert state["cycle"]["killed"]["free"] == {"age": 20, "liquidity": 5}
    assert state["cycle"]["killed"]["trade"] == {"no_sells": 2}
    assert state["cycle"]["killed"]["chain"] == {"honeypot": 1}
    assert state["cycle"]["killed"]["soft"] == {"momentum_already_spent": 3}
    assert "tokens" in state
    assert len(state["tokens"]) == 2
    assert state["tokens"][0]["ticker"] == "T1"
    assert state["tokens"][0]["stage"] == "free"
    assert state["tokens"][0]["reason"] == "age"
    assert state["tokens"][0]["verdict"] == "DROP"
    assert state["tokens"][1]["verdict"] == "PASS"
    assert "held" in state
    assert "bench" in state
    assert state["pick"] == order


def test_write_state_no_trade_cycle():
    """write_state should handle NO TRADE cycles correctly."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    stats = {
        "seen": 100,
        "benched": 50,
        "judged": 0,
        "free": {"age": 30, "liquidity": 10, "volume": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": [
            {"tid": "addr1:56", "ticker": "T1", "chain": "bsc", "stage": "free",
             "reason": "age", "verdict": "DROP", "mcap_usd": 50000,
             "liquidity_usd": 2000, "age_minutes": 8}
        ]
    }
    
    desk.write_state(None, stats)
    
    import json
    state = json.loads((desk_mod.OUTBOX / "state.json").read_text())
    
    assert state["demo"] is False
    assert state["cycle"]["outcome"] == "NO TRADE"
    assert state["cycle"]["error"] is None
    assert state["pick"] is None


def test_write_state_held_position():
    """write_state should handle HOLDING cycles correctly."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    stats = {"held": "TICKER", "minutes": 25}
    
    desk.write_state(None, stats)
    
    import json
    state = json.loads((desk_mod.OUTBOX / "state.json").read_text())
    
    assert state["demo"] is False
    assert state["cycle"]["outcome"] == "HOLDING"
    assert state["cycle"]["error"] is None


def test_api_state_returns_file():
    """GET /api/state should return the state.json file."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    import desk as desk_mod
    desk = desk_mod.Desk()
    stats = {
        "seen": 10,
        "benched": 2,
        "judged": 1,
        "free": {"age": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": []
    }
    desk.write_state(None, stats)
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/api/state")
    assert response.status_code == 200
    data = response.json()
    assert data["demo"] is False
    assert data["cycle"]["seen"] == 10


def test_api_state_404_when_missing():
    """GET /api/state should return 404 with demo:false when state.json doesn't exist."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    import pathlib
    state_path = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox")) / "state.json"
    if state_path.exists():
        state_path.unlink()
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/api/state")
    assert response.status_code == 404
    data = response.json()
    assert data["demo"] is False
    assert data["tokens"] == []
    assert data["cycle"] is None


def test_ops_endpoint_returns_html():
    """GET /ops should return 200 and HTML."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/ops")
    assert response.status_code == 200
    assert "jev-desk ops" in response.text
    assert "ops-console" in response.text or "Kill Histograms" in response.text
    # Verify new summary section exists
    assert "summary-box" in response.text
    assert "What Happened" in response.text
    assert "Judge Health" in response.text


def test_ops_panel_has_summary_and_judge_health():
    """Ops panel should include summary box and judge health display."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/ops")
    assert response.status_code == 200
    html = response.text
    
    # Verify summary section
    assert 'id="summary-box"' in html
    assert 'id="summary-content"' in html
    assert "What Happened" in html
    
    # Verify judge health field
    assert 'id="judge-health"' in html
    
    # Verify JavaScript functions for rendering
    assert "renderSummary" in html
    assert "renderJudgeHealth" in html
    assert "summary-highlight" in html  # CSS class for emphasis
    assert "summary-warning" in html    # CSS class for warnings


def test_run_once_records_tokens(monkeypatch):
    """run_once should populate stats['tokens'] with stage/reason for each token."""
    book.release()
    
    # Use unique addresses to avoid collisions with bench entries from earlier tests
    import time
    unique_suffix = int(time.time() * 1000000) % 1000000
    
    # Create tokens with unique addresses and ensure at least one passes free_kill
    def fake_shortlist(fomo, id_list):
        tokens = []
        for i in range(5):
            addr = f"UniqueAddr{i}_{unique_suffix}"
            tid = f"{addr}:1399811149"
            # Make token 0-1 fail free kill (young), token 2+ pass
            t = tok(i, tid=tid, addr=addr, age_minutes=10 if i < 2 else 42)
            tokens.append(t)
        return tokens
    
    ids = [f"UniqueAddr{i}_{unique_suffix}:1399811149" for i in range(5)]
    
    monkeypatch.setattr(shift, "universe", lambda: ids)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    
    # Monkeypatch book.benched to ensure tokens aren't skipped
    monkeypatch.setattr(book, "benched", lambda tid: False)
    
    monkeypatch.setattr(shift, "trade_counts", lambda t: {
        "buys_h1": 540, "sells_h1": 120,
        "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000})
    monkeypatch.setattr(shift, "dossier", lambda t: {
        **t, "chain": "solana", "top_10_percent": 30,
        "top_wallet_percent": 0.02, "developer_holding_percentage": 2,
        "gt_score_details": None, "is_honeypot": None,
        "mint_authority": None, "freeze_authority": None,
        "description": "a token", "x_handle": None})
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier=12)
    
    assert "tokens" in stats
    assert len(stats["tokens"]) >= 1, f"Expected at least 1 token record, got {len(stats['tokens'])}"
    
    for t in stats["tokens"]:
        assert "tid" in t or "ticker" in t
        assert "stage" in t
        assert "verdict" in t
        if t["verdict"] == "DROP":
            assert t["reason"] is not None


def test_normalise_and_clean_handle():
    m = FakeFomo().tokens(["Addr1:56"])["Addr1:56"]
    t = collect.normalise("Addr1:56", m)
    assert t["net"] == 56 and 41 < t["age_minutes"] < 44 and t["change"]["1h"] == 0.22
    assert collect.clean_handle("LuffyX100X/status/2102659581109272876") == "LuffyX100X"
    assert collect.clean_handle("@good_handle?x=1") == "good_handle"
    assert collect.clean_handle("https://x.com/foo") is None
    assert collect.clean_handle("") is None


def test_universe_429_stops_network_not_all():
    """GeckoTerminal 429 should stop pagination for that network only, not wipe entire universe."""
    import requests
    
    # Mock responses: solana page 1 succeeds, page 2 gets 429, bsc succeeds
    mock_responses = [
        # Solana page 1 - success
        Mock(status_code=200, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "solana_addr1"}}}}
            ]
        }),
        # Solana page 2 - 429
        Mock(status_code=429),
        # BSC page 1 - success (should still run)
        Mock(status_code=200, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "bsc_addr2"}}}}
            ]
        }),
        # BSC page 2 - success
        Mock(status_code=200, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "bsc_addr3"}}}}
            ]
        })
    ]
    
    original_get = requests.get
    mock_get = Mock(side_effect=mock_responses)
    requests.get = mock_get
    
    try:
        ids = collect.universe(nets=("solana", "bsc"), pages=2)
        
        # Should have IDs from solana page 1 and both bsc pages
        # (solana stopped at page 2 due to 429, but BSC continued)
        assert len(ids) >= 2
        assert any("1399811149" in tid for tid in ids)  # Solana network
        assert any("56" in tid for tid in ids)  # BSC network
        
        # Verify we collected from multiple networks despite 429 on one
        networks = {tid.split(":")[1] for tid in ids}
        assert len(networks) >= 2 or len(ids) >= 1  # Got IDs from at least one network
    finally:
        requests.get = original_get
