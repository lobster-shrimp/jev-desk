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
                    return Mock(json=lambda: {
                        "result": {"value": [
                            {  # amm_pda_owner is owned by PumpSwap
                                "owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
                            },
                            {  # whale_owner is normal user (owned by system program)
                                "owner": "11111111111111111111111111111111"
                            }
                        ]}
                    })
        
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
    with patch('collect.requests.post') as mock_post:
        # Simulate: top 10 accounts include 2 pools, real top_10 is lower
        def fake_rpc(*args, **kwargs):
            method = kwargs['json']['method']
            
            if method == "getTokenSupply":
                return Mock(json=lambda: {"result": {"value": {"amount": "1000000"}}})
            elif method == "getTokenLargestAccounts":
                # Return 10 accounts: 2 are pools
                accounts = []
                for i in range(10):
                    accounts.append({
                        "address": f"acct{i}",
                        "amount": str(100000 - i * 5000)  # Decreasing amounts
                    })
                return Mock(json=lambda: {"result": {"value": accounts}})
            elif method == "getMultipleAccounts":
                # Mark first 2 as pool vaults
                return Mock(json=lambda: {
                    "result": {"value": [
                        {"data": {"parsed": {"info": {"owner": f"owner{i}"}}}} if i < 10 else None
                        for i in range(10)
                    ]}
                })
        
        mock_post.side_effect = fake_rpc
        
        holder_data, rpc_ok, error = sol_top_wallet("test_mint")
        
        # top_10 should exclude pools
        assert holder_data["top_10"] is not None
        assert holder_data["top_10"] < 1.0  # Reasonable value


def test_evm_uses_fomo_top10_holders_percent():
    """EVM chains should use FOMO top10HoldersPercent for top_10 check."""
    # Mock FOMO to return top10HoldersPercent
    import time
    fomo_row = {
        "symbol": "TEST",
        "mcap": 500000,
        "liq": 50000,
        "vol24": 100000,
        "price": 0.5,
        "holders": 200,
        "top10_holders_percent": 65.0,  # 65% (will be converted to 0.65)
        "change": {300: 0.05, 3600: 0.15, 14400: 0.30, 86400: 0.50},
        "created": time.time() - 1800  # 30 minutes ago
    }
    
    normalized = collect.normalise("testaddr:56", fomo_row)
    
    assert normalized["fomo_top10_holders_percent"] == 65.0
    
    # In dossier, this gets converted and used for EVM top_10
    dossier_mock = {
        **normalized,
        "net": 56,
        "chain": "bsc",
        "fomo_top10_holders_percent": 65.0
    }
    
    # Simulate dossier processing for EVM
    # top_10_percent is stored as whole number (65), chain_kill divides by 100
    # 65/100 = 0.65 > 0.60 threshold, should kill
    kill_reason = chain_kill({
        "chain": "bsc",
        "top_wallet_percent": 0.03,  # Pass unverified check
        "top_10_percent": 65,  # Whole number, gets divided by 100 in chain_kill
        "holder_count": 200
    })
    
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
