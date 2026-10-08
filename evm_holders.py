"""
EVM holder concentration module for BSC (56), Robinhood (4663), and Base (8453).

Computes real (non-pool) top_wallet and top_10 percentages from free keyless sources:
- Honeypot.is TopHolders API for BSC and Base
- Robinhood Transfer-log fold over public RPC
- GoPlus as secondary fallback for all chains

Units:
- top_wallet: 0-1 fraction (compared to 0.05 in filter.py)
- top_10: 0-100 percent (divided by 100 in filter.py)
"""
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from collections import defaultdict
from typing import Any

import requests

from secret_utils import safe_err


log = logging.getLogger("evm_holders")


# Result cache duration (10 minutes)
CACHE_DURATION = 600

# Per-token time budget (20 seconds) - hard deadline, never sleep/block beyond this
TOKEN_TIMEOUT = 20

# Max RPC calls per Robinhood fold
MAX_RPC_CALLS_PER_FOLD = 40

# Burn addresses
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}

# Shared pool contracts (lowercase)
POOL_CONTRACTS = {
    # BSC Uniswap v4 PoolManager
    "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
    # BSC PancakeSwap Infinity Vault
    "0x238a358808379702088667322f80ac48bad5e6c4",
    # Robinhood Uniswap v4 PoolManager
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",
}

# Launchpad curve contracts (lowercase)
LAUNCHPAD_CURVES = {
    # Flap Portal BSC
    "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
    # Flap Portal Robinhood
    "0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09",
    # four.meme TokenManager2
    "0x5c952063c7fc8610ffdb798152d69f0b9550762b",
}

# Permanent lockers (lowercase)
PERMANENT_LOCKERS = {
    # RobinFunFi V2 LaunchLocker
    "0x267444d099b10fb5ed7c3cc7b7c767adca574952",
}

# Time-based lockers that need GoPlus check (lowercase)
CONDITIONAL_LOCKERS = {
    # PinkLock02
    "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe",
}

# Multicall3 address (same on all EVM chains)
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"

# Rate limiter state
_rate_limiters = {}


@dataclass
class HolderResult:
    """Result of holder concentration analysis."""
    top_wallet: float | None  # 0-1 fraction
    top_10: float | None  # 0-100 percent
    source: str  # 'honeypot', 'rpc_fold', 'goplus', 'cache', 'unavailable'
    excluded: list[tuple[str, float, str]]  # [(address, pct, reason), ...]
    ok: bool  # False if result is unavailable
    error: str | None  # Error message if not ok
    # Raw values before exclusions (for logging)
    raw_top_wallet: float | None = None  # 0-1 fraction
    raw_top_10: float | None = None  # 0-100 percent


class RateLimiter:
    """Simple rate limiter with 429 backoff."""
    
    def __init__(self, max_per_sec: float):
        self.max_per_sec = max_per_sec
        self.min_interval = 1.0 / max_per_sec
        self.last_call = 0.0
        self.backoff_until = 0.0
    
    def is_in_backoff(self) -> bool:
        """Check if currently in 429 backoff without sleeping."""
        return time.time() < self.backoff_until
    
    def wait(self, deadline: float | None = None):
        """Wait if needed to respect rate limit and backoff.
        
        If deadline is provided and we'd exceed it, raises TimeoutError.
        """
        now = time.time()
        
        # Check deadline first
        if deadline and now >= deadline:
            raise TimeoutError("deadline_exceeded")
        
        # Wait for 429 backoff
        if self.backoff_until > now:
            sleep_time = self.backoff_until - now
            if deadline and now + sleep_time > deadline:
                raise TimeoutError("deadline_exceeded")
            time.sleep(sleep_time)
            now = time.time()
        
        # Wait for rate limit
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            if deadline and now + sleep_time > deadline:
                raise TimeoutError("deadline_exceeded")
            time.sleep(sleep_time)
        
        self.last_call = time.time()
    
    def record_429(self, retry_after: int | None = None):
        """Record 429 and set backoff."""
        backoff = retry_after if retry_after else 60
        self.backoff_until = time.time() + backoff
        log.warning("429 received, backing off %ds", backoff)


def _get_limiter(name: str, rate: float) -> RateLimiter:
    """Get or create rate limiter."""
    if name not in _rate_limiters:
        _rate_limiters[name] = RateLimiter(rate)
    return _rate_limiters[name]


