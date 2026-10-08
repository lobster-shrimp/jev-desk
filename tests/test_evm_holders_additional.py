"""
Additional tests for review 2 requirements - FIXED VERSION
"""
import pytest
import sys
sys.path.insert(0, '/workspace')

from unittest.mock import Mock, patch
import evm_holders
from eth_abi import encode, decode
import time


def test_multicall3_golden_encode_decode(isolate_evm_state):
    """Test Multicall3 real call with proper encoding/decoding."""
    import evm_holders
    from eth_abi import encode, decode
    from unittest.mock import Mock, patch
    
    # Real B13B address and V3 pool from spec
    b13b_token = "0x4902c5ebc598265ed2212b559b042de8a5eeec3f"
    v3_pool = "0xcf936261a1582b45eae2246105b3388d1e31c94d"
    other_addr = "0x320e9d32c1eea81a30ee00c48b02d74fd79e92d9"
    
    holders = [
        (v3_pool, 1000, True),
        (other_addr, 500, True),
    ]
    
    # Expected Multicall3 data
    expected_calls = [
        (v3_pool, True, bytes.fromhex("0dfe1681")),  # token0()
        (v3_pool, True, bytes.fromhex("d21220a7")),  # token1()
        (other_addr, True, bytes.fromhex("0dfe1681")),
        (other_addr, True, bytes.fromhex("d21220a7")),
    ]
    expected_data = "0x82ad56cb" + encode(['(address,bool,bytes)[]'], [expected_calls]).hex()
    
    # Mock RPC response: pool returns token addresses, other returns nothing
    b13b_padded = b'\x00' * 12 + bytes.fromhex(b13b_token[2:])
    other_padded = b'\x00' * 12 + bytes.fromhex(other_addr[2:])
    mock_response = encode(['(bool,bytes)[]'], [[
        (True, b13b_padded),  # token0()
        (True, other_padded),  # token1()
        (True, b''),  # other token0()
        (True, b''),  # other token1()
    ]])
    
    with patch('evm_holders.requests.post') as mock_post:
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"result": "0x" + mock_response.hex()}
        )
        
        result = evm_holders._check_pools_via_multicall(holders, b13b_token, 56, time.time() + 10)
        
        # Verify the POST data matches expected encoding
        assert mock_post.called
        call_json = mock_post.call_args[1]['json']
        assert call_json['method'] == 'eth_call'
        actual_data = call_json['params'][0]['data']
        assert actual_data == expected_data, f"Expected {expected_data}, got {actual_data}"
        
        # Verify result identifies the pool
        assert v3_pool in result or v3_pool.lower() in result
        assert other_addr not in result


def test_fold_mint_transfer_burn_sequence(isolate_evm_state):
    """Test RPC fold processes mint, transfer, and burn correctly."""
    import book
    db = book.DB
    
    # Use proper 20-byte addresses padded to 32 bytes for topics
    ALICE = "0x" + "0" * 24 + "a1" + "1ce" + "0" * 15  # 0x00...a11ce00...
    BOB = "0x" + "0" * 24 + "b0b" + "0" * 17  # 0x00...b0b00...
    ZERO = "0x" + "0" * 64
    
    with patch('evm_holders.requests.post') as mock_post:
        mint_logs = [{
            "blockNumber": hex(500),
            "topics": [
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                ZERO,
                ALICE
            ],
            "data": hex(1000000)
        }]
        
        full_logs = [
            {"blockNumber": hex(500), "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", ZERO, ALICE], "data": hex(1000000)},
            {"blockNumber": hex(600), "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", ALICE, BOB], "data": hex(300000)},
            {"blockNumber": hex(700), "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", BOB, ZERO], "data": hex(100000)}
        ]
        
        def json_rpc(url, json=None, timeout=None):
            method = json.get("method")
            if method == "eth_blockNumber":
                return Mock(status_code=200, json=lambda: {"result": hex(1000)})
            elif method == "eth_getLogs":
                p = json.get("params")[0]
                fb = int(p["fromBlock"], 16)
                tb = int(p["toBlock"], 16)
                topics = p.get("topics", [])
                if len(topics) > 1 and topics[1] == ZERO:
                    return Mock(status_code=200, json=lambda: {"result": mint_logs if fb <= 500 <= tb else []})
                elif fb == 500:
                    return Mock(status_code=200, json=lambda: {"result": full_logs})
                else:
                    return Mock(status_code=200, json=lambda: {"result": []})
            elif method == "eth_call":
                return Mock(status_code=200, json=lambda: {"result": hex(900000)})
            return Mock(status_code=404)
        
        mock_post.side_effect = json_rpc
        
        call_count = [0]
        data, error = evm_holders._holders_rpc_fold("0xFOLD01", time.time() + 100, db, call_count)
        
        assert data is not None, f"Fold failed: {error}"
        assert data["complete"] == True
        assert data["supply"] == 900000
        
        # Addresses are normalized to lowercase and last 40 chars
        alice_key = "0x" + ALICE[-40:].lower()
        bob_key = "0x" + BOB[-40:].lower()
        
        assert alice_key in data["balances"], f"Alice not in {list(data['balances'].keys())}"
        assert bob_key in data["balances"], f"Bob not in {list(data['balances'].keys())}"
        assert data["balances"][alice_key] == 700000
        assert data["balances"][bob_key] == 200000


