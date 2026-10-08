# EVM Holder Concentration Implementation Summary

## Overview
Implemented real holder concentration analysis for EVM tokens (BSC 56, Robinhood 4663, Base 8453) using free keyless sources, replacing the fail-closed `top_wallet_unverified` behavior.

## Implementation Details

### Core Module: `evm_holders.py`
- **Function**: `evm_holder_concentration(chain_id, token, pair_addrs, age_min, db) -> HolderResult`
- **Units**: 
  - `top_wallet`: 0-1 fraction (compared to 0.05 in filter.py)
  - `top_10`: 0-100 percent (divided by 100 in filter.py)
- **Denominator**: Supply minus burned addresses (consistent with Solana)

### Data Sources

#### 1. Honeypot.is API (BSC, Base)
- Endpoint: `GET https://api.honeypot.is/v1/TopHolders?address={token}&chainID={id}`
- Rate limit: 1 req/s
- Returns: totalSupply, holders[50] with address, balance, isContract
- Performance: 0.2-0.4s, data ~25m old

#### 2. Robinhood RPC Transfer-log Fold
- Public RPC: `https://rpc.mainnet.chain.robinhood.com`
- Method: Fold Transfer(from, to, value) logs from first mint to head-20
- Adaptive window: 250k blocks, halve on "exceeds limit", double when <4k logs
- Completeness: |minted - totalSupply| <= 1e-6 * supply
- SQLite cache: `evm_holder_cache(chain_id, token, last_block, balances_json, supply, updated_at)`
- Incremental: Resume from last_block+1 each cycle

#### 3. GoPlus Secondary (age >= 120m)
- Endpoint: `GET https://api.gopluslabs.io/api/v1/token_security/{chainId}?contract_addresses={addr}`
- Rate limit: 150 CU/min (assume 1 CU/call)
- Used for: Fallback when primary unavailable, lock tag checks for >= 3% holders

### Exclusions

#### Automatic Exclusions
- **Burns**: 0x0...000, 0x0...dead, 0xdead...942069
- **Pool Contracts**:
  - BSC Uniswap v4 PoolManager: 0x28e2ea090877bf75740558f6bfb36a5ffee9e9df
  - BSC PancakeSwap Infinity Vault: 0x238a358808379702088667322f80ac48bad5e6c4
  - Robinhood Uniswap v4 PoolManager: 0x8366a39cc670b4001a1121b8f6a443a643e40951
- **Launchpad Curves**:
  - Flap Portal BSC: 0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0
  - Flap Portal Robinhood: 0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09
  - four.meme TokenManager2: 0x5c952063c7fc8610ffdb798152d69f0b9550762b
- **Permanent Lockers**:
  - RobinFunFi V2 LaunchLocker: 0x267444d099b10fb5ed7c3cc7b7c767adca574952
- **Pairs**: DexScreener pairAddress + GoPlus dex[].pair + Multicall3 token0()/token1() detection

#### Conditional Exclusions
- **PinkLock02** (0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe): Only if unlock > 7 days or permanent
- **GoPlus locked holders**: Only >= 3% holdings with unlock > 7 days

#### NOT Excluded (Intentional)
- Unknown contracts
- EIP-7702 wallets (code starts 0xef0100)
- Token contract itself
- Exchange wallets

### Integration

#### book.py
- Added SQLite migration for `evm_holder_cache` table

#### collect.py
1. Modified `trade_counts()` to return `pair_address` from DexScreener
2. Modified `dossier()` for EVM chains:
   - Calls `evm_holder_concentration()`
   - Stores results in `top_wallet_percent` and `top_10_percent`
   - Logs exclusions and values on kill thresholds
   - Fail closed on unavailable: `top_wallet_percent = None` -> `top_wallet_unverified`

#### filter.py
- Existing `top_wallet_unverified` check now passes when we have data
- `top_wallet` check: kills at 0.05 (5%)
- `top_10` check: kills at 60%

### Rate Limits & Performance
- **Per-token budget**: 20s timeout
- **Result cache**: 10 min in-memory
- **Honeypot**: 1 req/s with 429 backoff
- **Robinhood RPC**: 5 req/s with 429 backoff
- **GoPlus**: 150 CU/min (~2.5/s)
- **Never blocks cycle**: timeout -> unverified

### Error Handling
- All exceptions through `safe_err()` (secrets redacted)
- Network timeouts: bounded per-token (20s)
- Fail closed: no ok result -> `top_wallet_unverified` (6h bench)

## Tests

### Test Suite: `tests/test_evm_holders.py` (19 tests, all passing)