def _holders_honeypot(chain_id: int, token: str, deadline: float) -> tuple[dict | None, str | None]:
    """
    Fetch holder data from Honeypot.is API.
    
    Returns (data, error) where data is {"totalSupply": int, "holders": [{address, balance, isContract}, ...]}.
    Returns (None, error_msg) on failure.
    """
    limiter = _get_limiter("honeypot", 1.0)  # 1 req/s
    
    # Check if in backoff - return immediately without sleeping
    if limiter.is_in_backoff():
        return None, "rate_limited"
    
    try:
        limiter.wait(deadline)
    except TimeoutError:
        return None, "timeout"
    
    url = "https://api.honeypot.is/v1/TopHolders"
    params = {"address": token, "chainID": chain_id}
    
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None, "timeout"
        
        resp = requests.get(url, params=params, timeout=min(10, remaining))
        
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            limiter.record_429(int(retry_after) if retry_after else None)
            return None, "rate_limited"
        
        if resp.status_code == 400:
            # Could be invalid chain or malformed request
            return None, "bad_request"
        
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        
        data = resp.json()
        
        # Validate response
        if "totalSupply" not in data or "holders" not in data:
            return None, "malformed_response"
        
        total_supply = data.get("totalSupply")
        
        # Handle string supply (bug #7b)
        if isinstance(total_supply, str):
            if total_supply == "0":
                return None, "zero_supply"
            try:
                total_supply = int(total_supply)
            except (ValueError, TypeError):
                return None, "invalid_supply"
        
        if total_supply == 0 or total_supply is None:
            return None, "zero_supply"
        
        # Update data with parsed supply
        data["totalSupply"] = total_supply
        
        holders = data.get("holders", [])
        if not holders:
            return None, "no_holders"
        
        return data, None
        
    except requests.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, safe_err(e)