def test_fold_1e6_tolerance(isolate_evm_state):
    """Test fold uses 1e-6 tolerance."""
    import book
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, json=None, timeout=None):
            method = json.get("method")
            if method == "eth_blockNumber":
                return Mock(status_code=200, json=lambda: {"result": hex(1000)})
            elif method == "eth_getLogs":
                return Mock(status_code=200, json=lambda: {"result": [{"blockNumber": hex(500), "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "0x" + "0" * 64, "0x000000000000000000000000000000000000616c696365"], "data": hex(1000000)}]})
            elif method == "eth_call":
                return Mock(status_code=200, json=lambda: {"result": hex(1000001)})
            return Mock(status_code=404)
        mock_post.side_effect = rpc
        call_count = [0]
        data, error = evm_holders._holders_rpc_fold("0xFOLD02", time.time() + 100, book.DB, call_count)
        assert data["complete"] == True


def test_fold_halving_on_limit(isolate_evm_state):
    """Test fold halves window on limit error."""
    import book
    halved = [0]
    
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, json=None, timeout=None):
            method = json.get("method")
            
            if method == "eth_blockNumber":
                # Set head very high so initial window is large
                return Mock(status_code=200, json=lambda: {"result": hex(10_000_000)})
            
            elif method == "eth_getLogs":
                p = json.get("params")[0]
                topics = p.get("topics", [])
                fb = int(p["fromBlock"], 16)
                tb = int(p["toBlock"], 16)
                window = tb - fb + 1
                
                # Mint search - return mint at block 1
                if len(topics) > 1 and topics[1] == "0x" + "0" * 64:
                    return Mock(status_code=200, json=lambda: {"result": [{
                        "blockNumber": hex(1),
                        "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "0x" + "0" * 64, "0x" + "a" * 64],
                        "data": hex(1000)
                    }]})
                
                # Full scan with default 250k window triggers limit error first time
                if window >= 250_000 and halved[0] == 0:
                    halved[0] = 1
                    return Mock(status_code=200, json=lambda: {"error": {"message": "query exceeds limit of 10000 results"}})
                
                # After halving, return empty
                return Mock(status_code=200, json=lambda: {"result": []})
            
            elif method == "eth_call":
                return Mock(status_code=200, json=lambda: {"result": hex(1000)})
            
            return Mock(status_code=404)
        
        mock_post.side_effect = rpc
        call_count = [0]
        data, error = evm_holders._holders_rpc_fold("0xFOLD03", time.time() + 100, book.DB, call_count)
        
        assert halved[0] == 1, "Window should have been halved due to limit error"


def test_fold_incremental_from_cache(isolate_evm_state):
    """Test fold resumes from cache."""
    import book
    import json
    db = book.DB
    
    # Insert cached state at block 500
    cached_balances = {"0x" + "a"*40: 1000000}
    db.execute("INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)", 
               (4663, "0xfold04", 500, json.dumps(cached_balances), "1000000", time.time()))
    db.commit()
    
    resumed = [False]
    
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, **kwargs):
            json_data = kwargs.get('json', {})
            method = json_data.get("method")
            
            if method == "eth_blockNumber":
                # Head is at block 300k+ (beyond 250k threshold), so fold needs to continue
                return Mock(status_code=200, json=lambda: {"result": hex(300_000)})
            
            elif method == "eth_getLogs":
                p = json_data.get("params", [{}])[0]
                fb = int(p.get("fromBlock", "0x0"), 16)
                
                # Check if we're resuming from block 501
                if fb == 501:
                    resumed[0] = True
                
                return Mock(status_code=200, json=lambda: {"result": []})
            
            elif method == "eth_call":
                # Return current supply
                return Mock(status_code=200, json=lambda: {"result": hex(1000000)})
            
            return Mock(status_code=404)
        
        mock_post.side_effect = rpc
        call_count = [0]
        data, error = evm_holders._holders_rpc_fold("0xfold04", time.time() + 100, db, call_count)
        
        assert resumed[0] == True, "Fold should resume from block 501"
        assert data is not None
        assert data["complete"] == True
        assert data["balances"] == cached_balances


def test_supply_zero_definitive(isolate_evm_state):
    """Test supply=0 is definitive."""
    import book
    with patch('evm_holders.requests.get') as mock_get:
        mock_get.return_value = Mock(status_code=200, json=lambda: {"totalSupply": 0, "holders": []})
        result = evm_holders.evm_holder_concentration(56, "0xcccccccccccccccccccccccccccccccccccccccc", [], 30, book.DB)
        assert not result.ok
        assert result.error == "zero_supply"
        assert result.is_transient == False


def test_eip7702_wallet_counted(isolate_evm_state):
    """Test EIP-7702 delegated EOAs counted."""
    import book
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        mock_get.return_value = Mock(status_code=200, json=lambda: {"totalSupply": 1000000, "holders": [{"address": "0xdddddddddddddddddddddddddddddddddddddddd", "balance": 500000, "isContract": True}, {"address": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "balance": 500000, "isContract": False}]})
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b''), (False, b'')]]).hex()})
        result = evm_holders.evm_holder_concentration(56, "0xcccccccccccccccccccccccccccccccccccccccc", [], 30, book.DB)
        assert result.ok
        assert result.top_wallet == 0.5


