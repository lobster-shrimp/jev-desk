# RPC Cost Analysis for Pool-Aware Solana Holder Math

## Calls Per Dossier

The pool-aware holder math adds **~2 batched RPC calls** per Solana token dossier:

1. **getMultipleAccounts** (token account owners): 
   - Fetches owner info for top N token accounts (limited to 15 to bound cost)
   - Batched call resolving multiple accounts at once
   - ~1 RPC call

2. **getMultipleAccounts** (owner programs):
   - Fetches program ownership for unique owners
   - Batched call, only for uncached owners
   - ~1 RPC call
   - Cached per cycle, so repeated tokens don't re-fetch

## Caching Strategy

- Owner lookups are cached per cycle via `_sol_owner_cache`
- Cache is cleared at cycle start via `_clear_sol_owner_cache()`
- Reduces redundant RPC calls for tokens with shared pool vaults

## Rate Limit Bounds

- Token accounts limited to top 15 (prevents excessive RPC calls)
- Owner programs limited to 15 unique owners
- Both use batched `getMultipleAccounts` calls (not per-account)
- Existing calls: getTokenSupply (1) + getTokenLargestAccounts (1) = 2
- New calls: ~2 batched getMultipleAccounts
- **Total per Solana dossier: ~4 RPC calls** (was 2, now 4)

## Fallback Behavior

If owner resolution fails:
- Falls back to including the account (fail open for RPC issues)
- Logs warning but continues processing
- Existing fallback behavior preserved

## Cost vs Benefit

- Cost: +2 RPC calls per Solana token (~50% increase per dossier)
- Benefit: Excludes pool vaults that falsely triggered 11/14 recent kills
- Net: Prevents false kills worth significantly more than RPC cost