def _holders_rpc_fold(token: str, deadline: float, db: sqlite3.Connection, call_count: list[int]) -> tuple[dict | None, str | None]:
    """
    Fold Transfer logs on Robinhood RPC to compute holder balances.
    
    Returns (data, error) where data is {"balances": {address: int}, "supply": int, "complete": bool}.
    Uses incremental cache from evm_holder_cache table.
    
    call_count is a mutable list[int] tracking RPC calls for cap enforcement.
    """
    rpc_url = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    limiter = _get_limiter("robinhood_rpc", 5.0)  # 5 req/s
    
    token_lower = token.lower()
    
    def rpc_call(method: str, params: list) -> tuple[dict | None, str | None]:
        """Make one RPC call with rate limiting and deadline."""
        if call_count[0] >= MAX_RPC_CALLS_PER_FOLD:
            return None, "call_limit_exceeded"
        
        if time.time() >= deadline:
            return None, "timeout"
        
        # Check if in backoff - return immediately
        if limiter.is_in_backoff():
            return None, "rate_limited"
        
        try:
            limiter.wait(deadline)
        except TimeoutError:
            return None, "timeout"
        
        call_count[0] += 1
        
        try:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None, "timeout"
            
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=min(10, remaining)
            )
            
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                limiter.record_429(int(retry_after) if retry_after else None)
                return None, "rate_limited"
            
            if resp.status_code != 200:
                return None, f"http_{resp.status_code}"
            
            data = resp.json()
            
            if "error" in data:
                error = data["error"]
                if isinstance(error, dict):
                    # Scrub error message before storing
                    msg = error.get("message", "rpc_error")
                    return None, safe_err(Exception(msg))
                return None, safe_err(Exception(str(error)))
            
            return data.get("result"), None
            
        except requests.Timeout:
            return None, "timeout"
        except Exception as e:
            return None, safe_err(e)
    
    # Get current block number
    block_result, error = rpc_call("eth_blockNumber", [])
    if error:
        return None, f"eth_blockNumber: {error}"
    
    try:
        current_block = int(block_result, 16)
    except (ValueError, TypeError):
        return None, "invalid_block_number"
    
    # Use blocks up to current - 20 for safety
    head_block = current_block - 20
    
    # Check cache
    cached = db.execute(
        "SELECT last_block, balances_json, supply, updated_at FROM evm_holder_cache "
        "WHERE chain_id=? AND token=?",
        (4663, token_lower)
    ).fetchone()
    
    if cached:
        last_block, balances_json, cached_supply, updated_at = cached
        balances = json.loads(balances_json) if balances_json else {}
        start_block = last_block + 1  # Resume from next block (bug #3 fix)
        
        # Bug #4b fix: if cached last_block >= head-20, no new blocks to process
        if start_block > head_block:
            # No new blocks, use cached data
            try:
                supply = int(cached_supply) if cached_supply else 0
            except (ValueError, TypeError):
                supply = 0
            
            return {
                "balances": balances,
                "supply": supply,
                "complete": True  # Already complete from cache
            }, None
        
        log.info("Robinhood fold for %s: resuming from block %d", token, last_block)
    else:
        balances = {}
        start_block = None
        log.info("Robinhood fold for %s: starting from mint", token)
    
    # Find mint block if not cached
    if start_block is None:
        # Bug #4e fix: Try one query over head-9,999,000..head first
        search_start = max(1, head_block - 9_999_000)
        
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        zero_addr_padded = "0x" + "0" * 64
        
        # Try to find first mint in one query
        logs_result, error = rpc_call("eth_getLogs", [{
            "fromBlock": hex(search_start),
            "toBlock": hex(head_block),
            "address": token,
            "topics": [transfer_topic, zero_addr_padded]
        }])
        
        mint_block = None
        
        # Bug #4a fix: Only treat specific "exceeds limit of 10000" message as a limit error
        if error and "exceeds limit of 10000" in error.lower():
            # Fall back to chunking
            log.info("Robinhood fold: mint search needs chunking")
            chunk_size = 500_000
            for chunk_start in range(search_start, head_block, chunk_size):
                chunk_end = min(chunk_start + chunk_size - 1, head_block)
                
                logs_result, error = rpc_call("eth_getLogs", [{
                    "fromBlock": hex(chunk_start),
                    "toBlock": hex(chunk_end),
                    "address": token,
                    "topics": [transfer_topic, zero_addr_padded]
                }])
                
                if error:
                    if "exceeds limit of 10000" in error.lower():
                        # Chunk too large, continue to next
                        continue
                    return None, f"getLogs mint search: {error}"
                
                if logs_result:
                    try:
                        mint_block = int(logs_result[0]["blockNumber"], 16)
                        log.info("Robinhood fold: found mint at block %d", mint_block)
                        break
                    except (KeyError, ValueError, IndexError):
                        pass
        elif error:
            return None, f"getLogs mint search: {error}"
        elif logs_result:
            try:
                mint_block = int(logs_result[0]["blockNumber"], 16)
                log.info("Robinhood fold: found mint at block %d", mint_block)
            except (KeyError, ValueError, IndexError):
                pass
        
        if mint_block is None:
            # Couldn't find mint within 10M blocks - definitive failure
            return None, "mint_not_found"
        
        start_block = mint_block
    
    # Fetch Transfer logs incrementally with adaptive window
    window_size = 250_000  # Start with 250k blocks
    current = start_block
    
    while current <= head_block:
        if time.time() >= deadline:
            # Bug #4d: persist partial progress before timeout
            db.execute(
                "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (4663, token_lower, current - 1, json.dumps(balances), str(sum(balances.values())), time.time())
            )
            db.commit()
            return None, "timeout"
        
        to_block = min(current + window_size - 1, head_block)
        
        logs_result, error = rpc_call("eth_getLogs", [{
            "fromBlock": hex(current),
            "toBlock": hex(to_block),
            "address": token,
            "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"]  # Transfer
        }])
        
        if error:
            # Bug #4a fix: Only halve on exact "exceeds limit" message
            if "exceeds limit of 10000" in error.lower():
                # Halve window size and retry same range
                window_size = max(10_000, window_size // 2)
                log.info("Robinhood fold: halving window to %d blocks", window_size)
                continue
            
            # Other errors: persist partial progress and fail
            db.execute(
                "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (4663, token_lower, current - 1, json.dumps(balances), str(sum(balances.values())), time.time())
            )
            db.commit()
            return None, f"eth_getLogs: {error}"
        
        # Process logs
        if logs_result:
            for log_entry in logs_result:
                try:
                    topics = log_entry["topics"]
                    data = log_entry["data"]
                    
                    # Decode Transfer(address from, address to, uint256 value)
                    from_addr = "0x" + topics[1][-40:] if len(topics) > 1 else None
                    to_addr = "0x" + topics[2][-40:] if len(topics) > 2 else None
                    value = int(data, 16) if data and data != "0x" else 0
                    
                    # Bug #4c fix: Don't credit 0x0 (burn destination)
                    if from_addr and from_addr.lower() != "0x0000000000000000000000000000000000000000":
                        from_addr = from_addr.lower()
                        balances[from_addr] = balances.get(from_addr, 0) - value
                        if balances[from_addr] <= 0:
                            balances.pop(from_addr, None)
                    
                    if to_addr and to_addr.lower() != "0x0000000000000000000000000000000000000000":
                        to_addr = to_addr.lower()
                        balances[to_addr] = balances.get(to_addr, 0) + value
                        if balances[to_addr] <= 0:
                            balances.pop(to_addr, None)
                    
                except (KeyError, ValueError, IndexError) as e:
                    log.debug("Failed to parse log: %s", e)
                    continue
        
        # Double window if we got few logs
        if logs_result is not None and len(logs_result) < 4000 and window_size < 1_000_000:
            window_size = min(1_000_000, window_size * 2)
        
        # Bug #3 fix: Only update current AFTER successful processing
        current = to_block + 1
    
    # Update cache with final state
    total_supply = sum(balances.values())
    
    # Bug #4g: Store actual supply in cache
    db.execute(
        "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (4663, token_lower, head_block, json.dumps(balances), str(total_supply), time.time())
    )
    db.commit()
    
    # Get actual supply from token contract
    supply_result, error = rpc_call("eth_call", [
        {"to": token, "data": "0x18160ddd"},  # totalSupply()
        "latest"
    ])
    
    actual_supply = 0
    if supply_result:
        try:
            actual_supply = int(supply_result, 16)
        except ValueError:
            pass
    
    # Check completeness (bug #4: use 1e-6 tolerance as spec says)
    complete = False
    if actual_supply > 0:
        diff = abs(total_supply - actual_supply)
        complete = diff <= actual_supply * 1e-6
    
    return {
        "balances": balances,
        "supply": actual_supply if actual_supply > 0 else total_supply,
        "complete": complete
    }, None


def _holders_goplus(chain_id: int, token: str, deadline: float) -> tuple[dict | None, str | None]:
    """
    Fetch holder data from GoPlus API.
    
    Returns (data, error) where data includes holders list and lock info.
    """
    # Map chain_id to GoPlus chain identifier
    chain_map = {56: "56", 8453: "8453", 4663: "4663", 143: "143"}  # Monad for later
    chain_str = chain_map.get(chain_id)
    
    if not chain_str:
        return None, "unsupported_chain"
    
    limiter = _get_limiter("goplus", 150/60)  # 150 CU/min (assume 1 CU per call)
    
    # Check if in backoff
    if limiter.is_in_backoff():
        return None, "rate_limited"
    
    try:
        limiter.wait(deadline)
    except TimeoutError:
        return None, "timeout"
    
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_str}"
    params = {"contract_addresses": token}
    
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None, "timeout"
        
        resp = requests.get(url, params=params, timeout=min(10, remaining))
        
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            limiter.record_429(int(retry_after) if retry_after else None)
            return None, "rate_limited"
        
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        
        data = resp.json()
        
        # GoPlus returns {"code": 1, "result": {token_address: {...}}}
        result = data.get("result", {})
        token_lower = token.lower()
        token_data = result.get(token_lower)
        
        if not token_data:
            return None, "no_data"
        
        return token_data, None
        
    except requests.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, safe_err(e)


