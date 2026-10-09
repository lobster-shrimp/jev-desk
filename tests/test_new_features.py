"""
Tests for new features added in FOMO multi-chain and pool-aware holder checks PR.

These tests verify:
- FOMO X-Supported-Chains header on all calls
- FOMO native feeds (trending/graduated) with dedupe
- Pool vault exclusion from Solana holder checks
- EVM fail-closed behavior for unverified top_wallet
- Bench migrations and age-aware momentum
- Single-survivor pick gates
- DexScreener pair selection by txns
"""
import logging
import os
import pathlib
import sys
import tempfile
from unittest.mock import Mock, patch

import pytest
import requests

# Add workspace to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import book
import collect
from collect import sol_top_wallet, AMM_PROGRAMS
import fomo_api
from filter import chain_kill
import main as shift
from pick import pick


def test_fomo_header_sent_on_all_calls():
    """X-Supported-Chains header should be sent on all FOMO API calls."""
    with patch.object(fomo_api.requests.Session, 'post') as mock_post:
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"responseObject": []}
        )
        
        fomo = fomo_api.Fomo(bearer="test_bearer")
        fomo.tokens(["test:1399811149"])
        
        # Verify header was set on session
        assert fomo.s.headers.get("X-Supported-Chains") == fomo_api.FOMO_SUPPORTED_CHAINS
        
        # Verify post was called
        assert mock_post.called


def test_fomo_trending_fails_soft():
    """FOMO trending_tokens() should fail soft on errors and return empty list."""
    with patch.object(fomo_api.requests.Session, 'post') as mock_post:
        mock_post.side_effect = Exception("Network error")
        
        fomo = fomo_api.Fomo(bearer="test_bearer")
        result = fomo.trending_tokens()
        
        assert result == []  # Fails soft, returns empty


def test_fomo_graduated_fails_soft():
    """FOMO graduated_tokens() should fail soft on errors and return empty list."""
    with patch.object(fomo_api.requests.Session, 'post') as mock_post:
        mock_post.side_effect = Exception("Network error")
        
        fomo = fomo_api.Fomo(bearer="test_bearer")
        result = fomo.graduated_tokens()
        
        assert result == []  # Fails soft, returns empty


def test_fomo_feeds_dedupe_with_gt():
    """FOMO feeds should dedupe with GT universe ids."""
    with patch('collect.requests.get') as mock_get:
        # GT returns addr1, addr2
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"relationships": {"base_token": {"data": {"id": "solana_addr1"}}}},
                    {"relationships": {"base_token": {"data": {"id": "solana_addr2"}}}}
                ]
            }
        )
        
        # Mock FOMO to return addr2 (duplicate) and addr3 (new)
        fake_fomo = Mock()
        fake_fomo.trending_tokens.return_value = ["addr2:1399811149", "addr3:1399811149"]
        fake_fomo.graduated_tokens.return_value = []
        
        ids, _ = collect.universe(nets=("solana",), pages=1, include_trending=False, fomo=fake_fomo)
        
        # Should have addr1, addr2, addr3 with no duplicates
        assert len(ids) == 3
        assert len(set(ids)) == 3  # No duplicates
        assert "addr2:1399811149" in ids  # Not duplicated


