#!/usr/bin/env python3
"""
Live smoke test for EVM holder concentration on BSC and Robinhood tokens.
"""
import sys
import requests
import time
import sqlite3

# Add current directory to path
sys.path.insert(0, "/workspace")

import evm_holders
import book

# Initialize DB
db = book.DB

# Get trending tokens from GeckoTerminal
def get_trending_tokens(network: str, count: int = 3):
    """Fetch trending tokens from GT."""
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/trending_pools"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"Failed to fetch {network} trending: HTTP {resp.status_code}")
            return []
        
        data = resp.json()
        pools = data.get("data", [])[:count]
        
        tokens = []
        for pool in pools:
            attrs = pool.get("attributes", {})
            token_addr = attrs.get("base_token_address")
            token_symbol = attrs.get("base_token_symbol", "UNKNOWN")
            pair_addr = attrs.get("address")
            
            if token_addr:
                tokens.append({
                    "address": token_addr,
                    "symbol": token_symbol,
                    "pair_address": pair_addr,
                    "network": network
                })
        
        return tokens
    except Exception as e:
        print(f"Error fetching {network} trending: {e}")
        return []


def test_token(chain_id: int, token_addr: str, symbol: str, pair_addr: str | None, network: str):
    """Test holder concentration on one token."""
    print(f"\n{'='*80}")
    print(f"Testing {symbol} on {network}")
    print(f"Token: {token_addr}")
    print(f"Pair: {pair_addr}")
    print(f"{'='*80}")
    
    pair_addrs = [pair_addr] if pair_addr else []
    
    result = evm_holders.evm_holder_concentration(
        chain_id=chain_id,
        token=token_addr,
        pair_addrs=pair_addrs,
        age_min=120,  # Assume >= 2h old
        db=db
    )
    
    print(f"Result: ok={result.ok}, source={result.source}")
    
    if result.ok:
        print(f"Raw top_wallet: {result.raw_top_wallet*100:.2f}%" if result.raw_top_wallet else "N/A")
        print(f"Raw top_10: {result.raw_top_10:.2f}%" if result.raw_top_10 else "N/A")
        print(f"Post-exclusion top_wallet: {result.top_wallet*100:.2f}%")
        print(f"Post-exclusion top_10: {result.top_10:.2f}%")
        print(f"Excluded {len(result.excluded)} holders:")
        for addr, pct, reason in result.excluded[:5]:
            print(f"  - {addr[:10]}... {pct:.2f}% ({reason})")
        
        # Determine chain_kill outcome
        if result.top_wallet > 0.05:
            outcome = f"KILL (top_wallet: {result.top_wallet*100:.2f}% > 5%)"
        elif result.top_10 > 60:
            outcome = f"KILL (top_10: {result.top_10:.2f}% > 60%)"
        else:
            outcome = "PASS"
        
        print(f"\nchain_kill outcome: {outcome}")
    else:
        print(f"Error: {result.error}")
        is_transient = result.error and result.error.startswith("transient:")
        bench_reason = "holders_pending (15 min)" if is_transient else "top_wallet_unverified (6h)"
        print(f"Bench reason: {bench_reason}")
        print(f"\nchain_kill outcome: KILL (fail-closed)")


def main():
    print("EVM Holder Concentration Live Smoke Test")
    print("==========================================\n")
    
    # BSC (chain 56, network 'bsc')
    print("Fetching BSC trending tokens...")
    bsc_tokens = get_trending_tokens("bsc", 2)
    
    # Robinhood (chain 4663, network 'robinhood')
    print("Fetching Robinhood trending tokens...")
    rh_tokens = get_trending_tokens("robinhood", 2)
    
    # Test BSC tokens
    for token in bsc_tokens:
        test_token(56, token["address"], token["symbol"], token["pair_address"], "BSC")
        time.sleep(2)  # Rate limit
    
    # Test Robinhood tokens
    for token in rh_tokens:
        test_token(4663, token["address"], token["symbol"], token["pair_address"], "Robinhood")
        time.sleep(2)  # Rate limit
    
    print("\n" + "="*80)
    print("Smoke test complete!")
    print("="*80)


if __name__ == "__main__":
    main()