def test_goplus_units_via_dossier_chain_kill(isolate_evm_state):
    """End-to-end GoPlus units test via collect.dossier and chain_kill."""
    import book
    import collect
    from filter import chain_kill
    from unittest.mock import Mock, patch
    
    # Test case 1: top_wallet 0.20 (20%) should kill on top_wallet
    W1 = "0x1111111111111111111111111111111111111111"
    W2 = "0x2222222222222222222222222222222222222222"
    
    mock_goplus_whale = {
        "holders": [
            {"address": W1, "percent": "0.20", "is_contract": 0},  # 20%
            {"address": W2, "percent": "0.10", "is_contract": 0},  # 10%
        ],
        "dex": []
    }
    
    # Normalized token for dossier
    normalized_whale = {
        "ticker": "WHALE",
        "addr": "0xabcd1111111111111111111111111111111111ab",
        "net": 56,
        "age_minutes": 150,
        "liquidity_usd": 50000,
        "volume_h24": 100000,
        "mcap_usd": 200000,
        "holder_count": 200,
        "trades_h24": 500,
    }
    
    with patch('collect.requests.get') as mock_get, patch('collect.requests.post') as mock_post:
        def get_side_effect(url, **kwargs):
            if "geckoterminal" in url:
                return Mock(status_code=200, json=lambda: {"data": {"attributes": {}}})
            elif "honeypot" in url:
                return Mock(status_code=500)  # Force GoPlus fallback
            elif "gopluslabs" in url:
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {normalized_whale["addr"]: mock_goplus_whale}})
            return Mock(status_code=404)
        
        mock_get.side_effect = get_side_effect
        
        # Multicall (none are pools)
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b'')] * 4])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        d = collect.dossier(normalized_whale, limiter=None)
        
        assert d["top_wallet_percent"] is not None
        assert abs(d["top_wallet_percent"] - 0.20) < 0.01
        
        kill_reason = chain_kill(d)
        assert kill_reason == "top_wallet", f"Expected top_wallet kill, got {kill_reason}"
    
    # Test case 2: top_wallet 0.04 (4%) and top_10 < 60% should NOT kill
    mock_goplus_pass = {
        "holders": [
            {"address": W1, "percent": "0.04", "is_contract": 0},  # 4%
            {"address": W2, "percent": "0.03", "is_contract": 0},  # 3%
        ],
        "dex": []
    }
    
    normalized_pass = {
        "ticker": "PASS",
        "addr": "0xabcd2222222222222222222222222222222222ab",
        "net": 56,
        "age_minutes": 150,
        "liquidity_usd": 50000,
        "volume_h24": 100000,
        "mcap_usd": 200000,
        "holder_count": 200,
        "trades_h24": 500,
    }
    
    with patch('collect.requests.get') as mock_get, patch('collect.requests.post') as mock_post:
        def get_side_effect(url, **kwargs):
            if "geckoterminal" in url:
                return Mock(status_code=200, json=lambda: {"data": {"attributes": {}}})
            elif "honeypot" in url:
                return Mock(status_code=500)
            elif "gopluslabs" in url:
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {normalized_pass["addr"]: mock_goplus_pass}})
            return Mock(status_code=404)
        
        mock_get.side_effect = get_side_effect
        
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b'')] * 4])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        d = collect.dossier(normalized_pass, limiter=None)
        
        assert d["top_wallet_percent"] is not None
        assert abs(d["top_wallet_percent"] - 0.04) < 0.01
        
        kill_reason = chain_kill(d)
        assert kill_reason is None, f"Expected no kill, got {kill_reason}"