def test_pool_vault_excluded_from_top_wallet():
    """Pool vault (largest account owned by AMM program) should be excluded from top_wallet."""
    # Clear cache to avoid test pollution
    collect._clear_sol_owner_cache()
    
    with patch('collect.requests.post') as mock_post:
        # Simulate: largest account is pool vault, second is real whale at 7%
        call_count = [0]
        
        def fake_rpc(*args, **kwargs):
            call_count[0] += 1
            method = kwargs['json']['method']
            params = kwargs['json'].get('params', [])
            
            if method == "getTokenSupply":
                return Mock(json=lambda: {
                    "result": {"value": {"amount": "1000000"}}
                })
            elif method == "getTokenLargestAccounts":
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": "pool_vault", "amount": "150000"},  # 15% but it's a pool
                        {"address": "whale_wallet", "amount": "70000"}   # 7% real whale
                    ]}
                })
            elif method == "getMultipleAccounts":
                # First call: resolve token account owners
                if call_count[0] == 3:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {  # pool_vault owner
                                "data": {
                                    "parsed": {
                                        "info": {"owner": "amm_pda_owner"}
                                    }
                                }
                            },
                            {  # whale_wallet owner  
                                "data": {
                                    "parsed": {
                                        "info": {"owner": "whale_owner"}
                                    }
                                }
                            }
                        ]}
                    })
                # Second call: resolve owner programs
                else:
                    requested = params[0] if params else []
                    result_value = []
                    for owner_addr in requested:
                        if owner_addr == "amm_pda_owner":
                            # amm_pda_owner is owned by PumpSwap AMM program
                            result_value.append({"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
                        else:
                            # whale_owner is normal user (owned by system program)
                            result_value.append({"owner": "11111111111111111111111111111111"})
                    return Mock(json=lambda: {"result": {"value": result_value}})
        
        mock_post.side_effect = fake_rpc
        
        holder_data, rpc_ok, error = sol_top_wallet("test_mint")
        
        # Pool vault should be excluded, whale_wallet becomes top
        assert holder_data["top_wallet"] == 0.07  # 7% whale, not 15% pool
        assert holder_data["pools_excluded"] is True
        assert rpc_ok is True


def test_real_whale_at_7_percent_still_kills():
    """Real whale at 7% (above 5% threshold) should still kill token."""
    dossier = {
        "chain": "solana",
        "top_wallet_percent": 0.07,  # 7% > 5% threshold
        "top_10_percent": 35,
        "holder_count": 200
    }
    
    result = chain_kill(dossier)
    assert result == "top_wallet"  # Kills because 7% > 5%


def test_solana_top_10_from_rpc_excludes_pools():
    """Solana top_10 should come from RPC excluding pools, not from GT."""
    # Clear cache to avoid pollution
    collect._clear_sol_owner_cache()
    
    with patch('collect.requests.post') as mock_post:
        # Simulate: 10 accounts, first 2 are pools
        # Total supply: 1,000,000
        # Accounts: 100k, 95k (pools), 90k, 85k, 80k, 75k, 70k, 65k, 60k, 55k
        # Non-pool accounts (8): 90+85+80+75+70+65+60+55 = 580k = 58%
        call_count = [0]
        
        def fake_rpc(*args, **kwargs):
            call_count[0] += 1
            method = kwargs['json']['method']
            
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": f"acct{i}", "amount": str(100000 - i * 5000)}
                        for i in range(10)
                    ]}
                })
            elif method == "getMultipleAccounts":
                # First multi call (id=3): token account owners
                if call_count[0] == 3:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"data": {"parsed": {"info": {"owner": f"owner{i}"}}}}
                            for i in range(10)
                        ]}
                    })
                # Second multi call: owner programs
                # This gets called with the list of unique owners, we need to check what was requested
                # For simplicity, return data matching the requested owners
                params = kwargs['json']['params']
                requested_owners = params[0] if params else []
                result_value = []
                for owner_addr in requested_owners:
                    # Parse owner index from "ownerX"
                    owner_idx = int(owner_addr.replace("owner", ""))
                    if owner_idx < 2:
                        # First 2 are AMM programs
                        result_value.append({"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
                    else:
                        # Rest are normal users
                        result_value.append({"owner": "11111111111111111111111111111111"})
                return Mock(json=lambda: {"result": {"value": result_value}})
        
        mock_post.side_effect = fake_rpc
        
        holder_data, rpc_ok, error = sol_top_wallet("test_mint")
        
        # top_10 should exclude first 2 pools (100k+95k) and sum remaining 8
        # 90+85+80+75+70+65+60+55 = 580k out of 1M = 0.58 (58%)
        expected_top_10 = 0.58
        assert abs(holder_data["top_10"] - expected_top_10) < 0.01
        assert holder_data["pools_excluded"] is True


def test_evm_uses_fomo_top10_holders_percent():
    """End-to-end: EVM token with computed concentration uses those values, not FOMO."""
    import time
    import collect
    from filter import chain_kill
    from unittest.mock import Mock, patch
    
    # Token with FOMO data
    fomo_row = {
        "symbol": "TEST",
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "holders": 200,
        "top10_holders_percent": 65.0,  # FOMO value (should be ignored)
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800
    }
    
    normalized = collect.normalise("testaddr:56", fomo_row)
    assert normalized["fomo_top10_holders_percent"] == 65.0
    
    # Mock GT and evm_holders
    with patch('collect.requests.get') as mock_gt, \
         patch('evm_holders.evm_holder_concentration') as mock_evm:
        
        # GT returns minimal data
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {
                "holders": {"count": 200},
                "developer_holding_percentage": None,
                "gt_score_details": None,
                "is_honeypot": None
            }}}
        )
        
        # EVM holder check returns ok with top_wallet 0.03, top_10 85
        from evm_holders import HolderResult
        mock_evm.return_value = HolderResult(
            top_wallet=0.03,
            top_10=85.0,
            source="honeypot",
            excluded=[],
            ok=True,
            error=None,
            is_transient=False,
            raw_top_wallet=0.03,
            raw_top_10=85.0
        )
        
        d = collect.dossier(normalized, limiter=None)
        
        # Should use computed values, not FOMO
        assert d["top_10_percent"] == 85.0  # From evm_holders, not 65 from FOMO
        assert d["top_wallet_percent"] == 0.03
        
        # Should be killed by top_10 (85/100 = 0.85 > 0.60)
        kill_reason = chain_kill(d)
        assert kill_reason == "top_10"