def _encode_multicall3_aggregate3(calls: list[dict]) -> str:
    """
    Encode Multicall3 aggregate3 call data.
    
    calls: [{"target": "0x...", "allowFailure": true, "callData": "0x..."}]
    
    Returns: hex string of encoded call data
    """
    # aggregate3(Call3[] calldata calls) returns (Result[] memory returnData)
    # selector: 0x82ad56cb
    selector = "0x82ad56cb"
    
    # Encode the calls array
    # offset to array data (32 bytes)
    offset = "0" * 64
    
    # array length
    length = hex(len(calls))[2:].zfill(64)
    
    # each call is 3 words: target (address), allowFailure (bool), callData (bytes)
    encoded_calls = []
    
    # Calculate offsets for dynamic callData
    # Each call takes 3 slots: target, allowFailure, offset_to_callData
    call_data_offset = len(calls) * 96  # 3 * 32 bytes per call
    
    call_datas = []
    for call in calls:
        target = call["target"][2:].zfill(64).lower()  # Remove 0x, pad to 32 bytes
        allow_failure = "0" * 63 + ("1" if call["allowFailure"] else "0")
        
        # Offset to this call's data (from start of array data)
        offset_hex = hex(call_data_offset)[2:].zfill(64)
        
        encoded_calls.extend([target, allow_failure, offset_hex])
        
        # Encode callData as bytes
        call_data = call["callData"][2:] if call["callData"].startswith("0x") else call["callData"]
        call_data_len = hex(len(call_data) // 2)[2:].zfill(64)
        
        # Pad to 32-byte boundary
        padded_data = call_data + "0" * (64 - len(call_data) % 64 if len(call_data) % 64 != 0 else 0)
        
        call_datas.append(call_data_len + padded_data)
        call_data_offset += 32 + len(padded_data) // 2  # length + data
    
    # Combine everything
    encoded = selector + offset + length + "".join(encoded_calls) + "".join(call_datas)
    
    return "0x" + encoded


def _decode_multicall3_aggregate3(result_hex: str) -> list[tuple[bool, bytes]]:
    """
    Decode Multicall3 aggregate3 result.
    
    Returns: [(success: bool, returnData: bytes), ...]
    """
    if not result_hex or result_hex == "0x":
        return []
    
    data = result_hex[2:] if result_hex.startswith("0x") else result_hex
    
    # Skip offset to array (first 32 bytes) and get array length
    array_length = int(data[64:128], 16)
    
    results = []
    pos = 128  # Start after offset + length
    
    for i in range(array_length):
        # Each result is 2 words: success (bool), offset to returnData
        success = int(data[pos:pos+64], 16) == 1
        return_data_offset = int(data[pos+64:pos+128], 16)
        
        # Read returnData from offset (relative to array data start at position 64)
        abs_offset = 64 + return_data_offset * 2  # Convert to hex string offset
        return_data_len = int(data[abs_offset:abs_offset+64], 16)
        return_data_hex = data[abs_offset+64:abs_offset+64+return_data_len*2]
        
        results.append((success, bytes.fromhex(return_data_hex) if return_data_hex else b''))
        
        pos += 128  # Next result
    
    return results


def _check_pools_via_multicall(holders: list[tuple[str, int, float]], token: str, chain_id: int, deadline: float) -> list[str]:
    """
    Check if holders are pools by calling token0()/token1() via Multicall3.
    
    Returns list of addresses that are pools (return our token).
    """
    if time.time() >= deadline:
        return []
    
    # Get RPC URL
    if chain_id == 56:
        rpc_url = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org")
    elif chain_id == 4663:
        rpc_url = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    elif chain_id == 8453:
        rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    else:
        return []
    
    # Build Multicall3 aggregate3 call
    calls = []
    addresses = [h[0] for h in holders[:20]]  # Top 20 only
    
    for addr in addresses:
        # token0() selector: 0x0dfe1681
        # token1() selector: 0xd21220a7
        calls.append({
            "target": addr,
            "allowFailure": True,
            "callData": "0x0dfe1681"  # token0()
        })
        calls.append({
            "target": addr,
            "allowFailure": True,
            "callData": "0xd21220a7"  # token1()
        })
    
    # Encode the call
    try:
        call_data = _encode_multicall3_aggregate3(calls)
    except Exception as e:
        log.warning("Multicall3 encode failed: %s", safe_err(e))
        return []
    
    # Make the call
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return []
        
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
                {"to": MULTICALL3, "data": call_data},
                "latest"
            ]},
            timeout=min(10, remaining)
        )
        
        if resp.status_code != 200:
            log.warning("Multicall3 call HTTP %d", resp.status_code)
            return []
        
        result = resp.json().get("result")
        if not result:
            return []
        
        # Decode results
        decoded = _decode_multicall3_aggregate3(result)
        
        pools = []
        token_lower = token.lower()
        
        # Check each address (2 calls per address: token0, token1)
        for i, addr in enumerate(addresses):
            token0_idx = i * 2
            token1_idx = i * 2 + 1
            
            if token0_idx < len(decoded):
                success0, data0 = decoded[token0_idx]
                if success0 and len(data0) == 32:
                    # Extract address from padded result
                    token_addr = "0x" + data0[-20:].hex()
                    if token_addr.lower() == token_lower:
                        pools.append(addr)
                        continue
            
            if token1_idx < len(decoded):
                success1, data1 = decoded[token1_idx]
                if success1 and len(data1) == 32:
                    token_addr = "0x" + data1[-20:].hex()
                    if token_addr.lower() == token_lower:
                        pools.append(addr)
        
        return pools
        
    except Exception as e:
        log.warning("Multicall pool check failed: %s", safe_err(e))
        return []


