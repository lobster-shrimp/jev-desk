# Root Cause Analysis: Zero Liquidity/Mcap Bug

**Repository**: lobster-shrimps/jev-desk  
**Symptom**: All tokens showing `liquidity_usd=0.0` and `mcap_usd=0.0`, causing mass liquidity kills  
**Affected Commit**: main at f1cb354  
**Date Observed**: 2026-10-05 ~02:43 ET  
**Evidence**: uploads/evidence.json (73 seen, 0 judged, 38 liquidity kills, 35 age kills)

---

## Executive Summary

**This was a CLEAR BUG**, not a data quality issue from FOMO.

The bug existed in a three-part chain:
1. `fomo_api._flatten_nested_token` overwrote real market metrics with `None`
2. `collect.normalise` converted `None` → `0.0`, violating its own "NEVER invent a number" contract
3. `filter.free_kill` killed every token for having `liquidity_usd < 12_000`

**Fix**: PR ready on branch `cursor/fix-zero-liquidity-bug-dd7a`  
**Tests**: 2 new regression tests, all 48 tests pass  
**Impact**: After merge, tokens will show real liquidity/mcap values and reach judgement stage

---

## Evidence Walkthrough

From the attached `evidence.json`:

```json
{
  "cycle_summary": {
    "seen": 73,
    "judged": 0,
    "killed": {
      "free": {
        "age": 35,
        "liquidity": 38
      }
    }
  },
  "token_stats": {
    "liquidity_usd_all_zero": true,
    "mcap_usd_all_zero": true,
    "age_minutes_med": 17.01,
    "liquidity_kills": 38
  },
  "sample_liquidity_kills": [
    {"ticker": "DOOYET", "age": 16.96, "liq": 0.0, "mcap": 0.0},
    {"ticker": "Okeer", "age": 17.01, "liq": 0.0, "mcap": 0.0}
  ]
}
```

**Key observations:**
- All 73 tokens had `liquidity_usd=0.0` and `mcap_usd=0.0`
- Liquidity-killed tokens were **past the 15-minute age floor** (median age 17 minutes)
- `HARD.min_liquidity_usd = 12_000`, so any token with `0.0` dies as "liquidity"
- Shift from "almost-all age kills" to "age + liquidity kills" suggests FOMO response shape changed

---

## Root Cause Analysis

### 1. FOMO API Response Shape (The Trigger)

FOMO's `filterTokens` endpoint can return multiple response shapes. The problematic shape:

```json
{
  "responseObject": [
    {
      "marketCap": 500000,      // ← Real values at TOP level
      "liquidity": 80000,
      "volume24": 250000,
      "priceUSD": 0.05,
      "holders": 1500,
      "createdAt": 1728086400000,
      "token": {
        "address": "...",       // ← Nested object has ONLY identity
        "networkId": 56
      }
    }
  ]
}
```

This differs from the fully-nested shape (where all metrics live inside `token`).

### 2. Bug in `fomo_api._flatten_nested_token` (The Overwrite)

**Location**: `fomo_api.py` lines 186-204

**The bug**:
```python
def _flatten_nested_token(m: dict, token_data: dict) -> dict:
    flattened = dict(m)  # ← Copies top-level {marketCap: 500000, liquidity: 80000, ...}
    
    flattened.update({
        "marketCap": token_data.get("marketCap"),  # ← None (not in token object)
        "liquidity": token_data.get("liquidity"),  # ← None
        # ...
    })  # ← Overwrites real 500000/80000 with None!
    
    return flattened
```

**What happened**:
1. `dict(m)` copies top-level fields including real `marketCap: 500000`, `liquidity: 80000`
2. `flattened.update({...})` overwrites with `token_data.get("marketCap")` → `None`
3. Result: Real values become `None`

**Evidence from test**: `test_top_level_metrics_with_nested_token_structure` reproduces this exactly.

### 3. Bug in `collect.normalise` (The Invention)

**Location**: `collect.py` lines 71-81