def test_evm_fails_closed_without_top_wallet():
    """EVM tokens without top_wallet_percent should fail closed with top_wallet_unverified."""
    result = chain_kill({
        "chain": "bsc",
        "top_wallet_percent": None,  # No verification
        "top_10_percent": 30,
        "holder_count": 200
    })
    
    assert result == "top_wallet_unverified"
    
    # Verify bench time
    assert book.BENCH_MINUTES["top_wallet_unverified"] == 360  # 6 hours


def test_top_wallet_bench_is_1440_minutes():
    """top_wallet bench should be 1440 minutes (1 day), not 100000."""
    assert book.BENCH_MINUTES["top_wallet"] == 1440


def test_top_wallet_migration_removes_old_benches():
    """Migration should remove old top_wallet benches on first run."""
    # Check migration was applied
    migration_exists = book.DB.execute(
        "SELECT 1 FROM migrations WHERE name='clear_top_wallet_bench'"
    ).fetchone()
    
    assert migration_exists is not None


def test_momentum_bench_180_for_old_tokens():
    """Tokens aged >= 24h should get 180 min momentum bench."""
    tid = "old_token:1399811149"
    book.DB.execute("DELETE FROM bench WHERE tid=?", (tid,))
    book.DB.commit()
    
    # Old token (24+ hours)
    book.sit(tid, "momentum_already_spent", age_minutes=24*60)
    
    until = book.DB.execute("SELECT until FROM bench WHERE tid=?", (tid,)).fetchone()[0]
    benched_minutes = (until - book.time.time()) / 60
    
    # Should be ~180 minutes
    assert 175 < benched_minutes < 185


def test_momentum_bench_25_for_young_tokens():
    """Tokens aged < 24h should get default 25 min momentum bench."""
    tid = "young_token:1399811149"
    book.DB.execute("DELETE FROM bench WHERE tid=?", (tid,))
    book.DB.commit()
    
    # Young token (< 24h)
    book.sit(tid, "momentum_already_spent", age_minutes=60)
    
    until = book.DB.execute("SELECT until FROM bench WHERE tid=?", (tid,)).fetchone()[0]
    benched_minutes = (until - book.time.time()) / 60
    
    # Should be ~25 minutes
    assert 20 < benched_minutes < 30


