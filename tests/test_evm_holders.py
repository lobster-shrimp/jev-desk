"""
Tests for EVM holder concentration module (evm_holders.py).

Covers:
- Honeypot.is API for BSC and Base
- Robinhood RPC Transfer-log fold
- GoPlus fallback
- Pool/locker/burn exclusions
- Dossier integration
- Filter integration (top_wallet/top_10 thresholds, fail-closed paths)
"""
import json
import logging
import os
import pathlib
import sqlite3
import sys
import tempfile
from unittest.mock import Mock, patch, call

import pytest

# Add workspace to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import evm_holders
import book
import collect
from filter import chain_kill
from thresholds import HARD


# Pytest fixture to clear cache before each test
@pytest.fixture(autouse=True)
def clear_evm_cache():
    """Clear EVM holder cache before each test."""
    evm_holders._cache.clear()
    yield
    evm_holders._cache.clear()


# Test fixtures

def test_honeypot_fitcoin_pair_excluded_pass():
    """FITCOIN: raw 39% pair excluded -> real 2.15%/16.3% passes."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xPAIR123", "balance": 390000000, "isContract": True},  # 39% pair
                {"address": "0xWHALE1", "balance": 21500000, "isContract": False},   # 2.15% real top
                {"address": "0xHOLDER2", "balance": 20000000, "isContract": False},  # 2%
                {"address": "0xHOLDER3", "balance": 19000000, "isContract": False},
                {"address": "0xHOLDER4", "balance": 18000000, "isContract": False},
                {"address": "0xHOLDER5", "balance": 17000000, "isContract": False},
                {"address": "0xHOLDER6", "balance": 16000000, "isContract": False},
                {"address": "0xHOLDER7", "balance": 15000000, "isContract": False},
                {"address": "0xHOLDER8", "balance": 14000000, "isContract": False},
                {"address": "0xHOLDER9", "balance": 13000000, "isContract": False},
                {"address": "0xHOLDER10", "balance": 12000000, "isContract": False},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=["0xPAIR123"],
                age_min=30,
                db=db
            )
        
        assert result.ok
        assert result.source == "honeypot"
        assert result.top_wallet is not None
        assert abs(result.top_wallet - 0.0215) < 0.001  # ~2.15%
        assert result.top_10 is not None
        # top_10 = (21.5 + 20 + 19 + 18 + 17 + 16 + 15 + 14 + 13 + 12) / 1000 * 100 (excluding pair)
        assert abs(result.top_10 - 16.55) < 1  # ~16.55%
        
        # Check exclusions
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert "0xpair123" in exclusions
        assert exclusions["0xpair123"] == "pair_dexscreener"
        
        # Should pass chain_kill thresholds
        d = {"chain": "bsc", "top_wallet_percent": result.top_wallet, "top_10_percent": result.top_10, "holder_count": 100}
        assert chain_kill(d) is None  # Pass
        
    finally:
        db.close()
        os.unlink(db_path)


def test_honeypot_whale_kills():
    """SpaceXSI: 39.6% top wallet, 47.6% top_10 kills."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xWHALE", "balance": 396000000, "isContract": False},   # 39.6%
                {"address": "0xHOLDER2", "balance": 80000000, "isContract": False},  # 8%
                {"address": "0xHOLDER3", "balance": 50000000, "isContract": False},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        assert result.top_wallet is not None
        assert result.top_wallet > 0.39  # 39.6%
        assert result.top_10 is not None
        assert result.top_10 > 47  # 47.6%
        
        # Should kill on top_wallet
        d = {"chain": "bsc", "top_wallet_percent": result.top_wallet, "top_10_percent": result.top_10, "holder_count": 100}
        assert chain_kill(d) == "top_wallet"  # Kills on whale
        
    finally:
        db.close()
        os.unlink(db_path)