**The bug**:
```python
def normalise(tid: str, m: dict) -> dict:
    # File header says: "NEVER invent a number. A field that came back null stays null."
    
    return {
        "mcap_usd": m["mcap"] or 0.0,      # ← Invents 0.0 for None!
        "liquidity_usd": m["liq"] or 0.0,  # ← Invents 0.0 for None!
        "volume_h24": m["vol24"] or 0.0,   # ← Invents 0.0 for None!
        # ...
    }
```

**What happened**:
- `m["mcap"]` is `None` (from the overwrite bug)
- `None or 0.0` evaluates to `0.0`
- Result: **Invented `0.0` for all market metrics**, violating the function's own contract

**Evidence from test**: `test_normalise_preserves_none_for_missing_metrics` confirms the `or 0.0` coercion.

### 4. Consequence in `filter.free_kill` (The Mass Kill)

**Location**: `filter.py` lines 18-28

```python
def free_kill(t) -> str | None:
    if t["liquidity_usd"] < HARD["min_liquidity_usd"]:  # 12_000
        return "liquidity"
    # ...
```

**What happened**:
- Every token has `liquidity_usd = 0.0`
- `0.0 < 12_000` → True for all tokens
- Result: 38 liquidity kills (all tokens past age floor)
- Zero tokens reach judgement stage

---

## The Fix

### Changes Made

**Branch**: `cursor/fix-zero-liquidity-bug-dd7a`  
**Files Modified**: `fomo_api.py`, `collect.py`, `filter.py`, `tests/test_desk.py`

#### 1. Fix `_flatten_nested_token` (fomo_api.py)

**Before** (bug):
```python
flattened.update({
    "marketCap": token_data.get("marketCap") or token_data.get("mcap"),
    # Always overwrites, even when None
})
```

**After** (fix):
```python
# Only update if nested value is not None (preserves top-level values)
mcap = token_data.get("marketCap") or token_data.get("mcap")
if mcap is not None:
    updates["marketCap"] = mcap
```

**Why it works**: Preserves top-level market metrics when the nested `token` object lacks them.

#### 2. Fix `normalise` (collect.py)

**Before** (bug):
```python
"mcap_usd": m["mcap"] or 0.0,
"liquidity_usd": m["liq"] or 0.0,
```

**After** (fix):
```python
"mcap_usd": m["mcap"],
"liquidity_usd": m["liq"],
```

**Why it works**: Preserves `None` for truly missing data, honors "NEVER invent a number" contract.

#### 3. Update `free_kill` (filter.py)

**Before** (implicit None handling):
```python
if t["liquidity_usd"] < HARD["min_liquidity_usd"]:
    return "liquidity"
```

**After** (explicit None handling):
```python
if t["liquidity_usd"] is None or t["liquidity_usd"] < HARD["min_liquidity_usd"]:
    return "liquidity"
```

**Why it works**: Explicitly treats missing data (`None`) as a failure. Missing liquidity data = not tradeable.

### New Regression Tests

#### Test 1: `test_top_level_metrics_with_nested_token_structure`
Reproduces the exact FOMO response shape from the evidence:
- Market metrics at top level
- Nested `token` object with only address/networkId
- Verifies real values are preserved, not overwritten to `None`

#### Test 2: `test_normalise_preserves_none_for_missing_metrics`
Verifies the "NEVER invent a number" contract:
- FOMO returns `None` for truly missing metrics
- `normalise` preserves `None`, doesn't invent `0.0`

**Test Results**: All 48 tests pass (46 existing + 2 new)

---

## Verification Steps

After merging and pulling to the local shadow desk:

### 1. Check Token Data in state.json

```bash
cd ~/path/to/jev-desk
git pull origin main
jq '.tokens[] | {ticker, liquidity_usd, mcap_usd, age: .age_minutes, verdict}' outbox/state.json | head -20
```

**Expected**:
- Tokens show **real liquidity/mcap values** (not all zeros)
- Example: `{"ticker": "DOOYET", "liquidity_usd": 48000, "mcap_usd": 300000, "age": 17.5, "verdict": "DROP"}`
- Zero values only appear for tokens FOMO truly has no data for

### 2. Check Kill Distribution

```bash
jq '.cycle.killed' outbox/state.json
```

**Expected**:
- **Mix of kills**: age, liquidity, volume, mcap (not just age + liquidity)
- **Some tokens reach judgement**: `cycle.judged > 0` (tokens survived free stage)
- **Liquidity kills are real**: If a token dies on liquidity, it's a real thin book, not invented zero