#### Exclusion Logic Tests
1. `test_honeypot_fitcoin_pair_excluded_pass` - FITCOIN: 39% pair excluded -> 2.15%/16.55% passes
2. `test_honeypot_whale_kills` - SpaceXSI: 39.6% top wallet kills
3. `test_honeypot_burn_denominator` - ZNHJ: 79% burned -> 25.1% after adjustment kills
4. `test_pool_manager_excluded` - Robinhood PoolManager excluded
5. `test_flap_portal_excluded` - Flap Portal launchpad curve excluded
6. `test_pinklock_permanent_excluded` - PinkLock with permanent lock excluded
7. `test_locker_unlocking_under_7d_counted` - Locker unlocking <7d counted (not excluded)
8. `test_unknown_contract_counted` - WOJAK: 25.5% unknown contract kills (not excluded)

#### Fail-Closed Tests
9. `test_robinhood_fold_incomplete` - Incomplete fold -> top_wallet_unverified
10. `test_honeypot_invalid_chain` - Invalid chain -> unavailable
11. `test_honeypot_timeout` - Timeout -> unavailable

#### Units & Integration Tests
12. `test_units_at_gates` - top_wallet 0.0546 kills, top_10 61 kills
13. `test_evm_with_ok_result_no_longer_unverified` - EVM with ok result passes
14. `test_dossier_integration_bsc_pass` - BSC dossier with pair exclusion passes
15. `test_dossier_integration_bsc_whale_kill` - BSC dossier with whale kills
16. `test_dossier_integration_robinhood_poolmanager` - Robinhood with PoolManager passes
17. `test_dossier_integration_robinhood_incomplete` - Incomplete fold -> unverified

#### Infrastructure Tests
18. `test_secret_scrubbing` - All errors go through safe_err()
19. `test_cache` - 10-min result cache works

### Existing Tests
- Kept full existing suite green (no tests deleted or weakened)
- Modified tests: None (no filter/threshold changes needed)

## Live Smoke Test Results

Tested on current GeckoTerminal trending tokens (Oct 8, 2026):

### BSC Tokens
1. **0xbe9d156892e55e7154bcd3cb0fea677f9d3103e1**
   - Source: honeypot
   - Top wallet: 58.76% (KILL - whale)
   - Top 10: 84.13%
   - Excluded: Flap Portal (0.29%), PoolManager (0.09%), pair via Multicall (0.99%)

2. **0x7ab8d02cbb51ff7223fde700eaaa2a91bf750314**
   - Source: honeypot
   - Top wallet: 19.94% (KILL - whale)
   - Top 10: 81.67% (KILL - concentration)
   - Excluded: Burn 18.02%, pair via Multicall (2.61%)

### Robinhood Token
3. **0x21551503bcbeafa2abea7831de8598eafbed9a79**
   - Source: rpc_fold
   - Top wallet: 8.04% (PASS)
   - Top 10: 35.91% (PASS)
   - Excluded: PoolManager 12.66%, RobinFunFi LaunchLocker 8.16%, burn 1.09%

## Key Decisions & Trade-offs

1. **Fail closed**: Better to miss good tokens than pass bad ones
2. **Denominator = supply - burns**: Consistent with Solana behavior
3. **Pool detection**: Three-layer approach (known addresses, GoPlus, Multicall)
4. **Robinhood fold**: Incremental with SQLite cache, adaptive window sizing
5. **Rate limiting**: Per-source limiters with 429 backoff
6. **GoPlus usage**: Only for age >= 120m (fresh tokens lack holder data) and lock checks >= 3%
7. **Cache**: 10 min to avoid redundant API calls while staying fresh

## Hard Rules Followed

- ✅ No changes to thresholds.py, SOFT/HARD limits, min_age, filter order
- ✅ All exceptions through safe_err()
- ✅ No secrets in code
- ✅ Network calls bounded (20s per token)
- ✅ Never crash or block cycle
- ✅ SQLite migration pattern in book.py
- ✅ Tests: offline fixtures only
- ✅ Full existing suite green, no tests weakened

## Files Changed

1. **evm_holders.py** (NEW) - 1,071 lines
2. **book.py** - Added evm_holder_cache table migration
3. **collect.py** - Wired evm_holder_concentration into dossier, added pair_address to trade_counts
4. **tests/test_evm_holders.py** (NEW) - 19 comprehensive tests

## Commits

1. `33d3866` - Add EVM holder concentration module (WIP)
2. `538dd65` - Fix EVM holder tests - all 19 passing
3. `a02f1ff` - Fix EVM holder bugs found in live smoke test

## Branch

- **Name**: `cursor/evm-holder-concentration-2512`
- **Head SHA**: `a02f1ff`
- **Base**: `main` (54a9164, PR #36)
- **Test count**: 19 new tests (all passing)
- **Modified tests**: 0 (no existing tests changed)
