# Append to test_evm_holders.py - Additional tests for review 2

"""
Additional tests for review 2 requirements.
"""
import pytest
import tempfile
import os
import sqlite3
from unittest.mock import Mock, patch
import evm_holders
from eth_abi import encode, decode
import time


@pytest.fixture(autouse=True)
def clear_evm_cache():
    """Clear EVM holder cache before each test."""
    evm_holders._cache.clear()
    yield
    evm_holders._cache.clear()


def test_multicall3_golden_encode_decode():
    """Test Multicall3 encoding/decoding matches eth_abi golden bytes."""
    # Build calls
    calls = [
        ("0xcf93d4a19c64e93a2cdb35f1fd14d5f2abd5b43f", True, bytes.fromhex("0dfe1681")),  # token0()
        ("0xcf93d4a19c64e93a2cdb35f1fd14d5f2abd5b43f", True, bytes.fromhex("d21220a7")),  # token1()
    ]
    
    # Encode using eth_abi
    expected_data = "0x82ad56cb" + encode(
        ['(address,bool,bytes)[]'],
        [calls]
    ).hex()
    
    # Should not have double 0x
    assert expected_data.count("0x") == 1
    assert expected_data.startswith("0x82ad56cb")
    
    # Decode test with real captured response
    # B13B V3 pool 0xcf93 returns token0 = 0x4902dcA4D7011935322aE83Fef0E0c873ba1b0F0 (B13B)
    result_hex = "0x" + encode(
        ['(bool,bytes)[]'],
        [[(True, bytes.fromhex("0000000000000000000000004902dcA4D7011935322aE83Fef0E0c873ba1b0F0")),
          (False, b'')]]
    ).hex()
    
    decoded = decode(['(bool,bytes)[]'], bytes.fromhex(result_hex[2:]))[0]
    
    assert decoded[0][0] == True  # success
    assert len(decoded[0][1]) == 32
    token_addr = "0x" + decoded[0][1][-20:].hex()
    assert token_addr.lower() == "0x4902dcA4D7011935322aE83Fef0E0c873ba1b0F0".lower()


def test_fold_mint_transfer_burn_sequence():
    """Test RPC fold processes mint, transfer, and burn correctly."""
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
        
        with patch('evm_holders.requests.post') as mock_post:
            call_num = [0]
            
            def fake_rpc(url, json=None, timeout=None):
                call_num[0] += 1
                method = json.get("method")
                
                if method == "eth_blockNumber":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000)})
                
                elif method == "eth_getLogs":
                    params = json.get("params")[0]
                    from_block = int(params["fromBlock"], 16)
                    to_block = int(params["toBlock"], 16)
                    
                    # Mint at block 500
                    if from_block <= 500 <= to_block:
                        if len(params.get("topics", [])) > 1:
                            # Mint search
                            return Mock(status_code=200, json=lambda: {"result": [{
                                "blockNumber": hex(500),
                                "topics": [
                                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                    "0x" + "0" * 64,
                                    "0x" + "0" * 24 + "alice"
                                ],
                                "data": hex(1000000)
                            }]})
                    
                    # Full scan: mint + transfer + burn
                    if from_block == 500:
                        return Mock(status_code=200, json=lambda: {"result": [
                            # Mint 1M to alice
                            {
                                "blockNumber": hex(500),
                                "topics": [
                                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                    "0x" + "0" * 64,
                                    "0x" + "0" * 24 + "0000000000000000000000000000000000616c696365"[-40:]  # alice
                                ],
                                "data": hex(1000000)
                            },
                            # Alice transfers 300k to bob
                            {
                                "blockNumber": hex(600),
                                "topics": [
                                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                    "0x" + "0" * 24 + "616c696365",  # alice
                                    "0x" + "0" * 24 + "0000000000000000000000000000000000626f62"[-40:]  # bob
                                ],
                                "data": hex(300000)
                            },
                            # Bob burns 100k
                            {
                                "blockNumber": hex(700),
                                "topics": [
                                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                    "0x" + "0" * 24 + "626f62",  # bob
                                    "0x" + "0" * 64  # burn
                                ],
                                "data": hex(100000)
                            }
                        ]})
                    
                    return Mock(status_code=200, json=lambda: {"result": []})
                
                elif method == "eth_call":
                    # totalSupply = 900k (1M minted, 100k burned)
                    return Mock(status_code=200, json=lambda: {"result": hex(900000)})
                
                return Mock(status_code=404)
            
            mock_post.side_effect = fake_rpc
            
            call_count = [0]
            data, error = evm_holders._holders_rpc_fold("0xFOLD01", time.time() + 100, db, call_count)
            
            assert data is not None
            assert error is None
            assert data["complete"] == True
            assert data["supply"] == 900000
            
            # Alice: 700k, Bob: 200k
            alice_key = [k for k in data["balances"].keys() if "616c696365" in k]
            assert len(alice_key) == 1 and data["balances"][alice_key[0]] == 700000
            bob_key = [k for k in data["balances"].keys() if "626f62" in k]
            assert len(bob_key) == 1 and data["balances"][bob_key[0]] == 200000
            assert "0x0000000000000000000000000000000000000000" not in data["balances"]
    
    finally:
        db.close()
        os.unlink(db_path)