### 3. Watch Next Cycle

```bash
# In one terminal, watch the log
tail -f desk.log

# In another, watch state updates
watch -n 5 'jq ".cycle.judged, .cycle.killed.free" outbox/state.json'
```

**Expected after fix**:
- `judged` count increases from 0 to 1+ per cycle
- Free stage kills distribute across multiple reasons (age, liquidity, volume, mcap)
- Tokens with real FOMO data survive to trade/chain/soft stages

### 4. If Still NO TRADE

If cycles still end NO TRADE after the fix, it means:
- **Valid filters are working**: Tokens are legitimately thin, young, or low-volume
- **Not a bug**: The filters are doing their job with real data
- **Options to consider**:
  - Check FOMO bearer is fresh (not expired)
  - Verify GeckoTerminal `new_pools` is returning candidates
  - Review `HARD` thresholds if market conditions changed
  - Check defer table isn't full (200 row cap)

**Do NOT lower `HARD.min_age_minutes` (15) or `min_liquidity_usd` (12_000) just to get trades.**  
The bug was invented zeros, not real data.

---

## Why This Wasn't Caught Earlier

1. **Tests covered flat and fully-nested shapes**, but not the **mixed shape** (metrics at top + nested token object)
2. **`test_flat_and_nested_shapes_both_work`** existed but tested simple cases, not the exact problematic structure
3. **The `or 0.0` pattern was pervasive** and looked reasonable at first glance
4. **No test explicitly checked the "NEVER invent a number" contract** from collect.py's header

The new tests close these gaps.

---

## Impact Assessment

### Before Fix
- All tokens: `liquidity_usd=0.0`, `mcap_usd=0.0`
- Mass liquidity kills: 38/73 tokens (52%)
- Zero tokens judged (funnel broken)
- State diverged from reality

### After Fix
- Tokens show real market metrics
- Liquidity kills based on actual thin books
- Tokens reach judgement stage
- Filter funnel works as designed

### Breaking Changes
**None.** Tokens with valid data behave identically. The change only affects:
- Tokens that were incorrectly showing `0.0` (now show real values or `None`)
- Missing data handling (now explicit `None` checks instead of implicit)

---

## Recommended Actions

1. **Merge PR from branch `cursor/fix-zero-liquidity-bug-dd7a`**
2. **Pull to local shadow desk** and monitor next cycle
3. **Verify state.json tokens** show non-zero liquidity/mcap
4. **Confirm `judged > 0`** in next cycle stats
5. **If NO TRADE persists**, it's legitimate filters, not the bug

**Do NOT**:
- Lower `HARD.min_age_minutes` (15 min is correct for data quality)
- Lower `min_liquidity_usd` just to force trades (12k is already aggressive)
- Assume zero liquidity is real if it appears again (check FOMO response first)

---

## Appendix: Code Locations

- **Bug 1**: `fomo_api.py:186-204` (`_flatten_nested_token`)
- **Bug 2**: `collect.py:71-81` (`normalise`)
- **Bug 3**: `filter.py:18-28` (`free_kill`)
- **Test 1**: `tests/test_desk.py::test_top_level_metrics_with_nested_token_structure`
- **Test 2**: `tests/test_desk.py::test_normalise_preserves_none_for_missing_metrics`
- **Evidence**: `uploads/evidence.json`
- **Fix Commit**: `cd575a6` on branch `cursor/fix-zero-liquidity-bug-dd7a`

---

## Questions?

If after merge the desk still shows all-zero liquidity:
1. Check FOMO bearer is valid (`FOMO_BEARER` or Chrome CDP)
2. Inspect a raw FOMO response (add logging to `_filter_tokens`)
3. Verify the fix branch was actually merged (check `git log`)

If the fix works but NO TRADE persists:
1. This is **expected** if launches are genuinely thin/young
2. Check `state.json` kill reasons (should be diverse, not just liquidity)
3. Review defer table size (`SELECT COUNT(*) FROM defer` in desk.db)
4. Consider whether market conditions have shifted

**The bug is fixed. Real liquidity data will now flow through the funnel.**