def test_honeypot_burn_denominator():
    """ZNHJ: 79% burned -> 25.1%/68.3% after adjusting supply."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        total_supply = 1000000000
        burned = int(total_supply * 0.79)  # 79% burned
        remaining = total_supply - burned
        
        mock_response = {
            "totalSupply": total_supply,
            "holders": [
                {"address": "0x0000000000000000000000000000000000000000", "balance": burned, "isContract": False},  # Burn
                {"address": "0xWHALE", "balance": int(remaining * 0.251), "isContract": False},  # 25.1% of remaining
                {"address": "0xHOLDER2", "balance": int(remaining * 0.15), "isContract": False},
                {"address": "0xHOLDER3", "balance": int(remaining * 0.10), "isContract": False},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        # Burns excluded, denominator is remaining supply
        assert result.top_wallet is not None
        assert abs(result.top_wallet - 0.251) < 0.01  # ~25.1%
        
        # Should kill on top_wallet
        d = {"chain": "bsc", "top_wallet_percent": result.top_wallet, "top_10_percent": result.top_10, "holder_count": 100}
        assert chain_kill(d) == "top_wallet"
        
    finally:
        db.close()
        os.unlink(db_path)


def test_pool_manager_excluded():
    """Robinhood PoolManager (Uniswap v4) excluded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0x8366a39cc670b4001a1121b8f6a443a643e40951", "balance": 400000000, "isContract": True},  # Robinhood PoolManager
                {"address": "0xWHALE", "balance": 30000000, "isContract": False},  # 3% real
            ]
        }
        
        # Mock RPC fold to return complete data
        def mock_fold(token, timeout, db, call_count):
            return {
                "balances": {
                    "0x8366a39cc670b4001a1121b8f6a443a643e40951": 400000000,
                    "0xwhale": 30000000,
                },
                "supply": 1000000000,
                "complete": True
            }, None
        
        with patch('evm_holders._holders_rpc_fold', side_effect=mock_fold):
            result = evm_holders.evm_holder_concentration(
                chain_id=4663,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        assert result.source == "rpc_fold"
        # PoolManager excluded
        assert result.top_wallet is not None
        assert abs(result.top_wallet - 0.03) < 0.01  # ~3%
        
        # Check exclusions
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert "0x8366a39cc670b4001a1121b8f6a443a643e40951".lower() in exclusions
        assert exclusions["0x8366a39cc670b4001a1121b8f6a443a643e40951".lower()] == "pool_contract"
        
    finally:
        db.close()
        os.unlink(db_path)


def test_flap_portal_excluded():
    """Flap Portal launchpad curve excluded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0", "balance": 300000000, "isContract": True},  # Flap Portal BSC
                {"address": "0xWHALE", "balance": 40000000, "isContract": False},  # 4%
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        assert result.top_wallet is not None
        assert abs(result.top_wallet - 0.04) < 0.01  # ~4%
        
        # Check exclusions
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0" in exclusions
        
    finally:
        db.close()
        os.unlink(db_path)


def test_pinklock_permanent_excluded():
    """PinkLock with permanent lock excluded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        # Mock GoPlus response with permanent lock (using REAL schema)
        mock_goplus = {
            "holders": [
                {
                    "address": "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe",
                    "is_locked": "1",
                    "locked_detail": [
                        {
                            "end_time": "permanent"
                        }
                    ]
                }
            ],
            "dex": []
        }
        
        mock_honeypot = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe", "balance": 400000000, "isContract": True},  # PinkLock02
                {"address": "0xWHALE", "balance": 50000000, "isContract": False},  # 5%
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            def side_effect(url, **kwargs):
                if "honeypot" in url:
                    return Mock(status_code=200, json=lambda: mock_honeypot)
                elif "gopluslabs" in url:
                    return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xtoken": mock_goplus}})
                return Mock(status_code=404)
            
            mock_get.side_effect = side_effect
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=120,  # >= 120m to enable GoPlus
                db=db
            )
        
        assert result.ok
        # PinkLock excluded
        assert result.top_wallet is not None
        assert abs(result.top_wallet - 0.05) < 0.01
        
        # Check exclusions (both conditional_lockers match and goplus permanent lock)
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe" in exclusions
        
    finally:
        db.close()
        os.unlink(db_path)