def test_429_non_blocking(isolate_evm_state):
    """Test 429 returns immediately."""
    import book
    with patch('evm_holders.requests.get') as mock_get:
        mock_get.return_value = Mock(status_code=429, headers={"Retry-After": "60"})
        start = time.time()
        result = evm_holders.evm_holder_concentration(56, "0xRATELIM", [], 30, book.DB)
        elapsed = time.time() - start
        assert not result.ok
        assert result.error == "rate_limited"
        assert result.is_transient == True
        assert elapsed < 5


def test_deadline_enforcement(isolate_evm_state):
    """Test deadline is enforced."""
    import book
    import requests
    
    with patch('evm_holders.requests.get') as mock_get:
        def slow(url, **kwargs):
            # Simulate timeout exception instead of actually sleeping
            raise requests.Timeout("Request timed out")
        
        mock_get.side_effect = slow
        start = time.time()
        result = evm_holders.evm_holder_concentration(56, "0xDEADLINE", [], 20, book.DB)
        elapsed = time.time() - start
        
        # Should return quickly with a timeout/transient error
        assert elapsed < 5
        assert not result.ok
        assert result.is_transient == True


def test_incomplete_at_head_definitive(isolate_evm_state):
    """Test incomplete_at_head is definitive."""
    import book
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, json=None, timeout=None):
            method = json.get("method")
            if method == "eth_blockNumber":
                return Mock(status_code=200, json=lambda: {"result": hex(1000)})
            elif method == "eth_getLogs":
                return Mock(status_code=200, json=lambda: {"result": [{"blockNumber": hex(500), "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", "0x" + "0" * 64, "0x000000000000000000000000000000000000616c696365"], "data": hex(1000000)}]})
            elif method == "eth_call":
                return Mock(status_code=200, json=lambda: {"result": hex(5000000)})
            return Mock(status_code=404)
        mock_post.side_effect = rpc
        call_count = [0]
        data, error = evm_holders._holders_rpc_fold("0xINCOMPL", time.time() + 100, book.DB, call_count)
        assert data is None
        assert error == "incomplete_at_head"


def test_connection_error_transient(isolate_evm_state):
    """Test connection errors are transient."""
    import book
    with patch('evm_holders.requests.get') as mock_get:
        import requests
        mock_get.side_effect = requests.ConnectionError("Network unreachable")
        result = evm_holders.evm_holder_concentration(56, "0xCONNERR", [], 30, book.DB)
        assert not result.ok
        assert result.is_transient == True