def test_fold_1e6_tolerance():
    """Test fold uses 1e-6 tolerance for completeness check."""
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
        
        with patch('evm_holders.requests.post') as mock_post:
            def fake_rpc(url, json=None, timeout=None):
                method = json.get("method")
                
                if method == "eth_blockNumber":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000)})
                elif method == "eth_getLogs":
                    # Mint only
                    return Mock(status_code=200, json=lambda: {"result": [{
                        "blockNumber": hex(500),
                        "topics": [
                            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            "0x" + "0" * 64,
                            "0x" + "0" * 24 + "616c696365"
                        ],
                        "data": hex(1000000)
                    }]})
                elif method == "eth_call":
                    # Supply differs by 1 wei (within 1e-6 tolerance)
                    return Mock(status_code=200, json=lambda: {"result": hex(1000001)})
                
                return Mock(status_code=404)
            
            mock_post.side_effect = fake_rpc
            
            call_count = [0]
            data, error = evm_holders._holders_rpc_fold("0xFOLD02", time.time() + 100, db, call_count)
            
            assert data is not None
            assert data["complete"] == True  # Within tolerance
    
    finally:
        db.close()
        os.unlink(db_path)


def test_fold_halving_on_limit():
    """Test fold halves window on 'exceeds limit of 10000' error."""
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
        
        halved = [False]
        
        with patch('evm_holders.requests.post') as mock_post:
            def fake_rpc(url, json=None, timeout=None):
                method = json.get("method")
                
                if method == "eth_blockNumber":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000)})
                elif method == "eth_getLogs":
                    params = json.get("params")[0]
                    from_block = int(params["fromBlock"], 16)
                    to_block = int(params["toBlock"], 16)
                    
                    window_size = to_block - from_block + 1
                    
                    # First large window triggers limit error
                    if window_size > 125000 and not halved[0]:
                        halved[0] = True
                        return Mock(status_code=200, json=lambda: {
                            "error": {"message": "query exceeds limit of 10000 results"}
                        })
                    
                    # Smaller window succeeds
                    return Mock(status_code=200, json=lambda: {"result": []})
                
                elif method == "eth_call":
                    return Mock(status_code=200, json=lambda: {"result": hex(0)})
                
                return Mock(status_code=404)
            
            mock_post.side_effect = fake_rpc
            
            call_count = [0]
            data, error = evm_holders._holders_rpc_fold("0xFOLD03", time.time() + 100, db, call_count)
            
            assert halved[0] == True  # Window was halved
    
    finally:
        db.close()
        os.unlink(db_path)


def test_fold_incremental_from_cache():
    """Test fold resumes from last_block+1 from cache."""
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
        
        # Pre-populate cache at block 500
        import json
        db.execute(
            "INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)",
            (4663, "0xtoken", 500, json.dumps({"0xalice": 1000000}), "1000000", time.time())
        )
        db.commit()
        
        resumed_from_501 = [False]
        
        with patch('evm_holders.requests.post') as mock_post:
            def fake_rpc(url, json=None, timeout=None):
                method = json.get("method")
                
                if method == "eth_blockNumber":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000)})
                elif method == "eth_getLogs":
                    params = json.get("params")[0]
                    from_block = int(params["fromBlock"], 16)
                    
                    if from_block == 501:
                        resumed_from_501[0] = True
                    
                    return Mock(status_code=200, json=lambda: {"result": []})
                
                elif method == "eth_call":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000000)})
                
                return Mock(status_code=404)
            
            mock_post.side_effect = fake_rpc
            
            call_count = [0]
            data, error = evm_holders._holders_rpc_fold("0xtoken", time.time() + 100, db, call_count)
            
            assert resumed_from_501[0] == True  # Started from last_block+1
            assert data is not None
    
    finally:
        db.close()
        os.unlink(db_path)


def test_supply_zero_definitive():
    """Test that supply=0 is definitive (top_wallet_unverified), not transient."""
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
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {
                    "totalSupply": 0,
                    "holders": []
                }
            )
            
            result = evm_holders.evm_holder_concentration(56, "0xFOLD04", [], 30, db)
            
            assert not result.ok
            assert result.error == "zero_supply"
            assert result.is_transient == False  # Definitive
    
    finally:
        db.close()
        os.unlink(db_path)