def test_locker_unlocking_under_7d_counted():
    """Locker unlocking in <7 days is NOT excluded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        import time
        unlock_time = time.time() + 3 * 86400  # 3 days from now
        
        mock_goplus = {
            "holders": [],
            "dex": [],
            "locked_detail": [
                {
                    "holder": "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe",
                    "is_permanent": False,
                    "end_time": str(int(unlock_time))
                }
            ]
        }
        
        mock_honeypot = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe", "balance": 400000000, "isContract": True},  # Unlocking soon
                {"address": "0xWHALE", "balance": 50000000, "isContract": False},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            def side_effect(url, **kwargs):
                if "honeypot" in url:
                    return Mock(status_code=200, json=lambda: mock_honeypot)
                elif "gopluslabs" in url:
                    return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xtoken": mock_goplus}})
                return Mock(status_code=404)
            
            mock_get.side_effect = side_effect
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=120,
                db=db
            )
        
        assert result.ok
        # Locker NOT excluded (unlocks <7d)
        assert result.top_wallet is not None
        assert result.top_wallet >= 0.4  # 40% counted
        
    finally:
        db.close()
        os.unlink(db_path)


def test_unknown_contract_counted():
    """WOJAK: 25.5% unknown contract kills (not excluded)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xUNKNOWNCONTRACT", "balance": 255000000, "isContract": True},  # 25.5% unknown contract
                {"address": "0xWHALE", "balance": 50000000, "isContract": False},
            ]
        }
        
        # Mock Multicall to return nothing (not a pair)
        with patch('evm_holders.requests.get') as mock_get, \
             patch('evm_holders.requests.post') as mock_post:
            
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            # Multicall returns nothing (not a pair)
            mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x0000000000000000000000000000000000000000000000000000000000000000"})
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        # Unknown contract NOT excluded
        assert result.top_wallet is not None
        assert result.top_wallet > 0.25  # 25.5% counted
        
        # Should kill on top_wallet
        d = {"chain": "bsc", "top_wallet_percent": result.top_wallet, "top_10_percent": result.top_10, "holder_count": 100}
        assert chain_kill(d) == "top_wallet"
        
    finally:
        db.close()
        os.unlink(db_path)


def test_robinhood_fold_incomplete():
    """Robinhood fold incomplete -> top_wallet_unverified."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        # Mock incomplete fold
        def mock_fold(token, timeout, db, call_count):
            return {
                "balances": {"0xwhale": 300000000},
                "supply": 1000000000,
                "complete": False  # Incomplete
            }, None
        
        with patch('evm_holders._holders_rpc_fold', side_effect=mock_fold):
            result = evm_holders.evm_holder_concentration(
                chain_id=4663,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert not result.ok
        assert result.error == "transient:incomplete_fold"  # Transient: has some balances
        assert result.top_wallet is None
        
        # Should fail closed
        d = {"chain": "robinhood", "top_wallet_percent": result.top_wallet, "holder_count": 100}
        assert chain_kill(d) == "top_wallet_unverified"
        
    finally:
        db.close()
        os.unlink(db_path)


def test_honeypot_invalid_chain():
    """Honeypot 'Invalid chain' -> unavailable."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=400)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert not result.ok
        assert result.error == "bad_request"
        
    finally:
        db.close()
        os.unlink(db_path)


def test_honeypot_timeout():
    """Honeypot timeout -> unavailable."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        with patch('evm_holders.requests.get') as mock_get:
            import requests
            mock_get.side_effect = requests.Timeout("timeout")
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert not result.ok
        assert result.error == "transient:timeout"  # Transient flag added
        
    finally:
        db.close()
        os.unlink(db_path)


def test_units_at_gates():
    """Units: top_wallet 0.0546 kills, top_10 61 kills."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        # Test top_wallet threshold (0.05)
        d1 = {"chain": "bsc", "top_wallet_percent": 0.0546, "top_10_percent": 30, "holder_count": 100}
        assert chain_kill(d1) == "top_wallet"  # 5.46% > 5%
        
        d2 = {"chain": "bsc", "top_wallet_percent": 0.049, "top_10_percent": 30, "holder_count": 100}
        assert chain_kill(d2) is None  # 4.9% <= 5%, pass
        
        # Test top_10 threshold (60%)
        d3 = {"chain": "bsc", "top_wallet_percent": 0.03, "top_10_percent": 61, "holder_count": 100}
        assert chain_kill(d3) == "top_10"  # 61% > 60%
        
        d4 = {"chain": "bsc", "top_wallet_percent": 0.03, "top_10_percent": 59, "holder_count": 100}
        assert chain_kill(d4) is None  # 59% <= 60%, pass
        
    finally:
        db.close()
        os.unlink(db_path)