def test_http_5xx_transient(isolate_evm_state):
    """Test 5xx errors are transient."""
    import book
    with patch('evm_holders.requests.get') as mock_get:
        mock_get.return_value = Mock(status_code=503)
        result = evm_holders.evm_holder_concentration(56, "0xHTTP5XX", [], 30, book.DB)
        assert not result.ok
        assert result.error == "http_503"
        assert result.is_transient == True


def test_goplus_burn_denominator(isolate_evm_state):
    """Test GoPlus burn denominator."""
    import book
    mock_goplus = {"holders": [{"address": "0x2222222222222222222222222222222222222222", "percent": "0.1802", "is_contract": 0}, {"address": "0x000000000000000000000000000000000000dead", "percent": "0.18", "is_contract": 0}, {"address": "0xOTHER", "percent": "0.10", "is_contract": 0}], "dex": []}
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def se(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=500)
            elif "gopluslabs" in url:
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xgpburn": mock_goplus}})
            return Mock(status_code=404)
        mock_get.side_effect = se
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b'')]*6]).hex()})
        result = evm_holders.evm_holder_concentration(56, "0xGPBURN", [], 120, book.DB)
        assert result.ok
        # Denominator = 1 - 0.18 = 0.82, top_wallet = 0.1802 / 0.82 ≈ 0.2197
        assert abs(result.top_wallet - 0.2197) < 0.01


def test_address_constants(isolate_evm_state):
    """Test all address constants are valid 42-char lowercase hex."""
    import evm_holders
    
    # Check all burn addresses
    for addr in evm_holders.BURN_ADDRESSES:
        assert len(addr) == 42, f"Burn address {addr} is not 42 chars"
        assert addr.startswith("0x"), f"Burn address {addr} doesn't start with 0x"
        assert addr == addr.lower(), f"Burn address {addr} is not lowercase"
        assert all(c in "0123456789abcdef" for c in addr[2:]), f"Burn address {addr} has invalid hex"
    
    # Check specific burn addresses match spec
    assert "0x0000000000000000000000000000000000000000" in evm_holders.BURN_ADDRESSES
    assert "0x000000000000000000000000000000000000dead" in evm_holders.BURN_ADDRESSES
    assert "0xdead000000000000000042069420694206942069" in evm_holders.BURN_ADDRESSES
    
    # Check pool contracts
    for addr in evm_holders.POOL_CONTRACTS:
        assert len(addr) == 42, f"Pool contract {addr} is not 42 chars"
        assert addr == addr.lower(), f"Pool contract {addr} is not lowercase"
    
    # Check launchpad curves
    for addr in evm_holders.LAUNCHPAD_CURVES:
        assert len(addr) == 42, f"Launchpad curve {addr} is not 42 chars"
        assert addr == addr.lower(), f"Launchpad curve {addr} is not lowercase"
    
    # Check permanent lockers
    for addr in evm_holders.PERMANENT_LOCKERS:
        assert len(addr) == 42, f"Permanent locker {addr} is not 42 chars"
        assert addr == addr.lower(), f"Permanent locker {addr} is not lowercase"
    
    # Check conditional lockers
    for addr in evm_holders.CONDITIONAL_LOCKERS:
        assert len(addr) == 42, f"Conditional locker {addr} is not 42 chars"
        assert addr == addr.lower(), f"Conditional locker {addr} is not lowercase"
    
    # Check Multicall3
    assert len(evm_holders.MULTICALL3) == 42
    assert evm_holders.MULTICALL3 == evm_holders.MULTICALL3.lower()
    assert evm_holders.MULTICALL3 == "0xca11bde05977b3631167028862be2a173976ca11"


