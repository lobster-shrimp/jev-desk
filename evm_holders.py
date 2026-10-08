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
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from collections import defaultdict

import requests

from secret_utils import safe_err


log = logging.getLogger("evm_holders")


# Result cache duration (10 minutes)
CACHE_DURATION = 600

# Per-token time budget (20 seconds)
TOKEN_TIMEOUT = 20

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


class RateLimiter:
    """Simple rate limiter with 429 backoff."""
    
    def __init__(self, max_per_sec: float):
        self.max_per_sec = max_per_sec
        self.min_interval = 1.0 / max_per_sec
        self.last_call = 0.0
        self.backoff_until = 0.0
    
    def wait(self):
        """Wait if needed to respect rate limit and backoff."""
        now = time.time()
        
        # Wait for 429 backoff
        if self.backoff_until > now:
            sleep_time = self.backoff_until - now
            log.info("Rate limit backoff: waiting %.1fs", sleep_time)
            time.sleep(sleep_time)
            now = time.time()
        
        # Wait for rate limit
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
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


def _holders_honeypot(chain_id: int, token: str, timeout: float) -> tuple[dict | None, str | None]:
    """
    Fetch holder data from Honeypot.is API.
    
    Returns (data, error) where data is {"totalSupply": int, "holders": [{address, balance, isContract}, ...]}.
    Returns (None, error_msg) on failure.
    """
    limiter = _get_limiter("honeypot", 1.0)  # 1 req/s
    limiter.wait()
    
    url = "https://api.honeypot.is/v1/TopHolders"
    params = {"address": token, "chainID": chain_id}
    
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        
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
        if total_supply == 0 or total_supply is None:
            return None, "zero_supply"
        
        holders = data.get("holders", [])
        if not holders:
            return None, "no_holders"
        
        return data, None
        
    except requests.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, safe_err(e)


def _holders_rpc_fold(token: str, timeout: float, db: sqlite3.Connection) -> tuple[dict | None, str | None]:
    """
    Fold Transfer logs on Robinhood RPC to compute holder balances.
    
    Returns (data, error) where data is {"balances": {address: int}, "supply": int, "complete": bool}.
    Uses incremental cache from evm_holder_cache table.
    """
    rpc_url = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    limiter = _get_limiter("robinhood_rpc", 5.0)  # 5 req/s
    
    token_lower = token.lower()
    start_time = time.time()
    
    def rpc_call(method: str, params: list) -> tuple[dict | None, str | None]:
        """Make one RPC call with rate limiting."""
        if time.time() - start_time > timeout:
            return None, "timeout"
        
        limiter.wait()
        
        try:
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=min(10, timeout - (time.time() - start_time))
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
                    return None, error.get("message", "rpc_error")
                return None, str(error)
            
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
        import json
        last_block, balances_json, cached_supply, updated_at = cached
        balances = json.loads(balances_json)
        start_block = last_block + 1
        log.info("Robinhood fold for %s: resuming from block %d", token, last_block)
    else:
        balances = {}
        start_block = None
        log.info("Robinhood fold for %s: starting from mint", token)
    
    # Find mint block if not cached
    if start_block is None:
        # Binary search for first mint (Transfer from 0x0)
        # Start from a reasonable range (last 10M blocks)
        search_start = max(1, head_block - 10_000_000)
        search_end = head_block
        
        mint_block = None
        
        # Use getLogs to find first Transfer(from=0x0)
        # Try chunks to avoid "exceeds limit"
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        zero_addr = "0x0000000000000000000000000000000000000000"
        
        # Try to find mint in last 10M blocks in chunks
        chunk_size = 500_000
        for chunk_start in range(search_start, search_end, chunk_size):
            chunk_end = min(chunk_start + chunk_size - 1, search_end)
            
            logs_result, error = rpc_call("eth_getLogs", [{
                "fromBlock": hex(chunk_start),
                "toBlock": hex(chunk_end),
                "address": token,
                "topics": [transfer_topic, "0x" + zero_addr[2:].zfill(64)]
            }])
            
            if error:
                if "exceeds" in error.lower() or "limit" in error.lower():
                    # Chunk too large, give up on this approach
                    break
                return None, f"getLogs mint search: {error}"
            
            if logs_result:
                # Found some mints, use first
                try:
                    mint_block = int(logs_result[0]["blockNumber"], 16)
                    log.info("Robinhood fold: found mint at block %d", mint_block)
                    break
                except (KeyError, ValueError, IndexError):
                    pass
        
        if mint_block is None:
            # Couldn't find mint, start from 10M blocks ago
            mint_block = search_start
            log.warning("Robinhood fold: couldn't find mint, starting from block %d", mint_block)
        
        start_block = mint_block
    
    # Fetch Transfer logs incrementally with adaptive window
    window_size = 250_000  # Start with 250k blocks
    current = start_block
    
    while current <= head_block:
        if time.time() - start_time > timeout:
            break
        
        to_block = min(current + window_size - 1, head_block)
        
        logs_result, error = rpc_call("eth_getLogs", [{
            "fromBlock": hex(current),
            "toBlock": hex(to_block),
            "address": token,
            "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"]  # Transfer
        }])
        
        if error:
            if "exceeds" in error.lower() or "limit" in error.lower():
                # Halve window size and retry
                window_size = max(10_000, window_size // 2)
                log.info("Robinhood fold: halving window to %d blocks", window_size)
                continue
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
                    
                    if from_addr:
                        from_addr = from_addr.lower()
                        balances[from_addr] = balances.get(from_addr, 0) - value
                        if balances[from_addr] <= 0:
                            balances.pop(from_addr, None)
                    
                    if to_addr:
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
        
        current = to_block + 1
    
    # Update cache
    import json
    db.execute(
        "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (4663, token_lower, to_block, json.dumps(balances), 0, time.time())
    )
    db.commit()
    
    # Calculate total supply from balances
    total_supply = sum(balances.values())
    
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
    
    # Check completeness
    complete = False
    if actual_supply > 0:
        diff = abs(total_supply - actual_supply)
        complete = diff <= actual_supply * 1e-6
    
    return {
        "balances": balances,
        "supply": total_supply if complete else actual_supply,
        "complete": complete
    }, None


def _holders_goplus(chain_id: int, token: str, timeout: float) -> tuple[dict | None, str | None]:
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
    limiter.wait()
    
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_str}"
    params = {"contract_addresses": token}
    
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        
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