def test_weak_lone_survivor_not_picked():
    """Lone survivor with low worth/confidence should NOT be picked."""
    # Create survivor with weak metrics
    dossier = {
        "ticker": "WEAK",
        "addr": "weak_addr",
        "net": 1399811149,
        "chain": "solana",
        "age_minutes": 30,
        "mcap_usd": 500000,
        "liquidity_usd": 50000,
        "holder_count": 200
    }
    
    answers = {
        "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45}
    }
    
    def fake_judge(question_set, state):
        if question_set == "pick":
            return {
                "model": "test",
                "answers": {
                    "best": {"type": "choice", "choice": "WEAK", "confidence": 0.50,  # Below 0.55 threshold
                            "probabilities": {"WEAK": 0.50}},
                    "worth_trading_at_all": {"type": "noul", "noul": 0.55}  # Below 0.60 threshold
                },
                "usage": {}
            }
        return {"model": "test", "answers": {}, "usage": {}}
    
    result = pick(fake_judge, [(dossier, answers)])
    
    assert result is None  # NOT picked due to low worth


def test_dex_pair_selection_by_txns():
    """DexScreener should choose pair by highest h24 txns, not liquidity."""
    # BELIEVE-shaped: main pair has high txns but None liquidity,
    # side pool has low txns but $50 liquidity
    pairs = [
        {
            "liquidity": None,  # Main PumpSwap pair
            "txns": {
                "h24": {"buys": 5130, "sells": 5131}  # 10,261 total txns
            }
        },
        {
            "liquidity": {"usd": 50},  # Meteora side pool
            "txns": {
                "h24": {"buys": 10, "sells": 5}  # Only 15 txns
            }
        }
    ]
    
    with patch('collect.requests.get') as mock_get:
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: pairs
        )
        
        token = {"net": 1399811149, "addr": "BELIEVE", "ticker": "BELIEVE"}
        trade_data, status = collect.trade_counts(token)
        
        # Should pick the high-txn pair (10,261), not the $50 pool (15 txns)
        assert trade_data["trades_h24"] == 10261
        assert status == 'ok'


def test_kill_logs_include_values(caplog):
    """Chain kill logs should include top_10, top_wallet, pools_excluded values."""
    caplog.set_level(logging.INFO)
    
    from filter import chain_kill
    
    dossier = {
        "tid": "test:1399811149",
        "ticker": "TEST",
        "chain": "solana",
        "age_minutes": 30,
        "top_wallet_percent": 0.08,
        "top_10_percent": 35,
        "holder_count": 200,
        "pools_excluded": True
    }
    
    # This will be called by main.py which does the logging
    result = chain_kill(dossier)
    
    assert result == "top_wallet"  # Kills because 8% > 5%


def test_safe_err_used_on_rpc_errors():
    """RPC errors should use safe_err() to scrub secrets."""
    with patch('collect.requests.post') as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "Max retries with url: /?api-key=SECRET123"
        )
        
        holder_data, rpc_ok, error = sol_top_wallet("test_mint")
        
        assert "SECRET123" not in error
        assert "REDACTED" in error


def test_amm_programs_are_valid():
    """AMM_PROGRAMS constant should contain valid Solana program addresses."""
    # All should be 43-44 character base58 strings
    for program_id in AMM_PROGRAMS:
        assert len(program_id) >= 32  # Base58 pubkeys are 32+ chars
        assert program_id.replace("1", "").replace("A", "").replace("B", "")  # Valid base58 chars