def test_goplus_lock_gating_no_unidentified(isolate_evm_state):
    """Test GoPlus NOT called when pair + burn >=3% with no unknown contract."""
    import book
    import evm_holders
    from unittest.mock import Mock, patch
    
    pair_addr = "0x1111111111111111111111111111111111111111"
    burn_addr = "0x000000000000000000000000000000000000dead"
    whale_addr = "0x2222222222222222222222222222222222222222"
    token_addr = "0xcccccccccccccccccccccccccccccccccccccccc"
    
    mock_honeypot = {
        "totalSupply": 1000000,
        "holders": [
            {"address": pair_addr, "balance": 200000, "isContract": True},  # 20%
            {"address": burn_addr, "balance": 300000, "isContract": False},  # 30% burn
            {"address": whale_addr, "balance": 500000, "isContract": False},  # 50%
        ]
    }
    
    goplus_called = [0]
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def get_side_effect(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=200, json=lambda: mock_honeypot)
            elif "gopluslabs" in url:
                goplus_called[0] += 1
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {}})
            return Mock(status_code=404)
        
        mock_get.side_effect = get_side_effect
        
        # Multicall identifies the pair with proper abi-encoded addresses
        from eth_abi import encode
        token_padded = b'\x00' * 12 + bytes.fromhex(token_addr[2:])
        other_padded = b'\x00' * 12 + bytes.fromhex("3333333333333333333333333333333333333333")
        multicall_result = encode(['(bool,bytes)[]'], [[
            (True, token_padded),  # token0()
            (True, other_padded),  # token1()
        ]])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, token_addr, [], 120, book.DB)
        
        # GoPlus should NOT have been called
        assert goplus_called[0] == 0, f"GoPlus called {goplus_called[0]} times, expected 0"
        
        # Pair should be excluded
        assert result.ok
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert pair_addr in exclusions
        assert exclusions[pair_addr] == "pair_multicall"


def test_goplus_lock_gating_with_unidentified(isolate_evm_state):
    """Positive control: GoPlus IS called when unidentified contract holds 5%."""
    import book
    import evm_holders
    from unittest.mock import Mock, patch
    
    unknown_contract = "0x1111111111111111111111111111111111111111"
    whale_addr = "0x2222222222222222222222222222222222222222"
    token_addr = "0xcccccccccccccccccccccccccccccccccccccccc"
    
    mock_honeypot = {
        "totalSupply": 1000000,
        "holders": [
            {"address": unknown_contract, "balance": 50000, "isContract": True},  # 5%
            {"address": whale_addr, "balance": 950000, "isContract": False},
        ]
    }
    
    goplus_called = [0]
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def get_side_effect(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=200, json=lambda: mock_honeypot)
            elif "gopluslabs" in url:
                goplus_called[0] += 1
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {token_addr: {"holders": [], "dex": []}}})
            return Mock(status_code=404)
        
        mock_get.side_effect = get_side_effect
        
        # Multicall says unknown contract is not a pool
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b''), (False, b'')]])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, token_addr, [], 120, book.DB)
        
        # GoPlus SHOULD have been called exactly once
        assert goplus_called[0] == 1, f"GoPlus called {goplus_called[0]} times, expected 1"


def test_goplus_lock_gating_conditional_locker(isolate_evm_state):
    """Conditional locker: PinkLock >= 3% -> GoPlus called."""
    import book
    import evm_holders
    from unittest.mock import Mock, patch
    from datetime import datetime, timezone, timedelta
    
    pinklock_addr = "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe"
    whale_addr = "0x2222222222222222222222222222222222222222"
    token_addr = "0xcccccccccccccccccccccccccccccccccccccccc"
    
    future_time = datetime.now(timezone.utc) + timedelta(days=30)
    iso_time = future_time.isoformat()
    
    mock_goplus = {
        "holders": [
            {
                "address": pinklock_addr,
                "is_locked": 1,
                "locked_detail": [{"end_time": iso_time}]
            }
        ],
        "dex": []
    }
    
    mock_honeypot = {
        "totalSupply": 1000000,
        "holders": [
            {"address": pinklock_addr, "balance": 400000, "isContract": True},
            {"address": whale_addr, "balance": 600000, "isContract": False},
        ]
    }
    
    goplus_called = [0]
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def get_side_effect(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=200, json=lambda: mock_honeypot)
            elif "gopluslabs" in url:
                goplus_called[0] += 1
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {token_addr: mock_goplus}})
            return Mock(status_code=404)
        
        mock_get.side_effect = get_side_effect
        
        # Multicall says PinkLock is not a pool
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b''), (False, b''), (False, b''), (False, b'')]])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, token_addr, [], 120, book.DB)
        
        # GoPlus SHOULD have been called
        assert goplus_called[0] == 1, f"GoPlus called {goplus_called[0]} times, expected 1"