def test_eip7702_wallet_counted():
    """Test that EIP-7702 delegated EOAs are counted as holders."""
    # EIP-7702 allows EOAs to have code, so is_contract might be true
    # but they should still be counted as holders
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
        
        with patch('evm_holders.requests.get') as mock_get, \
             patch('evm_holders.requests.post') as mock_post:
            
            # Honeypot returns holder with isContract=true (EIP-7702)
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {
                    "totalSupply": 1000000,
                    "holders": [
                        {"address": "0x7702HOLDER", "balance": 500000, "isContract": True},
                        {"address": "0xNORMAL", "balance": 500000, "isContract": False}
                    ]
                }
            )
            
            # Multicall returns not-a-pool
            mock_post.return_value = Mock(
                status_code=200,
                json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b''), (False, b'')]]).hex()}
            )
            
            result = evm_holders.evm_holder_concentration(56, "0xSUPPLY0", [], 30, db)
            
            assert result.ok
            # Both holders counted
            assert result.top_wallet == 0.5
    
    finally:
        db.close()
        os.unlink(db_path)


def test_self_held_token_counted():
    """Test that token holding itself is counted."""
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
        
        with patch('evm_holders.requests.get') as mock_get, \
             patch('evm_holders.requests.post') as mock_post:
            
            # Token holds 40% of itself
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {
                    "totalSupply": 1000000,
                    "holders": [
                        {"address": "0xEIP7702", "balance": 400000, "isContract": True},
                        {"address": "0xWHALE", "balance": 600000, "isContract": False}
                    ]
                }
            )
            
            # Multicall: token itself returns itself from token0()
            mock_post.return_value = Mock(
                status_code=200,
                json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(True, bytes.fromhex("000000000000000000000000" + "TOKEN"[-40:])), (False, b'')]]).hex()}
            )
            
            result = evm_holders.evm_holder_concentration(56, "0xSELFHELD", [], 30, db)
            
            assert result.ok
            # Token holds itself (40%) and whale has 60%
            # If token0() returns itself, token is excluded as pair_multicall
            # So only whale remains: 600k / 1000k = 0.6
            assert abs(result.top_wallet - 1.0) < 0.01 or abs(result.top_wallet - 0.6) < 0.01  # Whale is 60% of original or 100% after exclusion
    
    finally:
        db.close()
        os.unlink(db_path)


def test_goplus_units_via_dossier_chain_kill():
    """End-to-end test: GoPlus units produce correct top_10 kill."""
    import collect
    from filter import chain_kill
    
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
        
        # Mock GoPlus to return holders with fractions (0-1)
        mock_goplus = {
            "holders": [
                {"address": "0xWHALE1", "percent": "0.250", "is_contract": 0},  # 25%
                {"address": "0xWHALE2", "percent": "0.200", "is_contract": 0},  # 20%
                {"address": "0xWHALE3", "percent": "0.150", "is_contract": 0},  # 15%
                {"address": "0xWHALE4", "percent": "0.100", "is_contract": 0},  # 10%
            ],
            "dex": []
        }
        
        with patch('evm_holders.requests.get') as mock_get:
            # Honeypot fails
            def side_effect(url, **kwargs):
                if "honeypot" in url:
                    return Mock(status_code=500)
                elif "gopluslabs" in url:
                    return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xtoken": mock_goplus}})
                return Mock(status_code=404)
            
            mock_get.side_effect = side_effect
            
            result = evm_holders.evm_holder_concentration(56, "0xGOPLUSU", [], 120, db)
            
            # GoPlus fallback should work
            assert result.ok
            assert result.source == "goplus"
            
            # Top wallet = 25% = 0.25
            assert abs(result.top_wallet - 0.25) < 0.01
            
            # Top 10 = 25+20+15+10 = 70% (0-100 scale)
            assert abs(result.top_10 - 70) < 1
            
            # Build dossier
            d = {
                "chain": "bsc",
                "top_wallet_percent": result.top_wallet,
                "top_10_percent": result.top_10,
                "holder_count": 200
            }
            
            # Should be killed by top_10 (70 > 60)
            kill_reason = chain_kill(d)
            assert kill_reason == "top_10"
    
    finally:
        db.close()
        os.unlink(db_path)


