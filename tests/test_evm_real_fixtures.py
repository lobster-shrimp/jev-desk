"""
Tests using real captured API responses as fixtures.
"""
import pytest
import tempfile
import os
import sqlite3
import sys
sys.path.insert(0, '/workspace')

from unittest.mock import Mock, patch
import evm_holders
from eth_abi import encode
import time


@pytest.fixture(autouse=True)
def clear_evm_cache():
    """Clear EVM holder cache before each test."""
    evm_holders._cache.clear()
    yield
    evm_holders._cache.clear()


def test_honeypot_real_tart_fixture():
    """Test with real captured TART Honeypot response."""
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
        
        # Real captured TART response from Honeypot.is
        real_response = {
            "totalSupply": "10000000000000000000",  # String decimal
            "holders": [
                {"address": "0x000000000000000000000000000000000000dEaD", "balance": "1801818258707251413", "alias": "", "isContract": False},
                {"address": "0x038C92ac8269c9A648BA06e434056706Bc7832cE", "balance": "1635165296410634605", "alias": "", "isContract": True},
                {"address": "0x2a9cC2df5F17d8f0553C41d43ea85C823CB0C3d8", "balance": "1571995254631088860", "alias": "", "isContract": True},
                {"address": "0x9E04C9387174Da0db8753Bd0f0908BA3F3536953", "balance": "1372311374000000000", "alias": "", "isContract": True},
            ]
        }
        
        with patch('evm_holders.requests.get') as mock_get, \
             patch('evm_holders.requests.post') as mock_post:
            
            mock_get.return_value = Mock(status_code=200, json=lambda: real_response)
            
            # Multicall returns not pools
            mock_post.return_value = Mock(
                status_code=200,
                json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b'')]*8]).hex()}
            )
            
            result = evm_holders.evm_holder_concentration(56, "0x7ab8d02cbb51ff7223fde700eaaa2a91bf750314", [], 120, db)
            
            assert result.ok
            assert result.source == "honeypot"
            
            # Verify string conversion worked
            assert result.top_wallet is not None
            assert result.top_10 is not None
            
            # Dead address should be excluded
            excluded_addrs = [addr.lower() for addr, _, _ in result.excluded]
            assert "0x000000000000000000000000000000000000dead" in excluded_addrs
    
    finally:
        db.close()
        os.unlink(db_path)