def test_cache_at_head_mismatch_definitive(isolate_evm_state):
    """Test cache-at-head with totalSupply mismatch returns definitive error."""
    import book
    import evm_holders
    import json
    import time
    from unittest.mock import Mock, patch
    
    db = book.DB
    
    # Insert cache with sum=1000
    db.execute("INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)",
               (4663, "0xcached", 500, json.dumps({"0xholder": 1000}), "1000", time.time()))
    db.commit()
    
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, **kwargs):
            json_data = kwargs.get('json', {})
            method = json_data.get("method")
            
            if method == "eth_blockNumber":
                # Head is at block 500, so cache is at head
                return Mock(status_code=200, json=lambda: {"result": hex(480)})
            
            elif method == "eth_call":
                # totalSupply = 5000 (mismatch with cache sum of 1000)
                return Mock(status_code=200, json=lambda: {"result": hex(5000)})
            
            return Mock(status_code=404)
        
        mock_post.side_effect = rpc
        
        result = evm_holders.evm_holder_concentration(4663, "0xcached", [], 30, db)
        
        assert not result.ok
        assert result.error == "incomplete_at_head"
        assert result.is_transient == False  # Definitive (6h bench)


def test_prune_old_cache(isolate_evm_state):
    """Test prune_old_cache deletes rows older than 72h."""
    import book
    import evm_holders
    import json
    import time
    
    db = book.DB
    now = time.time()
    
    # Insert old cache (4 days old)
    db.execute("INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)",
               (4663, "0xold", 100, json.dumps({}), "0", now - 4*24*3600))
    
    # Insert new cache (1 day old)
    db.execute("INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)",
               (4663, "0xnew", 200, json.dumps({}), "0", now - 1*24*3600))
    db.commit()
    
    # Prune
    evm_holders.prune_old_cache(db)
    
    # Check: old should be gone, new should remain
    rows = db.execute("SELECT token FROM evm_holder_cache").fetchall()
    tokens = [r[0] for r in rows]
    
    assert "0xold" not in tokens
    assert "0xnew" in tokens


def test_self_held_balance_counted(isolate_evm_state):
    """Test token contract holding its own tokens is counted (not excluded)."""
    import book
    import evm_holders
    from unittest.mock import Mock, patch
    
    token_addr = "0x1111111111111111111111111111111111111111"
    whale_addr = "0x2222222222222222222222222222222222222222"
    
    mock_honeypot = {
        "totalSupply": 1000000,
        "holders": [
            {"address": token_addr, "balance": 300000, "isContract": True},  # Token holds itself
            {"address": whale_addr, "balance": 700000, "isContract": False},
        ]
    }
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        mock_get.return_value = Mock(status_code=200, json=lambda: mock_honeypot)
        
        # Multicall says token contract is not a pool
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b''), (False, b'')]])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, token_addr, [], 30, book.DB)
        
        assert result.ok
        # Top wallet should be whale at 70%, not token self-holding
        assert abs(result.top_wallet - 0.70) < 0.01