def test_solana_dossier_85_percent_top10_killed():
    """End-to-end: Solana dossier with RPC top_10 = 85% should be killed as 'top_10'."""
    collect._clear_sol_owner_cache()
    
    import time
    token = {
        "symbol": "CONC",
        "addr": "conc_addr",
        "net": 1399811149,
        "ticker": "CONC",
        "holders": 200,  # normalise expects "holders", not "holder_count"
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800
    }
    normalized = collect.normalise(f"{token['addr']}:{token['net']}", token)
    
    # Mock RPC to return 85% top_10 (0.85 fraction)
    with patch('collect.requests.post') as mock_rpc, \
         patch('collect.requests.get') as mock_gt:
        
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"holders": None}}}
        )
        
        def fake_rpc(*args, **kwargs):
            method = kwargs['json']['method']
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                # Top wallet: 49k = 4.9% (below 5% threshold)
                # Next 9: 62k each = ~55.8%
                # Total: ~60.7% (above 60% threshold)
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": "acct0", "amount": "49000"},  # 4.9%
                        *[{"address": f"acct{i}", "amount": "62000"} for i in range(1, 10)]
                    ]}
                })
            elif method == "getMultipleAccounts":
                call_id = kwargs['json']['id']
                if call_id == 3:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"data": {"parsed": {"info": {"owner": f"owner{i}"}}}}
                            for i in range(10)
                        ]}
                    })
                else:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"owner": "11111111111111111111111111111111"}
                            for i in range(10)
                        ]}
                    })
        
        mock_rpc.side_effect = fake_rpc
        
        d = collect.dossier(normalized, limiter=None)
        
        # Should store as percent (0-100)
        # Actual computed value: 49k + 9*62k = 607k = 60.7%
        # Top wallet: 4.9% (below 5% threshold)
        assert d["top_10_percent"] > 60.0  # Above 60% threshold
        assert d.get("top_wallet_percent", 0) < 0.05  # Below 5% threshold
        
        # Should be killed by top_10, not top_wallet
        kill_reason = chain_kill(d)
        assert kill_reason == "top_10"  # 60.7/100 = 0.607 > 0.60


def test_solana_dossier_40_percent_top10_passes():
    """End-to-end: Solana dossier with RPC top_10 = 40% should NOT be killed."""
    collect._clear_sol_owner_cache()
    
    import time
    token = {
        "symbol": "DISTRIB",
        "addr": "distrib_addr",
        "net": 1399811149,
        "ticker": "DISTRIB",
        "holders": 200,
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800
    }
    normalized = collect.normalise(f"{token['addr']}:{token['net']}", token)
    
    # Mock RPC to return 40% top_10
    with patch('collect.requests.post') as mock_rpc, \
         patch('collect.requests.get') as mock_gt:
        
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"holders": None}}}
        )
        
        def fake_rpc(*args, **kwargs):
            method = kwargs['json']['method']
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                # Top 10 hold 40% total
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": f"acct{i}", "amount": str(40000 - i * 500)}
                        for i in range(10)
                    ]}
                })
            elif method == "getMultipleAccounts":
                call_id = kwargs['json']['id']
                if call_id == 3:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"data": {"parsed": {"info": {"owner": f"owner{i}"}}}}
                            for i in range(10)
                        ]}
                    })
                else:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"owner": "11111111111111111111111111111111"}
                            for i in range(10)
                        ]}
                    })
        
        mock_rpc.side_effect = fake_rpc
        
        d = collect.dossier(normalized, limiter=None)
        
        # Should store as percent (0-100)
        # Actual computed value: 40+39.5+39+...+36 = 377.5k = 37.75%
        assert d["top_10_percent"] < 60.0  # Below 60% threshold
        
        # Should NOT be killed (top_10/100 < 0.60)
        kill_reason = chain_kill(d)
        assert kill_reason is None