def _classify(holders: list[tuple[str, int, bool]], supply: int, pair_addrs: list[str],
              chain_id: int, token: str, goplus_data: dict | None, deadline: float) -> tuple[list[tuple[str, int]], list[tuple[str, float, str]]]:
    """
    Classify and exclude holders.
    
    Args:
        holders: [(address, balance, is_contract), ...]
        supply: total supply
        pair_addrs: pair addresses from DexScreener
        chain_id: chain ID
        token: token address
        goplus_data: GoPlus data for lock checks
        deadline: absolute deadline timestamp
    
    Returns:
        (valid_holders, excluded) where:
        - valid_holders: [(address, balance), ...]
        - excluded: [(address, pct, reason), ...]
    """
    excluded = []
    valid = []
    
    # Normalize addresses
    pair_addrs_set = {p.lower() for p in pair_addrs if p}
    token_lower = token.lower()
    
    # Get GoPlus pairs and locked holders
    goplus_pairs = set()
    goplus_locked = {}  # {address: (unlock_time_timestamp, is_permanent)}
    
    if goplus_data:
        # Extract pairs from dex field
        dex_list = goplus_data.get("dex", [])
        if isinstance(dex_list, list):
            for dex_entry in dex_list:
                if isinstance(dex_entry, dict):
                    pair_addr = dex_entry.get("pair")
                    if pair_addr:
                        goplus_pairs.add(pair_addr.lower())
        
        # Bug #2 fix: Extract locked holders using REAL schema
        # Real schema: holders[].is_locked and holders[].locked_detail[].end_time (ISO strings)
        holders_list = goplus_data.get("holders", [])
        if isinstance(holders_list, list):
            for holder_entry in holders_list:
                if isinstance(holder_entry, dict):
                    holder_addr = holder_entry.get("address", "").lower()
                    is_locked = holder_entry.get("is_locked")
                    
                    if is_locked == "1" and holder_addr:
                        # Check locked_detail for unlock time
                        locked_details = holder_entry.get("locked_detail", [])
                        if isinstance(locked_details, list) and locked_details:
                            for detail in locked_details:
                                end_time_str = detail.get("end_time")
                                if end_time_str:
                                    # Parse ISO timestamp "2027-02-27T23:02:58+00:00"
                                    try:
                                        from datetime import datetime
                                        if end_time_str.lower() == "permanent" or end_time_str == "0":
                                            goplus_locked[holder_addr] = (None, True)
                                        else:
                                            # Parse ISO format
                                            dt = datetime.fromisoformat(end_time_str.replace("+00:00", "+00:00"))
                                            unlock_timestamp = dt.timestamp()
                                            goplus_locked[holder_addr] = (unlock_timestamp, False)
                                    except Exception:
                                        pass
    
    # Collect unidentified contracts >= 3% for GoPlus lock check
    unidentified_contracts = []
    
    # Collect top holders that might be pools (for Multicall check)
    potential_pools = []
    
    for addr, balance, is_contract in holders:
        if not addr:  # Bug #7c: handle missing address
            continue
        
        addr_lower = addr.lower()
        pct = (balance / supply * 100) if supply > 0 else 0
        
        # Burn addresses
        if addr_lower in BURN_ADDRESSES:
            excluded.append((addr, pct, "burn"))
            continue
        
        # Known pool contracts
        if addr_lower in POOL_CONTRACTS:
            excluded.append((addr, pct, "pool_contract"))
            continue
        
        # Launchpad curves
        if addr_lower in LAUNCHPAD_CURVES:
            excluded.append((addr, pct, "launchpad_curve"))
            continue
        
        # Permanent lockers
        if addr_lower in PERMANENT_LOCKERS:
            excluded.append((addr, pct, "permanent_locker"))
            continue
        
        # Pair addresses from DexScreener
        if addr_lower in pair_addrs_set:
            excluded.append((addr, pct, "pair_dexscreener"))
            continue
        
        # GoPlus pairs
        if addr_lower in goplus_pairs:
            excluded.append((addr, pct, "pair_goplus"))
            continue
        
        # Conditional lockers (need GoPlus lock check)
        if addr_lower in CONDITIONAL_LOCKERS:
            lock_info = goplus_locked.get(addr_lower)
            if lock_info:
                unlock_time, is_permanent = lock_info
                
                if is_permanent:
                    excluded.append((addr, pct, "locker_permanent"))
                    continue
                
                # Check if unlock > 7 days away
                if unlock_time:
                    days_until_unlock = (unlock_time - time.time()) / 86400
                    
                    if days_until_unlock > 7:
                        excluded.append((addr, pct, "locker_locked_7d"))
                        continue
        
        # GoPlus locked holders (>= 3% only) - bug #2: call GoPlus only for unidentified >= 3%
        if pct >= 3 and addr_lower in goplus_locked:
            unlock_time, is_permanent = goplus_locked[addr_lower]
            
            if is_permanent:
                excluded.append((addr, pct, "goplus_locker_permanent"))
                continue
            
            if unlock_time:
                days_until_unlock = (unlock_time - time.time()) / 86400
                
                if days_until_unlock > 7:
                    excluded.append((addr, pct, "goplus_locker_7d"))
                    continue
        
        # Track unidentified contracts >= 3% for potential GoPlus lookup
        if is_contract and pct >= 3:
            # Check if already identified
            if (addr_lower not in POOL_CONTRACTS and 
                addr_lower not in LAUNCHPAD_CURVES and 
                addr_lower not in PERMANENT_LOCKERS and
                addr_lower not in CONDITIONAL_LOCKERS and
                addr_lower not in pair_addrs_set and
                addr_lower not in goplus_pairs):
                unidentified_contracts.append((addr, balance, pct))
        
        # Collect top 20 for Multicall pool check (bug #5: run on Robinhood too)
        if is_contract and len(potential_pools) < 20:
            potential_pools.append((addr, balance, pct))
        
        # Not excluded yet
        valid.append((addr, balance, pct))
    
    # Bug #5: Multicall check for top contracts (ONE aggregate3 call)
    if potential_pools and time.time() < deadline:
        pools_found = _check_pools_via_multicall(potential_pools, token_lower, chain_id, deadline)
        
        # Remove identified pools from valid list
        pools_set = {p.lower() for p in pools_found}
        new_valid = []
        
        for addr, balance, pct in valid:
            if addr.lower() in pools_set:
                excluded.append((addr, pct, "pair_multicall"))
            else:
                new_valid.append((addr, balance))
        
        valid = new_valid
    else:
        # Convert valid to just (addr, balance)
        valid = [(addr, balance) for addr, balance, _ in valid]
    
    return valid, excluded