def test_cache_at_head_totalsupply_unavailable(isolate_evm_state):
    """Test cache-at-head with totalSupply 429/error returns transient error."""
    import book
    import evm_holders
    from filter import chain_kill
    import json
    import time
    from unittest.mock import Mock, patch
    
    db = book.DB
    
    # Insert cache at head with sum=1000
    db.execute("INSERT INTO evm_holder_cache VALUES (?, ?, ?, ?, ?, ?)",
               (4663, "0xcached429", 500, json.dumps({"0xaaaa": 600, "0xbbbb": 400}), "1000", time.time()))
    db.commit()
    
    with patch('evm_holders.requests.post') as mock_post:
        def rpc(url, **kwargs):
            json_data = kwargs.get('json', {})
            method = json_data.get("method")
            
            if method == "eth_blockNumber":
                # Head is at block 480, so cache (block 500) is at head
                return Mock(status_code=200, json=lambda: {"result": hex(480)})
            
            elif method == "eth_call":
                # totalSupply returns 429 (rate limited)
                return Mock(status_code=429, json=lambda: {"error": "rate limited"})
            
            return Mock(status_code=404)
        
        mock_post.side_effect = rpc
        
        result = evm_holders.evm_holder_concentration(4663, "0xcached429", [], 30, db)
        
        # Should be transient error, not ok
        assert not result.ok
        assert result.error == "totalSupply_unavailable"
        assert result.is_transient == True
        
        # chain_kill should return holders_pending
        dossier = {
            "top_wallet_percent": result.top_wallet,
            "top_10_percent": result.top_10,
            "evm_holder_source": result.source,
            "evm_holder_error": result.error,
            "evm_holder_is_transient": result.is_transient,
        }
        kill_reason = chain_kill(dossier)
        assert kill_reason == "holders_pending"


def test_denominator_excludes_burns_only_not_lockers(isolate_evm_state):
    """Test denominator = supply - burns only (lockers NOT subtracted).
    
    Guards against re-adding permanent lock subtraction.
    RobinFunFi locker 200/1000 + whale 45 -> tw=0.045, no kill, locker excluded.
    """
    import book
    import evm_holders
    from filter import chain_kill
    from unittest.mock import Mock, patch
    from datetime import datetime, timezone, timedelta
    
    robinfunfi_locker = "0x267444d07c9c8c3ccf4ee661cc35e430c8257a73"
    whale_addr = "0x1234567890123456789012345678901234567890"
    
    # Mock GoPlus response with lock >7 days away for RobinFunFi
    future_time = datetime.now(timezone.utc) + timedelta(days=365)
    iso_time = future_time.isoformat()
    
    mock_goplus = {
        "holders": [
            {
                "address": robinfunfi_locker,
                "is_locked": 1,
                "locked_detail": [{"end_time": iso_time}]
            }
        ],
        "dex": []
    }
    
    # Total: 1000
    # RobinFunFi locker: 200 (20% raw) - excluded from holder list
    # Whale: 45 (4.5% raw)
    # Other: 755
    # Denominator: 1000 (no burns)
    # After excluding locker: tw = 45/1000 = 0.045 (4.5%)
    EXPECTED_TOP_WALLET = 45 / 1000  # 0.045
    
    mock_honeypot = {
        "totalSupply": 1000,
        "holders": [
            {"address": robinfunfi_locker, "balance": 200, "isContract": True},
            {"address": whale_addr, "balance": 45, "isContract": False},
            {"address": "0x2222222222222222222222222222222222222222", "balance": 755, "isContract": False},
        ]
    }
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def side_effect(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=200, json=lambda: mock_honeypot)
            elif "gopluslabs" in url:
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xtoken": mock_goplus}})
            return Mock(status_code=404)
        
        mock_get.side_effect = side_effect
        
        # Multicall response (both not pools)
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b''), (False, b''), (False, b''), (False, b''), (False, b''), (False, b'')]])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, "0xtoken", [], 120, book.DB)
        
        assert result.ok, f"Result failed: {result.error}"
        assert result.top_wallet is not None
        
        # Exact expected value
        assert abs(result.top_wallet - EXPECTED_TOP_WALLET) < 0.0001, \
            f"Expected {EXPECTED_TOP_WALLET:.6f}, got {result.top_wallet:.6f}"
        
        # Locker should be excluded
        exclusions = {addr.lower(): reason for addr, _, reason in result.excluded}
        assert robinfunfi_locker in exclusions
        assert "locker" in exclusions[robinfunfi_locker]
        
        # Should pass chain_kill (not killed)
        dossier = {
            "top_wallet_percent": result.top_wallet,
            "top_10_percent": result.top_10,
            "evm_holder_source": result.source,
        }
        kill_reason = chain_kill(dossier)
        assert kill_reason is None, f"Expected pass, got kill: {kill_reason}"