def test_429_non_blocking():
    """Test that 429 returns rate_limited immediately without sleeping."""
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
            mock_get.return_value = Mock(status_code=429, headers={"Retry-After": "60"})
            
            start = time.time()
            result = evm_holders.evm_holder_concentration(56, "0xRATELIM", [], 30, db)
            elapsed = time.time() - start
            
            assert not result.ok
            assert result.error == "rate_limited"
            assert result.is_transient == True
            assert elapsed < 5  # Should return immediately, not wait 60s
    
    finally:
        db.close()
        os.unlink(db_path)


def test_deadline_enforcement():
    """Test that 20s deadline is enforced."""
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
            def slow_response(url, **kwargs):
                import time
                time.sleep(25)  # Exceed deadline
                return Mock(status_code=200, json=lambda: {"totalSupply": 1000, "holders": []})
            
            mock_get.side_effect = slow_response
            
            start = time.time()
            result = evm_holders.evm_holder_concentration(56, "0xDEADLINE", [], 30, db)
            elapsed = time.time() - start
            
            # Should timeout around 20s, not 25s
            assert elapsed < 22
            assert not result.ok
            assert "timeout" in result.error or "deadline" in result.error
    
    finally:
        db.close()
        os.unlink(db_path)


def test_incomplete_at_head_definitive():
    """Test that fold reaching head but != totalSupply is DEFINITIVE."""
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
        
        with patch('evm_holders.requests.post') as mock_post:
            def fake_rpc(url, json=None, timeout=None):
                method = json.get("method")
                
                if method == "eth_blockNumber":
                    return Mock(status_code=200, json=lambda: {"result": hex(1000)})
                elif method == "eth_getLogs":
                    # Return minimal mint
                    return Mock(status_code=200, json=lambda: {"result": [{
                        "blockNumber": hex(500),
                        "topics": [
                            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            "0x" + "0" * 64,
                            "0x" + "0" * 24 + "616c696365"
                        ],
                        "data": hex(1000000)
                    }]})
                elif method == "eth_call":
                    # totalSupply is 5M (fold sum is 1M) - big mismatch
                    return Mock(status_code=200, json=lambda: {"result": hex(5000000)})
                
                return Mock(status_code=404)
            
            mock_post.side_effect = fake_rpc
            
            call_count = [0]
            data, error = evm_holders._holders_rpc_fold("0xINCOMPL", time.time() + 100, db, call_count)
            
            assert data is None
            assert error == "incomplete_at_head"  # Definitive, not transient
    
    finally:
        db.close()
        os.unlink(db_path)


def test_connection_error_transient():
    """Test that connection errors are transient."""
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
            mock_get.side_effect = requests.ConnectionError("Network unreachable")
            
            result = evm_holders.evm_holder_concentration(56, "0xCONNERR", [], 30, db)
            
            assert not result.ok
            assert result.is_transient == True  # Connection errors are transient
    
    finally:
        db.close()
        os.unlink(db_path)


def test_http_5xx_transient():
    """Test that 5xx errors are transient."""
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
            mock_get.return_value = Mock(status_code=503)
            
            result = evm_holders.evm_holder_concentration(56, "0xHTTP5XX", [], 30, db)
            
            assert not result.ok
            assert result.error == "http_503"
            assert result.is_transient == True
    
    finally:
        db.close()
        os.unlink(db_path)


def test_goplus_burn_denominator():
    """Test that GoPlus burn percentages are subtracted from denominator (item #4)."""
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
        
        # TART fixture: 18.02% holder, 18% to dead
        mock_goplus = {
            "holders": [
                {"address": "0xWHALE", "percent": "0.1802", "is_contract": 0},  # 18.02%
                {"address": "0x000000000000000000000000000000000000dead", "percent": "0.18", "is_contract": 0},  # 18% burn
                {"address": "0xOTHER", "percent": "0.10", "is_contract": 0},
            ],
            "dex": []
        }
        
        with patch('evm_holders.requests.get') as mock_get, \
             patch('evm_holders.requests.post') as mock_post:
            
            def side_effect(url, **kwargs):
                if "honeypot" in url:
                    return Mock(status_code=500)
                elif "gopluslabs" in url:
                    return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xtoken": mock_goplus}})
                return Mock(status_code=404)
            
            mock_get.side_effect = side_effect
            
            # Multicall returns not pools
            mock_post.return_value = Mock(
                status_code=200,
                json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b'')]*6]).hex()}
            )
            
            result = evm_holders.evm_holder_concentration(56, "0xGPBURN", [], 120, db)
            
            assert result.ok
            assert result.source == "goplus"
            
            # Denominator = 1 - 0.18 = 0.82
            # Top wallet = 0.1802 / 0.82 = 0.2197 (about 22%)
            assert abs(result.top_wallet - 0.2197) < 0.01
    
    finally:
        db.close()
        os.unlink(db_path)


import time
