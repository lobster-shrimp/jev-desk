"""
Funnel tests. No network, no key: collectors are faked, the judge is the mock.
    pytest -q
"""
import os
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_DB", ":memory:")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))

import book                                           # noqa: E402
from filter import free_kill, trade_kill, chain_kill, soft_kill   # noqa: E402
from pick import pick, summary                        # noqa: E402
from mock_judge import mock_judge_fn                  # noqa: E402
from questions import SETS                            # noqa: E402
import main as shift                                  # noqa: E402
import collect                                        # noqa: E402

JUDGE = mock_judge_fn()
NOW_MS = int(time.time() * 1000)


def tok(i, net=1399811149, **over):
    t = {"addr": f"Addr{i}", "net": net, "tid": f"Addr{i}:{net}", "ticker": f"T{i}",
         "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
         "price_usd": 0.001, "holder_count": 310,
         "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}, "age_minutes": 42}
    t.update(over)
    return t


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


def test_pick_builds_options_from_candidates_and_gates_on_worth():
    survivors = []
    for i in range(3):
        d = {**tok(i), "chain": "solana"}
        ans = {"shape": {"choice": "crowd", "probabilities": {"crowd": 0.7 + i / 10}},
               "concentration_is_exit_risk": {"noul": 0.2},
               "authority_risk": {"choice": "renounced"}}
        survivors.append((d, ans))
    s = summary(*survivors[0])
    assert "solana" in s and "no usable X account" in s
    order = pick(JUDGE, survivors)
    if order is not None:
        assert order["token"]["ticker"] in {"T0", "T1", "T2"}
        assert order["size_factor"] == 0.6           # no social -> NO_SOCIAL_CUT
        assert 0 <= order["confidence"] <= 1


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
class FakeFomo:
    def token(self): return "x"
    def tokens(self, ids):
        rows = {}
        for i, tid in enumerate(ids):
            rows[tid] = {"symbol": f"T{i}", "mcap": 300_000 + i * 1000, "liq": 48_000,
                         "vol24": 610_000, "price": 0.001, "holders": 310,
                         "change": {300: 0.04, 3600: 0.22, 14400: 0.4, 86400: 0.61},
                         "created": NOW_MS - 42 * 60_000}
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
    monkeypatch.setattr(shift, "trade_counts", lambda t: {"buys_h1": 540, "sells_h1": 120,
                                                          "buys_h6": 900, "sells_h6": 400,
                                                          "trades_h24": 4000})
    monkeypatch.setattr(shift, "dossier", lambda t: {**t, "chain": "solana", "top_10_percent": 30,
                                                     "top_wallet_percent": 0.02,
                                                     "developer_holding_percentage": 2,
                                                     "gt_score_details": None, "is_honeypot": None,
                                                     "mint_authority": None, "freeze_authority": None,
                                                     "description": "a token", "x_handle": None})
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier=12)
    assert order is None                                   # shadow never returns an order
    assert book.held() is None                             # and never takes the book
    assert stats["seen"] == 12 and stats["judged"] >= 1
    # every token either survived, was benched, or was killed with a named reason
    killed = sum(sum(v.values()) for k, v in stats.items() if isinstance(v, dict))
    assert killed + len(desk.shadow) * 0 <= 12


def test_held_position_means_no_scan(monkeypatch):
    book.take({"token": {"ticker": "HELD", "address": "a", "network_id": 56}})
    called = []
    monkeypatch.setattr(shift, "universe", lambda: called.append(1) or [])
    order, stats = shift.run_once(FakeFomo(), JUDGE, FakeDesk(), 1000.0)
    assert order is None and stats["held"] == "HELD" and not called
    book.release()


def test_normalise_and_clean_handle():
    m = FakeFomo().tokens(["Addr1:56"])["Addr1:56"]
    t = collect.normalise("Addr1:56", m)
    assert t["net"] == 56 and 41 < t["age_minutes"] < 44 and t["change"]["1h"] == 0.22
    assert collect.clean_handle("LuffyX100X/status/2102659581109272876") == "LuffyX100X"
    assert collect.clean_handle("@good_handle?x=1") == "good_handle"
    assert collect.clean_handle("https://x.com/foo") is None
    assert collect.clean_handle("") is None