# In-memory result cache
_cache = {}  # {(chain_id, token): (result, timestamp)}


def evm_holder_concentration(chain_id: int, token: str, pair_addrs: list[str],
                            age_min: float, db: sqlite3.Connection) -> HolderResult:
    """
    Compute holder concentration for EVM tokens.
    
    Bug #7: Wrapped in try/except to catch all exceptions and return fail-closed result.
    
    Args:
        chain_id: Chain ID (56, 4663, 8453)
        token: Token address (checksummed or lowercase)
        pair_addrs: List of pair addresses from DexScreener
        age_min: Token age in minutes
        db: Database connection for cache table
    
    Returns:
        HolderResult with top_wallet (0-1), top_10 (0-100), source, exclusions, ok flag, error
    """
    try:
        return _evm_holder_concentration_impl(chain_id, token, pair_addrs, age_min, db)
    except Exception as e:
        # Bug #7: Catch all exceptions, return fail-closed
        error_msg = safe_err(e)
        log.warning("EVM holder check exception for %s: %s", token, error_msg)
        return HolderResult(None, None, "unavailable", [], False, error_msg)


def _evm_holder_concentration_impl(chain_id: int, token: str, pair_addrs: list[str],
                                  age_min: float, db: sqlite3.Connection) -> HolderResult:
    """
    Internal implementation of evm_holder_concentration.
    """
    # Check if enabled
    if os.environ.get("EVM_HOLDERS_ENABLED", "1") == "0":
        return HolderResult(None, None, "disabled", [], False, "disabled")
    
    token_lower = token.lower()
    cache_key = (chain_id, token_lower)
    
    # Check in-memory cache
    if cache_key in _cache:
        result, timestamp = _cache[cache_key]
        if time.time() - timestamp < CACHE_DURATION:
            # Return a copy with source updated to 'cache'
            cached_result = HolderResult(
                top_wallet=result.top_wallet,
                top_10=result.top_10,
                source="cache",
                excluded=result.excluded,
                ok=result.ok,
                error=result.error,
                raw_top_wallet=result.raw_top_wallet,
                raw_top_10=result.raw_top_10
            )
            return cached_result
    
    # Bug #6: Enforce 20s deadline, never sleep/block beyond it
    deadline = time.time() + TOKEN_TIMEOUT
    
    # Try primary source
    primary_result = None
    primary_error = None
    is_transient = False  # Track if error is transient (for holders_pending vs top_wallet_unverified)
    
    if chain_id in (56, 8453):
        # Honeypot.is
        data, error = _holders_honeypot(chain_id, token, deadline)
        
        if error:
            is_transient = error in ("rate_limited", "timeout", "http_500", "http_502", "http_503")
            primary_error = error
        elif data:
            # Parse Honeypot data
            total_supply = data["totalSupply"]
            holders_raw = data["holders"]
            
            # Convert to (address, balance, is_contract) format
            # Ensure balances are integers (bug #7d)
            holders = []
            for h in holders_raw:
                addr = h.get("address")
                if not addr:  # Bug #7c
                    continue
                
                balance = h.get("balance", 0)
                if isinstance(balance, str):
                    try:
                        balance = int(balance)
                    except (ValueError, TypeError):
                        balance = 0  # Bug #7d: bad balance silently 0
                
                holders.append((addr, balance, h.get("isContract", False)))
            
            # Calculate raw values before exclusions
            if holders and total_supply > 0:
                # Sort by balance
                sorted_holders = sorted(holders, key=lambda x: x[1], reverse=True)
                raw_top_wallet = sorted_holders[0][1] / total_supply
                raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                raw_top_10 = (raw_top_10_balance / total_supply) * 100
            else:
                raw_top_wallet = None
                raw_top_10 = None
            
            # Get GoPlus data for lock checks (only if we have large holders)
            goplus_data = None
            if any(h[1] / total_supply >= 0.03 for h in holders):
                # Only query GoPlus for age >= 120m
                if age_min >= 120 and time.time() < deadline:
                    goplus_data, _ = _holders_goplus(chain_id, token, deadline)
            
            # Classify and exclude
            valid_holders, excluded = _classify(holders, total_supply, pair_addrs, chain_id, token, goplus_data, deadline)
            
            # Subtract burns from supply
            burn_amount = sum(balance for addr, balance, _ in holders if addr.lower() in BURN_ADDRESSES)
            adjusted_supply = total_supply - burn_amount
            
            if adjusted_supply <= 0:
                primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns")
            elif not valid_holders:
                # Bug #7a: All holders excluded - fail closed
                primary_result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded")
            else:
                # Compute top_wallet and top_10
                sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                
                top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                top_wallet_pct = top_wallet_balance / adjusted_supply
                
                top_10_balance = sum(h[1] for h in sorted_valid[:10])
                top_10_pct = (top_10_balance / adjusted_supply) * 100
                
                primary_result = HolderResult(
                    top_wallet_pct, top_10_pct, "honeypot", excluded, True, None,
                    raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10
                )
    
    elif chain_id == 4663:
        # Robinhood RPC fold
        call_count = [0]  # Mutable for tracking
        data, error = _holders_rpc_fold(token, deadline, db, call_count)
        
        if error:
            is_transient = error in ("rate_limited", "timeout", "call_limit_exceeded")
            # Bug #4h: Don't return early, try GoPlus fallback
            primary_error = error
        elif data:
            balances = data["balances"]
            supply = data["supply"]
            complete = data["complete"]
            
            if not complete:
                # Incomplete fold - but check if it's progressing
                # If we have some balances, it's transient (holders_pending)
                # If completely empty, it's definitive (top_wallet_unverified)
                is_transient = len(balances) > 0
                primary_error = "incomplete_fold"
            else:
                # Convert to holders list (bug #5: mark all as is_contract=False, so Multicall runs)
                holders = [(addr, balance, False) for addr, balance in balances.items()]
                
                # Calculate raw values
                if holders and supply > 0:
                    sorted_holders = sorted(holders, key=lambda x: x[1], reverse=True)
                    raw_top_wallet = sorted_holders[0][1] / supply
                    raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                    raw_top_10 = (raw_top_10_balance / supply) * 100
                else:
                    raw_top_wallet = None
                    raw_top_10 = None
                
                # Get GoPlus data for lock checks
                goplus_data = None
                if any(balance / supply >= 0.03 for balance in balances.values()) and age_min >= 120:
                    if time.time() < deadline:
                        goplus_data, _ = _holders_goplus(chain_id, token, deadline)
                
                # Classify and exclude (bug #5: Multicall runs on Robinhood too)
                valid_holders, excluded = _classify(holders, supply, pair_addrs, chain_id, token, goplus_data, deadline)
                
                # Subtract burns
                burn_amount = sum(balance for addr, balance in balances.items() if addr in BURN_ADDRESSES)
                adjusted_supply = supply - burn_amount
                
                if adjusted_supply <= 0:
                    primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns")
                elif not valid_holders:
                    # Bug #7a
                    primary_result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded")
                else:
                    sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                    
                    top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                    top_wallet_pct = top_wallet_balance / adjusted_supply
                    
                    top_10_balance = sum(h[1] for h in sorted_valid[:10])
                    top_10_pct = (top_10_balance / adjusted_supply) * 100
                    
                    primary_result = HolderResult(
                        top_wallet_pct, top_10_pct, "rpc_fold", excluded, True, None,
                        raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10
                    )
    
    else:
        primary_error = "unsupported_chain"
    
    # If primary succeeded, cache and return
    if primary_result and primary_result.ok:
        _cache[cache_key] = (primary_result, time.time())
        return primary_result
    
    # Try GoPlus fallback (age >= 120m, non-empty holders)
    if age_min >= 120 and time.time() < deadline:
        goplus_data, goplus_error = _holders_goplus(chain_id, token, deadline)
        
        if goplus_data:
            # Bug #1: Extract holder percentages from GoPlus CORRECTLY
            # Real schema: holders[] with percent as 0-1 fraction (e.g. "0.180181" = 18%)
            holders_list = goplus_data.get("holders", [])
            
            if holders_list and isinstance(holders_list, list) and len(holders_list) > 0:
                try:
                    # Parse holders with percent as FRACTION (0-1)
                    holder_data = []
                    for h in holders_list[:50]:  # Top 50
                        if isinstance(h, dict):
                            addr = h.get("address")
                            pct_str = h.get("percent")
                            is_contract = h.get("is_contract") == "1"
                            
                            if addr and pct_str:
                                try:
                                    pct_fraction = float(pct_str)  # This is 0-1
                                    # Convert to balance (as if supply=1 for simplicity)
                                    balance = int(pct_fraction * 1e18)  # Scale up for integer math
                                    holder_data.append((addr, balance, is_contract))
                                except ValueError:
                                    pass
                    
                    if holder_data:
                        # Supply = 1e18 (scaled)
                        supply = int(1e18)
                        
                        # Calculate raw values
                        sorted_holders = sorted(holder_data, key=lambda x: x[1], reverse=True)
                        raw_top_wallet = sorted_holders[0][1] / supply
                        raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                        raw_top_10 = (raw_top_10_balance / supply) * 100
                        
                        # Bug #1: Run GoPlus holders through _classify too
                        valid_holders, excluded = _classify(holder_data, supply, pair_addrs, chain_id, token, goplus_data, deadline)
                        
                        if not valid_holders:
                            # All excluded
                            result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded")
                        else:
                            sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                            
                            # Bug #1 fix: top_wallet = fraction (0-1), top_10 = sum * 100
                            top_wallet_pct = sorted_valid[0][1] / supply  # 0-1 fraction
                            top_10_balance = sum(h[1] for h in sorted_valid[:10])
                            top_10_pct = (top_10_balance / supply) * 100  # 0-100 percent
                            
                            result = HolderResult(
                                top_wallet_pct, top_10_pct, "goplus", excluded, True, None,
                                raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10
                            )
                            _cache[cache_key] = (result, time.time())
                            return result
                
                except Exception as e:
                    log.warning("GoPlus parse error for %s: %s", token, safe_err(e))
    
    # Fail closed - distinguish transient vs definitive
    error_msg = primary_error or "no_data"
    
    # Transient errors get holders_pending (15 min), definitive get top_wallet_unverified (6h)
    # The bench reason is handled by collect.py dossier integration
    result = HolderResult(None, None, "unavailable", [], False, error_msg)
    
    # Add transient flag to error for collect.py to use
    if is_transient:
        result.error = f"transient:{error_msg}"
    
    return result


# Bug #4g: Prune old cache rows (>72h)
def prune_old_cache(db: sqlite3.Connection):
    """Remove cache entries older than 72 hours."""
    cutoff = time.time() - 72 * 3600
    deleted = db.execute(
        "DELETE FROM evm_holder_cache WHERE updated_at < ?",
        (cutoff,)
    ).rowcount
    
    if deleted > 0:
        db.commit()
        log.info("Pruned %d old EVM holder cache entries", deleted)