def test_evm_with_ok_result_no_longer_unverified():
    """EVM with ok result should NOT be top_wallet_unverified."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xWHALE", "balance": 30000000, "isContract": False},  # 3%
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result.ok
        assert result.top_wallet is not None
        assert result.top_wallet < 0.05  # Under threshold
        
        # Should NOT be top_wallet_unverified
        d = {"chain": "bsc", "top_wallet_percent": result.top_wallet, "top_10_percent": result.top_10, "holder_count": 100}
        assert chain_kill(d) is None  # Pass, not unverified
        
    finally:
        db.close()
        os.unlink(db_path)


def test_dossier_integration_bsc_pass():
    """BSC dossier with pair exclusion passes."""
    token_dict = {
        "ticker": "TEST",
        "addr": "0xTOKEN",
        "net": 56,
        "age_minutes": 30,
        "pair_address": "0xPAIR",
        "holder_count": 200
    }
    
    mock_gt_response = {
        "data": {
            "attributes": {
                "holders": None,  # No GT data, will use EVM holder check
                "developer_holding_percentage": None,
                "gt_score_details": None,
                "is_honeypot": False,
                "description": None,
                "twitter_handle": None
            }
        }
    }
    
    mock_honeypot = {
        "totalSupply": 1000000000,
        "holders": [
            {"address": "0xPAIR", "balance": 400000000, "isContract": True},
            {"address": "0xWHALE", "balance": 30000000, "isContract": False},  # 3% after exclusion
        ]
    }
    
    def get_side_effect(url, **kwargs):
        """Route requests to correct mocks."""
        if "geckoterminal" in url:
            return Mock(status_code=200, json=lambda: mock_gt_response)
        elif "honeypot" in url:
            return Mock(status_code=200, json=lambda: mock_honeypot)
        else:
            return Mock(status_code=404)
    
    with patch('collect.requests.get', side_effect=get_side_effect), \
         patch('evm_holders.requests.get', side_effect=get_side_effect):
            
        dossier = collect.dossier(token_dict)
    
    assert dossier["top_wallet_percent"] is not None
    assert dossier["top_wallet_percent"] < 0.05  # Pass
    assert dossier["evm_holder_source"] == "honeypot"
    
    # Chain kill should pass
    assert chain_kill(dossier) is None


def test_dossier_integration_bsc_whale_kill():
    """BSC dossier with whale kills."""
    token_dict = {
        "ticker": "WHALE",
        "addr": "0xTOKEN",
        "net": 56,
        "age_minutes": 30,
        "pair_address": None,
        "holder_count": 200
    }
    
    mock_gt_response = {
        "data": {
            "attributes": {
                "holders": None,
                "developer_holding_percentage": None,
                "gt_score_details": None,
                "is_honeypot": False,
                "description": None,
                "twitter_handle": None
            }
        }
    }
    
    mock_honeypot = {
        "totalSupply": 1000000000,
        "holders": [
            {"address": "0xWHALE", "balance": 400000000, "isContract": False},  # 40%
        ]
    }
    
    def get_side_effect(url, **kwargs):
        if "geckoterminal" in url:
            return Mock(status_code=200, json=lambda: mock_gt_response)
        elif "honeypot" in url:
            return Mock(status_code=200, json=lambda: mock_honeypot)
        else:
            return Mock(status_code=404)
    
    with patch('collect.requests.get', side_effect=get_side_effect), \
         patch('evm_holders.requests.get', side_effect=get_side_effect):
            
        dossier = collect.dossier(token_dict)
    
    assert dossier["top_wallet_percent"] is not None
    assert dossier["top_wallet_percent"] > 0.05  # Whale
    
    # Chain kill should kill on top_wallet
    assert chain_kill(dossier) == "top_wallet"


def test_dossier_integration_robinhood_poolmanager():
    """Robinhood dossier with PoolManager exclusion passes."""
    token_dict = {
        "ticker": "TEST",
        "addr": "0xTOKEN",
        "net": 4663,
        "age_minutes": 30,
        "pair_address": None
    }
    
    mock_gt_response = {
        "data": {
            "attributes": {
                "holders": {"count": 200},
                "developer_holding_percentage": None,
                "gt_score_details": None,
                "is_honeypot": None,  # No honeypot check on Robinhood
                "description": None,
                "twitter_handle": None
            }
        }
    }
    
    def mock_fold(token, timeout, db, call_count):
        return {
            "balances": {
                "0x8366a39cc670b4001a1121b8f6a443a643e40951": 400000000,  # PoolManager
                "0xwhale": 30000000,  # 3% real
            },
            "supply": 1000000000,
            "complete": True
        }, None
    
    with patch('collect.requests.get') as mock_get, \
         patch('evm_holders._holders_rpc_fold', side_effect=mock_fold):
        
        mock_get.return_value = Mock(status_code=200, json=lambda: mock_gt_response)
        
        dossier = collect.dossier(token_dict)
    
    assert dossier["top_wallet_percent"] is not None
    assert dossier["top_wallet_percent"] < 0.05  # Pass
    assert dossier["evm_holder_source"] == "rpc_fold"
    
    # Chain kill should pass
    assert chain_kill(dossier) is None


def test_dossier_integration_robinhood_incomplete():
    """Robinhood dossier with incomplete fold -> top_wallet_unverified."""
    token_dict = {
        "ticker": "TEST",
        "addr": "0xTOKEN",
        "net": 4663,
        "age_minutes": 30,
        "pair_address": None
    }
    
    mock_gt_response = {
        "data": {
            "attributes": {
                "holders": {"count": 200},
                "developer_holding_percentage": None,
                "gt_score_details": None,
                "is_honeypot": None,
                "description": None,
                "twitter_handle": None
            }
        }
    }
    
    def mock_fold(token, timeout, db, call_count):
        return {
            "balances": {"0xwhale": 300000000},
            "supply": 1000000000,
            "complete": False  # Incomplete
        }, None
    
    with patch('collect.requests.get') as mock_get, \
         patch('evm_holders._holders_rpc_fold', side_effect=mock_fold):
        
        mock_get.return_value = Mock(status_code=200, json=lambda: mock_gt_response)
        
        dossier = collect.dossier(token_dict)
    
    assert dossier["top_wallet_percent"] is None  # Fail closed
    assert dossier["evm_holder_source"] == "unavailable"
    assert dossier["evm_holder_error"] == "transient:incomplete_fold"  # Transient prefix
    
    # Chain kill should fail closed with holders_pending (transient, shorter bench for retry)
    assert chain_kill(dossier) == "holders_pending"  # 15 min bench


def test_secret_scrubbing():
    """All exception/log strings go through safe_err."""
    from secret_utils import safe_err
    
    # Test that evm_holders uses safe_err for exceptions
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        # Mock to raise exception with secret
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection failed: api-key=SECRET123")
            
            result = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert not result.ok
        # Error should be scrubbed
        assert "SECRET123" not in result.error
        assert "REDACTED" in result.error or "Exception" in result.error
        
    finally:
        db.close()
        os.unlink(db_path)


def test_cache():
    """Result cache works (10 min TTL)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = sqlite3.connect(db_path)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS evm_holder_cache(
          chain_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          last_block INTEGER NOT NULL,
          balances_json TEXT NOT NULL,
          supply TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (chain_id, token)
        );
        """)
        
        # Clear in-memory cache
        evm_holders._cache.clear()
        
        mock_response = {
            "totalSupply": 1000000000,
            "holders": [
                {"address": "0xWHALE", "balance": 30000000, "isContract": False},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            # First call
            result1 = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
            
            # Second call (should hit cache)
            result2 = evm_holders.evm_holder_concentration(
                chain_id=56,
                token="0xTOKEN",
                pair_addrs=[],
                age_min=30,
                db=db
            )
        
        assert result1.ok
        assert result1.source == "honeypot"
        
        assert result2.ok
        assert result2.source == "cache"  # From cache
        
        # Only called once (second hit cache)
        assert mock_get.call_count == 1
        
    finally:
        db.close()
        os.unlink(db_path)