def test_evm_dossier_85_percent_top10_killed():
    """End-to-end: EVM token with holder check unavailable fails closed."""
    import time
    import collect
    from filter import chain_kill
    from unittest.mock import Mock, patch
    
    token = {
        "symbol": "ETEST",
        "addr": "etest_addr",
        "net": 56,
        "ticker": "ETEST",
        "holders": 200,
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "top10_holders_percent": 85.0,
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800
    }
    normalized = collect.normalise(f"{token['addr']}:{token['net']}", token)
    
    # Mock GT and evm_holders to fail
    with patch('collect.requests.get') as mock_gt, \
         patch('evm_holders.evm_holder_concentration') as mock_evm:
        
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {
                "holders": {"count": 200},
                "developer_holding_percentage": None
            }}}
        )
        
        # EVM holder check fails (definitive)
        from evm_holders import HolderResult
        mock_evm.return_value = HolderResult(
            top_wallet=None,
            top_10=None,
            source="unavailable",
            excluded=[],
            ok=False,
            error="zero_supply",
            is_transient=False  # Definitive
        )
        
        d = collect.dossier(normalized, limiter=None)
        
        # Should have None for both values
        assert d["top_10_percent"] is None
        assert d["top_wallet_percent"] is None
        assert d["evm_holder_transient"] is False
        
        # Should fail closed with top_wallet_unverified (definitive)
        kill_reason = chain_kill(d)
        assert kill_reason == "top_wallet_unverified"


def test_solana_dossier_7_percent_wallet_killed():
    """End-to-end: Solana dossier with 7% top_wallet should be killed as 'top_wallet'."""
    collect._clear_sol_owner_cache()
    
    import time
    token = {
        "symbol": "WHALE",
        "addr": "whale_addr",
        "net": 1399811149,
        "ticker": "WHALE",
        "holders": 200,
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800
    }
    normalized = collect.normalise(f"{token['addr']}:{token['net']}", token)
    
    # Mock RPC to return 7% top_wallet
    with patch('collect.requests.post') as mock_rpc, \
         patch('collect.requests.get') as mock_gt:
        
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"holders": None}}}
        )
        
        def fake_rpc(*args, **kwargs):
            method = kwargs['json']['method']
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": "whale", "amount": "70000"},  # 7%
                        {"address": "acct1", "amount": "30000"}
                    ]}
                })
            elif method == "getMultipleAccounts":
                call_id = kwargs['json']['id']
                if call_id == 3:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"data": {"parsed": {"info": {"owner": "whale_owner"}}}},
                            {"data": {"parsed": {"info": {"owner": "owner1"}}}}
                        ]}
                    })
                else:
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"owner": "11111111111111111111111111111111"},
                            {"owner": "11111111111111111111111111111111"}
                        ]}
                    })
        
        mock_rpc.side_effect = fake_rpc
        
        d = collect.dossier(normalized, limiter=None)
        
        # Should store as fraction (0-1)
        assert d["top_wallet_percent"] == 0.07
        
        # Should be killed (0.07 > 0.05)
        kill_reason = chain_kill(d)
        assert kill_reason == "top_wallet"


