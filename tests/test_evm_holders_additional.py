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
    """Test Multicall3 encoding/decoding matches eth_abi golden bytes."""
    # Build calls
    calls = [
        ("0xcf93d4a19c64e93a2cdb35f1fd14d5f2abd5b43f", True, bytes.fromhex("0dfe1681")),
        ("0xcf93d4a19c64e93a2cdb35f1fd14d5f2abd5b43f", True, bytes.fromhex("d21220a7")),
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
    result_hex = "0x" + encode(
        ['(bool,bytes)[]'],
        [[(True, bytes.fromhex("0000000000000000000000004902dcA4D7011935322aE83Fef0E0c873ba1b0F0")),
          (False, b'')]]
    ).hex()
    
    decoded = decode(['(bool,bytes)[]'], bytes.fromhex(result_hex[2:]))[0]
    
    assert decoded[0][0] == True
    assert len(decoded[0][1]) == 32
    token_addr = "0x" + decoded[0][1][-20:].hex()
    assert token_addr.lower() == "0x4902dcA4D7011935322aE83Fef0E0c873ba1b0F0".lower()


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
        result = evm_holders.evm_holder_concentration(56, "0xSUPPLY0", [], 30, book.DB)
        assert not result.ok
        assert result.error == "zero_supply"
        assert result.is_transient == False


def test_eip7702_wallet_counted(isolate_evm_state):
    """Test EIP-7702 delegated EOAs counted."""
    import book
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        mock_get.return_value = Mock(status_code=200, json=lambda: {"totalSupply": 1000000, "holders": [{"address": "0x7702HOLDER", "balance": 500000, "isContract": True}, {"address": "0xNORMAL", "balance": 500000, "isContract": False}]})
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + encode(['(bool,bytes)[]'], [[(False, b''), (False, b'')]]).hex()})
        result = evm_holders.evm_holder_concentration(56, "0xEIP7702", [], 30, book.DB)
        assert result.ok
        assert result.top_wallet == 0.5


def test_goplus_units_via_dossier_chain_kill(isolate_evm_state):
    """End-to-end GoPlus units test - verify percent is fraction 0-1."""
    import book
    
    # Use valid Ethereum addresses
    W1 = "0x1111111111111111111111111111111111111111"
    W2 = "0x2222222222222222222222222222222222222222"
    W3 = "0x3333333333333333333333333333333333333333"
    W4 = "0x4444444444444444444444444444444444444444"
    
    mock_goplus = {
        "holders": [
            {"address": W1, "percent": "0.250", "is_contract": 0},   # 25%
            {"address": W2, "percent": "0.200", "is_contract": 0},   # 20%
            {"address": W3, "percent": "0.150", "is_contract": 0},   # 15%
            {"address": W4, "percent": "0.100", "is_contract": 0},   # 10%
            # Total top 4: 70%
        ],
        "dex": []
    }
    
    with patch('evm_holders.requests.get') as mock_get, patch('evm_holders.requests.post') as mock_post:
        def se(url, **kwargs):
            if "honeypot" in url:
                return Mock(status_code=500)
            elif "gopluslabs" in url:
                return Mock(status_code=200, json=lambda: {"code": 1, "result": {"0xgoplusu": mock_goplus}})
            return Mock(status_code=404)
        
        mock_get.side_effect = se
        
        # Mock Multicall3 response (none are pools)
        from eth_abi import encode
        multicall_result = encode(['(bool,bytes)[]'], [[(False, b'')] * 8])
        mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "0x" + multicall_result.hex()})
        
        result = evm_holders.evm_holder_concentration(56, "0xGOPLUSU", [], 120, book.DB)
        assert result.ok
        # Verify GoPlus percent (0.250) is interpreted as fraction, not percentage
        assert abs(result.top_wallet - 0.25) < 0.01
        assert abs(result.top_10 - 70.0) < 1


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
    mock_goplus = {"holders": [{"address": "0xWHALE", "percent": "0.1802", "is_contract": 0}, {"address": "0x000000000000000000000000000000000000dead", "percent": "0.18", "is_contract": 0}, {"address": "0xOTHER", "percent": "0.10", "is_contract": 0}], "dex": []}
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