def _classify(holders: list[tuple[str, int, int]], supply: int, pair_addrs: list[str],
              chain_id: int, token: str, goplus_data: dict | None) -> tuple[list[tuple[str, int]], list[tuple[str, float, str]]]:
    """
    Classify and exclude holders.
    
    Args:
        holders: [(address, balance, is_contract), ...]
        supply: total supply
        pair_addrs: pair addresses from DexScreener
        chain_id: chain ID
        token: token address
        goplus_data: GoPlus data for lock checks
    
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
    goplus_locked = {}  # {address: (unlock_time, is_permanent)}
    
    if goplus_data:
        # Extract pairs from dex field
        dex_list = goplus_data.get("dex", [])
        if isinstance(dex_list, list):
            for dex_entry in dex_list:
                if isinstance(dex_entry, dict):
                    pair_addr = dex_entry.get("pair")
                    if pair_addr:
                        goplus_pairs.add(pair_addr.lower())
        
        # Extract locked holders
        locked_detail = goplus_data.get("locked_detail", [])
        if isinstance(locked_detail, list):
            for lock_entry in locked_detail:
                if isinstance(lock_entry, dict):
                    holder = lock_entry.get("holder", "").lower()
                    end_time = lock_entry.get("end_time")
                    is_permanent = lock_entry.get("is_permanent", False)
                    
                    if holder:
                        goplus_locked[holder] = (end_time, is_permanent)
    
    # Collect top holders that might be pools (for Multicall check)
    potential_pools = []
    
    for addr, balance, is_contract in holders:
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
        if addr_lower in CONDITIONAL_LOCKERS and goplus_data:
            lock_info = goplus_locked.get(addr_lower)
            if lock_info:
                end_time, is_permanent = lock_info
                
                if is_permanent:
                    excluded.append((addr, pct, "locker_permanent"))
                    continue
                
                # Check if unlock > 7 days away
                if end_time:
                    try:
                        unlock_timestamp = float(end_time)
                        days_until_unlock = (unlock_timestamp - time.time()) / 86400
                        
                        if days_until_unlock > 7:
                            excluded.append((addr, pct, "locker_locked_7d"))
                            continue
                    except (ValueError, TypeError):
                        pass
        
        # GoPlus locked holders (>= 3% only)
        if pct >= 3 and addr_lower in goplus_locked and goplus_data:
            end_time, is_permanent = goplus_locked[addr_lower]
            
            if is_permanent:
                excluded.append((addr, pct, "goplus_locker_permanent"))
                continue
            
            if end_time:
                try:
                    unlock_timestamp = float(end_time)
                    days_until_unlock = (unlock_timestamp - time.time()) / 86400
                    
                    if days_until_unlock > 7:
                        excluded.append((addr, pct, "goplus_locker_7d"))
                        continue
                except (ValueError, TypeError):
                    pass
        
        # Collect top 20 contracts for Multicall pool check
        if is_contract and len(potential_pools) < 20:
            potential_pools.append((addr, balance, pct))
        
        # Not excluded yet
        valid.append((addr, balance, pct))
    
    # Multicall check for top contracts
    if potential_pools:
        pools_found = _check_pools_via_multicall(potential_pools, token_lower, chain_id)
        
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


def _check_pools_via_multicall(holders: list[tuple[str, int, float]], token: str, chain_id: int) -> list[str]:
    """
    Check if holders are pools by calling token0()/token1() via Multicall3.
    
    Returns list of addresses that are pools (return our token).
    """
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
    # aggregate3(Call3[] calldata calls) returns (Result[] memory returnData)
    # struct Call3 { address target; bool allowFailure; bytes callData; }
    
    calls = []
    addresses = [h[0] for h in holders]
    
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
    
    # Encode aggregate3 call
    # We'll use eth_call directly with encoded data
    try:
        # For simplicity, we'll call each pair individually rather than batch
        # (full Multicall3 encoding is complex without web3.py)
        pools = []
        
        limiter = _get_limiter(f"rpc_{chain_id}", 2.0)  # 2 req/s for pool checks
        
        for addr in addresses:
            limiter.wait()
            
            # Call token0()
            try:
                resp = requests.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
                        {"to": addr, "data": "0x0dfe1681"},
                        "latest"
                    ]},
                    timeout=5
                )
                
                result = resp.json().get("result")
                if result and len(result) == 66:  # 0x + 64 hex chars
                    # Extract address from padded result
                    token_addr = "0x" + result[-40:]
                    if token_addr.lower() == token.lower():
                        pools.append(addr)
                        continue
            except Exception:
                pass
            
            # Call token1()
            try:
                resp = requests.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
                        {"to": addr, "data": "0xd21220a7"},
                        "latest"
                    ]},
                    timeout=5
                )
                
                result = resp.json().get("result")
                if result and len(result) == 66:
                    token_addr = "0x" + result[-40:]
                    if token_addr.lower() == token.lower():
                        pools.append(addr)
            except Exception:
                pass
        
        return pools
        
    except Exception as e:
        log.warning("Multicall pool check failed: %s", safe_err(e))
        return []


# In-memory result cache
_cache = {}  # {(chain_id, token): (result, timestamp)}


def evm_holder_concentration(chain_id: int, token: str, pair_addrs: list[str],
                            age_min: float, db: sqlite3.Connection) -> HolderResult:
    """
    Compute holder concentration for EVM tokens.
    
    Args:
        chain_id: Chain ID (56, 4663, 8453)
        token: Token address (checksummed or lowercase)
        pair_addrs: List of pair addresses from DexScreener
        age_min: Token age in minutes
        db: Database connection for cache table
    
    Returns:
        HolderResult with top_wallet (0-1), top_10 (0-100), source, exclusions, ok flag, error
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
                error=result.error
            )
            return cached_result
    
    start_time = time.time()
    
    # Try primary source
    primary_result = None
    primary_error = None
    
    if chain_id in (56, 8453):
        # Honeypot.is
        data, error = _holders_honeypot(chain_id, token, TOKEN_TIMEOUT)
        
        if data:
            # Parse Honeypot data
            total_supply = data["totalSupply"]
            holders_raw = data["holders"]
            
            # Convert to (address, balance, is_contract) format
            holders = [(h["address"], h["balance"], h.get("isContract", False)) for h in holders_raw]
            
            # Get GoPlus data for lock checks (only if we have large holders)
            goplus_data = None
            if any(h[1] / total_supply >= 0.03 for h in holders):
                # Only query GoPlus for age >= 120m
                if age_min >= 120:
                    goplus_data, _ = _holders_goplus(chain_id, token, TOKEN_TIMEOUT - (time.time() - start_time))
            
            # Classify and exclude
            valid_holders, excluded = _classify(holders, total_supply, pair_addrs, chain_id, token, goplus_data)
            
            # Subtract burns from supply
            burn_amount = sum(balance for addr, balance in [(h[0], h[1]) for h in holders if h[0].lower() in BURN_ADDRESSES])
            adjusted_supply = total_supply - burn_amount
            
            if adjusted_supply <= 0:
                primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns")
            else:
                # Compute top_wallet and top_10
                if valid_holders:
                    sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                    
                    top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                    top_wallet_pct = top_wallet_balance / adjusted_supply
                    
                    top_10_balance = sum(h[1] for h in sorted_valid[:10])
                    top_10_pct = (top_10_balance / adjusted_supply) * 100
                    
                    primary_result = HolderResult(top_wallet_pct, top_10_pct, "honeypot", excluded, True, None)
                else:
                    # All holders excluded
                    primary_result = HolderResult(0.0, 0.0, "honeypot", excluded, True, None)
        
        primary_error = error
    
    elif chain_id == 4663:
        # Robinhood RPC fold
        data, error = _holders_rpc_fold(token, TOKEN_TIMEOUT, db)
        
        if data:
            balances = data["balances"]
            supply = data["supply"]
            complete = data["complete"]
            
            if not complete:
                # Incomplete fold -> fail closed
                return HolderResult(None, None, "unavailable", [], False, "incomplete_fold")
            
            # Convert to holders list
            holders = [(addr, balance, False) for addr, balance in balances.items()]  # Assume not contract for now
            
            # Get GoPlus data for lock checks
            goplus_data = None
            if any(balance / supply >= 0.03 for balance in balances.values()) and age_min >= 120:
                goplus_data, _ = _holders_goplus(chain_id, token, TOKEN_TIMEOUT - (time.time() - start_time))
            
            # Classify and exclude
            valid_holders, excluded = _classify(holders, supply, pair_addrs, chain_id, token, goplus_data)
            
            # Subtract burns
            burn_amount = sum(balance for addr, balance in balances.items() if addr in BURN_ADDRESSES)
            adjusted_supply = supply - burn_amount
            
            if adjusted_supply <= 0:
                primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns")
            else:
                if valid_holders:
                    sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                    
                    top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                    top_wallet_pct = top_wallet_balance / adjusted_supply
                    
                    top_10_balance = sum(h[1] for h in sorted_valid[:10])
                    top_10_pct = (top_10_balance / adjusted_supply) * 100
                    
                    primary_result = HolderResult(top_wallet_pct, top_10_pct, "rpc_fold", excluded, True, None)
                else:
                    primary_result = HolderResult(0.0, 0.0, "rpc_fold", excluded, True, None)
        
        primary_error = error
    
    else:
        primary_error = "unsupported_chain"
    
    # If primary succeeded, cache and return
    if primary_result and primary_result.ok:
        _cache[cache_key] = (primary_result, time.time())
        return primary_result
    
    # Try GoPlus fallback (age >= 120m, non-empty holders)
    if age_min >= 120:
        goplus_data, goplus_error = _holders_goplus(chain_id, token, TOKEN_TIMEOUT - (time.time() - start_time))
        
        if goplus_data:
            # Extract holder percentages from GoPlus
            holders_list = goplus_data.get("holders", [])
            
            if holders_list and isinstance(holders_list, list) and len(holders_list) > 0:
                # GoPlus holders format: [{"address": "0x...", "percent": "5.23", ...}, ...]
                # Convert to our format
                try:
                    # Parse percentages
                    holder_pcts = []
                    for h in holders_list[:10]:  # Top 10
                        if isinstance(h, dict):
                            pct_str = h.get("percent")
                            if pct_str:
                                try:
                                    pct = float(pct_str)
                                    holder_pcts.append(pct)
                                except ValueError:
                                    pass
                    
                    if holder_pcts:
                        top_wallet_pct = holder_pcts[0] / 100  # Convert to 0-1
                        top_10_pct = sum(holder_pcts)  # Keep as 0-100
                        
                        result = HolderResult(top_wallet_pct, top_10_pct, "goplus", [], True, None)
                        _cache[cache_key] = (result, time.time())
                        return result
                
                except Exception as e:
                    log.warning("GoPlus parse error for %s: %s", token, safe_err(e))
    
    # Fail closed
    error_msg = primary_error or "no_data"
    return HolderResult(None, None, "unavailable", [], False, error_msg)