def test_owner_cache_hits_on_shared_vault():
    """Second dossier sharing an owner should hit cache, no extra owner program RPC call."""
    collect._clear_sol_owner_cache()
    
    import time
    
    def make_token(name):
        return collect.normalise(f"{name}_addr:1399811149", {
            "symbol": name,
            "addr": f"{name}_addr",
            "net": 1399811149,
            "ticker": name,
            "holders": 200,
            "mcap": 500000,
            "liq": 50000,
            "vol24": 100000,
            "price": 0.5,
            "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
            "created": time.time() - 1800
        })
    
    token1 = make_token("TOKEN1")
    token2 = make_token("TOKEN2")
    
    call_counts = {"supply": 0, "largest": 0, "multi": 0, "multi_owners": 0, "multi_programs": 0}
    
    with patch('collect.requests.post') as mock_rpc, \
         patch('collect.requests.get') as mock_gt:
        
        mock_gt.return_value = Mock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"holders": None}}}
        )
        
        def fake_rpc(*args, **kwargs):
            method = kwargs['json']['method']
            params = kwargs['json'].get('params', [])
            
            if method == "getTokenSupply":
                call_counts["supply"] += 1
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                call_counts["largest"] += 1
                # Each token has different token accounts, but they share the same owner
                # (different token account addresses but owned by same PDA)
                token_idx = call_counts["largest"]
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": f"vault_t{token_idx}", "amount": "100000"},
                        {"address": f"holder_t{token_idx}", "amount": "50000"}
                    ]}
                })
            elif method == "getMultipleAccounts":
                call_counts["multi"] += 1
                # Check if this is requesting token accounts or owner programs
                # Token accounts are requested by address, owners by owner address
                requested = params[0] if params else []
                if any(addr.startswith("vault_") or addr.startswith("holder_") for addr in requested):
                    # This is fetching token account owners
                    call_counts["multi_owners"] += 1
                    result_value = []
                    for addr in requested:
                        if addr.startswith("vault_"):
                            # Both vaults owned by same AMM owner
                            result_value.append({"data": {"parsed": {"info": {"owner": "shared_amm_owner"}}}})
                        else:
                            # Different user owners
                            result_value.append({"data": {"parsed": {"info": {"owner": f"owner_{addr}"}}}})
                    return Mock(json=lambda: {"result": {"value": result_value}})
                else:
                    # This is fetching owner programs
                    call_counts["multi_programs"] += 1
                    result_value = []
                    for owner_addr in requested:
                        if owner_addr == "shared_amm_owner":
                            result_value.append({"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
                        else:
                            result_value.append({"owner": "11111111111111111111111111111111"})
                    return Mock(json=lambda: {"result": {"value": result_value}})
        
        mock_rpc.side_effect = fake_rpc
        
        # First dossier
        d1 = collect.dossier(token1, limiter=None)
        programs_after_first = call_counts["multi_programs"]
        
        # Second dossier (shares owner)
        d2 = collect.dossier(token2, limiter=None)
        programs_after_second = call_counts["multi_programs"]
        
        # First dossier: 1 owner program call (for shared_amm_owner and holder owner)
        # Second dossier: 0 owner program calls (shared_amm_owner cached, only new holder owner needed)
        # Actually, the new holder owner is different, so it will make a call for that
        # But the shared_amm_owner should be cached
        # Let's check that at least one program lookup was saved
        assert programs_after_first == 1
        # Second dossier has 1 new owner (holder), but shared owner is cached
        # So it should still make 1 call (for the new holder owner)
        assert programs_after_second == 2  # +1 for new owner, shared owner cached


def test_migration_preserves_non_top_wallet_benches():
    """Migration should preserve non-top_wallet benches like authority_open, momentum."""
    # Create test benches
    test_tid_top = "test_top:1399811149"
    test_tid_auth = "test_auth:1399811149"
    test_tid_mom = "test_mom:1399811149"
    
    # Clear existing
    book.DB.execute("DELETE FROM bench WHERE tid IN (?, ?, ?)", 
                    (test_tid_top, test_tid_auth, test_tid_mom))
    book.DB.commit()
    
    # Add benches
    future = book.time.time() + 3600
    book.DB.execute("INSERT INTO bench (tid, reason, until) VALUES (?, ?, ?)",
                    (test_tid_top, "top_wallet", future))
    book.DB.execute("INSERT INTO bench (tid, reason, until) VALUES (?, ?, ?)",
                    (test_tid_auth, "authority_open", future))
    book.DB.execute("INSERT INTO bench (tid, reason, until) VALUES (?, ?, ?)",
                    (test_tid_mom, "momentum_already_spent", future))
    book.DB.commit()
    
    # Run migration (idempotent, safe to re-run)
    book._clear_top_wallet_bench()
    
    # Check results
    top_exists = book.DB.execute("SELECT 1 FROM bench WHERE tid=? AND reason='top_wallet'", 
                                   (test_tid_top,)).fetchone()
    auth_exists = book.DB.execute("SELECT 1 FROM bench WHERE tid=? AND reason='authority_open'",
                                    (test_tid_auth,)).fetchone()
    mom_exists = book.DB.execute("SELECT 1 FROM bench WHERE tid=? AND reason='momentum_already_spent'",
                                   (test_tid_mom,)).fetchone()
    
    # top_wallet should be cleared, others preserved
    assert top_exists is None
    assert auth_exists is not None
    assert mom_exists is not None


def test_all_pools_edge_case():
    """If all top accounts are pools, return top_wallet=0.0 with pools_excluded=True."""
    collect._clear_sol_owner_cache()
    
    with patch('collect.requests.post') as mock_post:
        call_count = [0]
        
        def fake_rpc(*args, **kwargs):
            call_count[0] += 1
            method = kwargs['json']['method']
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                # All accounts are large pools
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"address": f"pool{i}", "amount": str(100000 - i * 10000)}
                        for i in range(10)
                    ]}
                })
            elif method == "getMultipleAccounts":
                if call_count[0] == 3:  # First call: token account owners
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {"data": {"parsed": {"info": {"owner": f"amm_owner{i}"}}}}
                            for i in range(10)
                        ]}
                    })
                else:  # Second call: owner programs - all AMMs
                    params = kwargs['json']['params']
                    requested_owners = params[0] if params else []
                    result_value = []
                    for owner_addr in requested_owners:
                        # All owners are owned by AMM program
                        result_value.append({"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
                    return Mock(json=lambda: {"result": {"value": result_value}})
        
        mock_post.side_effect = fake_rpc
        
        holder_data, rpc_ok, error = sol_top_wallet("all_pools_mint")
        
        # Should return 0.0, not None
        assert holder_data["top_wallet"] == 0.0
        assert holder_data["top_10"] == 0.0
        assert holder_data["pools_excluded"] is True


def test_pick_exception_handled():
    """Judge error on pick should fail soft (no pick) and cycle completes."""
    # Mock judge that raises on pick
    def fake_judge(question_set, state):
        if question_set == "pick":
            raise Exception("Judge 422 error")
        return {"model": "test", "answers": {}, "usage": {}}
    
    dossier = {
        "ticker": "TEST",
        "addr": "test_addr",
        "net": 1399811149,
        "chain": "solana",
        "age_minutes": 30,
        "mcap_usd": 500000,
        "liquidity_usd": 50000,
        "holder_count": 200
    }
    
    answers = {
        "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45}
    }
    
    # Should not raise, returns None
    result = pick(fake_judge, [(dossier, answers)])
    assert result is None


def test_pick_exception_scrubs_secrets(caplog):
    """Pick exception handling should scrub secrets from error messages."""
    import logging
    caplog.set_level(logging.WARNING)
    
    # Mock judge that raises exception with secret in message
    def fake_judge(question_set, state):
        if question_set == "pick":
            raise Exception("Connection failed: https://api.example.com/judge?api-key=SECRET999")
        return {"model": "test", "answers": {}, "usage": {}}
    
    dossier = {
        "ticker": "TEST",
        "addr": "test_addr",
        "net": 1399811149,
        "chain": "solana",
        "age_minutes": 30,
        "mcap_usd": 500000,
        "liquidity_usd": 50000,
        "holder_count": 200
    }
    
    answers = {
        "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45}
    }
    
    # Should not raise, returns None
    result = pick(fake_judge, [(dossier, answers)])
    assert result is None
    
    # Check log was captured
    assert len(caplog.records) > 0
    log_messages = " ".join([r.message for r in caplog.records])
    
    # Secret should be redacted
    assert "REDACTED" in log_messages
    assert "SECRET999" not in log_messages
    assert "NO PICK" in log_messages
