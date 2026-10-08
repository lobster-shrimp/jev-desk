"""
SCAN and VET — the collector. Jev fetches nothing; this is the code that goes and gets everything.

SCAN = universe() + shortlist()      (GeckoTerminal new_pools -> FOMO filterTokens, 20 per call)
VET  = trade_counts() + dossier()    (DexScreener per token -> GeckoTerminal info + chain RPC)

NEVER invent a number. A field that came back null stays null and the question sees it.
NEVER let null mean fine. Missing data gets its own option and its own consequence.
NEVER pull a dossier for a token stage 2 killed. That is a wasted rate limit slot.
"""
import logging
import os
import time
from collections import deque

import requests

from fomo_api import Fomo                      # Privy bearer out of Chrome over CDP
from secret_utils import safe_err

log = logging.getLogger("collect")


class GTRateLimiter:
    """Rolling-window rate limiter for GeckoTerminal API calls.
    
    Tracks call timestamps in a 60s window. Before each call, blocks until
    fewer than N calls have been made in the last 60s. Prioritizes dossier
    calls over universe pagination via reservation.
    
    Also tracks 429 responses and enforces backoff based on Retry-After header.
    
    Supports separate budgets for universe scan vs dossiers to prevent bunching.
    
    Shared across the entire process, not per-cycle."""
    
    def __init__(self, calls_per_min: int = 8, window_sec: float = 60.0, time_fn=None,
                 backoff_floor_sec: float = 25.0, backoff_max_sec: float = 120.0):
        self.calls_per_min = calls_per_min
        self.window_sec = window_sec
        self.time_fn = time_fn or time.time
        self.calls = deque()  # timestamps of calls in the window
        self.reserved = 0     # slots reserved for dossiers
        self.backoff_until = 0.0  # timestamp until which we must wait due to 429
        self.backoff_floor_sec = backoff_floor_sec  # minimum backoff for 0/missing Retry-After
        self.backoff_max_sec = backoff_max_sec  # cap on backoff duration
        self.consecutive_429s = 0  # track consecutive 429s for adaptive backoff
        self.saturated = False  # window is saturated after a 429
        
        # Universe scan budget tracking (separate from dossier budget)
        # Default to unlimited (999999) so tests without set_universe_budget() don't break
        self.universe_budget = 999999  # set via set_universe_budget() per cycle
        self.universe_calls_used = 0  # reset per cycle
    
    def available(self) -> int:
        """How many GT calls can be made without waiting."""
        self._expire_old_calls()
        return max(0, self.calls_per_min - len(self.calls))
    
    def _expire_old_calls(self):
        """Remove calls that fell outside the rolling window."""
        now = self.time_fn()
        cutoff = now - self.window_sec
        while self.calls and self.calls[0] < cutoff:
            self.calls.popleft()
    
    def record_429(self, retry_after_sec: float | None = None):
        """Record a 429 response and set backoff period.
        
        Args:
            retry_after_sec: Value from Retry-After header, or None if missing/unparseable.
                            When 0, missing, or unparseable, applies backoff_floor_sec.
                            Grows on consecutive 429s, capped at backoff_max_sec.
        """
        now = self.time_fn()
        self.consecutive_429s += 1
        self.saturated = True  # mark window as saturated
        
        # Apply floor backoff when Retry-After is 0, missing, or unparseable
        # Grow on consecutive 429s: floor * (1.5 ** (consecutive - 1)), capped
        if retry_after_sec is None or retry_after_sec <= 0:
            growth_factor = 1.5 ** (self.consecutive_429s - 1)
            backoff_duration = min(self.backoff_floor_sec * growth_factor, self.backoff_max_sec)
            log.info("GT 429 received with Retry-After=%s, applying floor backoff: %.1fs "
                     "(consecutive_429s=%d, floor=%.1fs, max=%.1fs)", 
                     retry_after_sec, backoff_duration, self.consecutive_429s,
                     self.backoff_floor_sec, self.backoff_max_sec)
        else:
            backoff_duration = min(retry_after_sec, self.backoff_max_sec)
            log.info("GT 429 received, backing off for %.1fs (Retry-After: %.1fs, capped at %.1fs)", 
                     backoff_duration, retry_after_sec, self.backoff_max_sec)
        
        self.backoff_until = now + backoff_duration
    
    def wait_if_needed(self, priority: bool = False):
        """Block until a call can be made within rate limits.
        
        If priority=True (dossier), can use reserved slots.
        If priority=False (universe), cannot use reserved slots.
        
        Also waits out any 429 backoff period before checking rate limits.
        Returns immediately if a slot is available and no backoff is active."""
        now = self.time_fn()
        
        # First, wait out any 429 backoff period
        if self.backoff_until > now:
            wait_sec = self.backoff_until - now
            log.info("GT 429 backoff: waiting %.1fs before retry", wait_sec)
            time.sleep(wait_sec)
            now = self.time_fn()
        
        # Then check rolling window rate limits
        self._expire_old_calls()
        
        # Determine effective limit based on priority
        effective_limit = self.calls_per_min if priority else (self.calls_per_min - self.reserved)
        
        if len(self.calls) < effective_limit:
            return
        
        # Wait until the oldest call expires
        wait_until = self.calls[0] + self.window_sec
        wait_sec = max(0, wait_until - now)
        if wait_sec > 0:
            log.info("GT pace: waited %.1fs", wait_sec)
            time.sleep(wait_sec)
            self._expire_old_calls()
    
    def record_success(self):
        """Record a successful API call. Clears saturated flag and resets consecutive 429 counter."""
        self.saturated = False
        self.consecutive_429s = 0
    
    def spend(self, cost: int = 1, priority: bool = False) -> bool:
        """Try to spend `cost` slots. Returns True if budget available, False otherwise.
        
        Does NOT block - use wait_if_needed() before calling this if you want blocking behavior.
        This is for backwards compatibility with tests that check budget without waiting."""
        self._expire_old_calls()
        effective_limit = self.calls_per_min if priority else (self.calls_per_min - self.reserved)
        
        if len(self.calls) + cost <= effective_limit:
            for _ in range(cost):
                self.calls.append(self.time_fn())
            log.debug("GT budget: spent %d, %d available", cost, self.available())
            return True
        log.warning("GT budget exhausted: tried to spend %d, only %d available", cost, self.available())
        return False
    
    def reserve(self, amount: int) -> int:
        """Reserve `amount` slots for priority use (e.g. dossiers). Returns actual reserved."""
        reserved = min(amount, self.calls_per_min)
        self.reserved = reserved
        if reserved > 0:
            log.info("GT budget: reserved %d for dossiers, %d remain for universe", 
                     reserved, self.calls_per_min - reserved)
        return reserved
    
    def set_universe_budget(self, budget: int):
        """Set the universe scan budget for this cycle and reset usage counter.
        
        Called at cycle start to allocate a fixed number of calls for universe scan.
        When exhausted, universe scan stops paging instead of waiting."""
        self.universe_budget = budget
        self.universe_calls_used = 0
        log.info("GT universe budget: allocated %d calls for this cycle", budget)
    
    def spend_universe(self, cost: int = 1) -> bool:
        """Try to spend `cost` from the universe budget.
        
        Returns True if spent, False if universe budget exhausted.
        Does NOT block or wait - universe scan should stop when False.
        Does NOT check rolling window budget - that's checked separately."""
        if self.universe_calls_used + cost > self.universe_budget:
            log.info("GT universe budget exhausted: tried to spend %d, used %d/%d", 
                     cost, self.universe_calls_used, self.universe_budget)
            return False
        self.universe_calls_used += cost
        log.debug("GT universe budget: spent %d, used %d/%d", 
                  cost, self.universe_calls_used, self.universe_budget)
        return True

GT  = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/token-pairs/v1"  # New documented endpoint
SOL_RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
      "Accept": "application/json"}

# the three chains the desk trades, plus Base which shares the BSC question set
GT_NET   = {1399811149: "solana", 4663: "robinhood", 56: "bsc", 8453: "base"}
FOMO_NET = {v: k for k, v in GT_NET.items()}

# Map network IDs to DexScreener chain IDs
DEX_CHAIN_ID = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood"}

# Special marker for dossier failures that should trigger retry
class DossierRetryNeeded(Exception):
    """Dossier failed due to rate limit / transient error and should be retried."""
    pass


def age_minutes(created) -> float:
    """createdAt comes back as epoch seconds or milliseconds depending on the row."""
    if not created:
        return 0.0
    c = float(created)
    if c > 1e11:                                # milliseconds
        c /= 1000
    return max(0.0, (time.time() - c) / 60)


def _gt_call_with_retry(url: str, params: dict, limiter: GTRateLimiter | None, 
                        priority: bool = False, retry_on_429: bool = False,
                        is_universe: bool = False) -> tuple[dict | None, bool]:
    """Make a rate-limited GT API call with optional 429 retry.
    
    Returns (response_json, should_continue):
      - (None, False): hard error, stop this feed
      - (None, True): budget exhausted, skip but continue other feeds
      - (data, True): success
    
    If retry_on_429=True (trending feeds), retries once on 429 with backoff.
    If is_universe=True, checks universe budget before spending rolling window budget."""
    # Check universe budget first if this is a universe call
    if is_universe and limiter and not limiter.spend_universe(1):
        # Universe budget exhausted, stop universe scan
        return None, True
    
    if limiter:
        # Wait BEFORE spending to avoid burst after backoff
        limiter.wait_if_needed(priority=priority)
        if not limiter.spend(1, priority=priority):
            return None, True  # budget exhausted, but continue other feeds
    
    try:
        resp = requests.get(url, params=params, headers=UA, timeout=20)
        if resp.status_code == 429:
            # Extract Retry-After and record with limiter
            retry_after_sec = None
            if resp.headers:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        retry_after_sec = float(retry_after)
                    except (ValueError, TypeError):
                        retry_after_sec = None
            
            if limiter:
                limiter.record_429(retry_after_sec)
            
            if not retry_on_429:
                return None, False  # non-retryable feed, stop
            
            # Retry once after backoff (wait_if_needed will honor the recorded backoff)
            if limiter:
                limiter.wait_if_needed(priority=priority)
            else:
                # No limiter, manual backoff
                backoff = retry_after_sec if retry_after_sec is not None else 60.0
                log.info("GT 429 (no limiter), backing off %.1fs before retry", backoff)
                time.sleep(backoff)
            
            # Retry
            resp = requests.get(url, params=params, headers=UA, timeout=20)
            if resp.status_code == 429:
                return None, False  # still 429 after retry, give up
        
        if limiter:
            limiter.record_success()  # clear saturated flag on success
        return resp.json(), True
    except Exception as e:
        log.warning("GT call failed: %s", safe_err(e))
        return None, False


def universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=True, 
             limiter: GTRateLimiter | None = None) -> tuple[list[str], dict]:
    """Where the whole thing starts. Fresh pools per chain -> (['<addr>:<netId>', ...], {tid: gt_txns}).
       Costs one GeckoTerminal slot per chain per page, so keep pages small.
       
       robinhood is capped at 1 page to avoid 429 rate limits every cycle.
       Other networks fetch 2 pages from new_pools.
       
       When include_trending=True, fetches 1 page of trending_pools per network BEFORE
       new_pools pagination. This prioritizes the high-yield trending feed.
       
       If limiter is provided, paces calls to stay under rate limits. Trending feeds
       retry once on 429 with Retry-After backoff; new_pools pages skip on 429.
       
       Universe scan respects its own budget (set via limiter.set_universe_budget()).
       When universe budget exhausted, stops paging and uses what it has.
       
       Also returns gt_txns_cache: {tid: {"h1": {"buys": N, "sells": N}, "h6": {...}, "h24": {...}}}
       for fallback when DexScreener is degraded."""
    ids, seen = [], set()
    gt_txns_cache = {}  # {tid: transaction data from GT}
    pages_fetched = 0  # Track total pages for logging
    
    # Phase 1: trending_pools (1 page per network, before new_pools)
    if include_trending:
        for net in nets:
            r, should_continue = _gt_call_with_retry(
                f"{GT}/networks/{net}/trending_pools",
                {"page": 1},
                limiter,
                priority=False,
                retry_on_429=True,  # trending feeds retry once
                is_universe=True
            )
            if r is None:
                if should_continue:
                    # Universe budget exhausted
                    log.info("universe scan budget exhausted after %d pages (during %s trending_pools)", 
                             pages_fetched, net)
                    return ids, gt_txns_cache
                else:
                    log.warning("GeckoTerminal 429 on %s trending_pools, skipping trending for this network", net)
                    continue
            
            pages_fetched += 1
            
            for pool in r.get("data", []):
                base = ((pool.get("relationships") or {}).get("base_token") or {})
                gid  = (base.get("data") or {}).get("id")      # 'solana_<addr>'
                if not gid or "_" not in gid:
                    continue
                addr = gid.split("_", 1)[1]
                tid  = f"{addr}:{FOMO_NET[net]}"
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
                
                # Cache GT transaction data for DexScreener fallback
                # Prefer highest liquidity pool if multiple pools for same token
                attrs = pool.get("attributes", {})
                txns = attrs.get("transactions", {})
                if txns:
                    # GT API returns reserve_in_usd as a string
                    reserve_str = attrs.get("reserve_in_usd")
                    try:
                        liq_usd = float(reserve_str) if reserve_str else 0.0
                    except (ValueError, TypeError):
                        liq_usd = 0.0
                    # Only cache if no existing data OR this pool has higher liquidity
                    existing = gt_txns_cache.get(tid)
                    if not existing or liq_usd > existing.get("_liq_usd", 0):
                        gt_txns_cache[tid] = {
                            "h1": txns.get("h1", {}),
                            "h6": txns.get("h6", {}),
                            "h24": txns.get("h24", {}),
                            "_liq_usd": liq_usd  # track for comparison
                        }
    
    # Phase 2: new_pools (existing behavior)
    for net in nets:
        net_pages = 1 if net == "robinhood" else pages
        for page in range(1, net_pages + 1):
            r, should_continue = _gt_call_with_retry(
                f"{GT}/networks/{net}/new_pools",
                {"page": page},
                limiter,
                priority=False,
                retry_on_429=False,  # new_pools doesn't retry
                is_universe=True
            )
            if r is None:
                if should_continue:
                    # Universe budget exhausted
                    log.info("universe scan budget exhausted after %d pages (during %s new_pools page %d)", 
                             pages_fetched, net, page)
                    return ids, gt_txns_cache
                else:
                    log.warning("GeckoTerminal 429 on %s new_pools page %s, stopping pagination for this network", net, page)
                break  # stop paging this network
            
            pages_fetched += 1
            
            for pool in r.get("data", []):
                base = ((pool.get("relationships") or {}).get("base_token") or {})
                gid  = (base.get("data") or {}).get("id")      # 'solana_<addr>'
                if not gid or "_" not in gid:
                    continue
                addr = gid.split("_", 1)[1]
                tid  = f"{addr}:{FOMO_NET[net]}"
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
                
                # Cache GT transaction data for DexScreener fallback
                # Prefer highest liquidity pool if multiple pools for same token
                attrs = pool.get("attributes", {})
                txns = attrs.get("transactions", {})
                if txns:
                    # GT API returns reserve_in_usd as a string
                    reserve_str = attrs.get("reserve_in_usd")
                    try:
                        liq_usd = float(reserve_str) if reserve_str else 0.0
                    except (ValueError, TypeError):
                        liq_usd = 0.0
                    # Only cache if no existing data OR this pool has higher liquidity
                    existing = gt_txns_cache.get(tid)
                    if not existing or liq_usd > existing.get("_liq_usd", 0):
                        gt_txns_cache[tid] = {
                            "h1": txns.get("h1", {}),
                            "h6": txns.get("h6", {}),
                            "h24": txns.get("h24", {}),
                            "_liq_usd": liq_usd  # track for comparison
                        }
    
    log.info("universe scan completed: fetched %d pages, found %d tokens", pages_fetched, len(ids))
    return ids, gt_txns_cache


def normalise(tid: str, m: dict) -> dict:
    """FOMO's field names become the desk's field names, once, here.
       Every file downstream reads these names and only these.
       
       NEVER invent a number. Preserve None for truly missing data.
       The `or 0.0` pattern is REMOVED: missing market metrics stay None
       so downstream filters can distinguish 'no data' from 'zero liquidity'.
       """
    addr, net = tid.split(":")
    return {"addr": addr, "net": int(net), "tid": tid, "ticker": m["symbol"],
            "mcap_usd": m["mcap"], "liquidity_usd": m["liq"],
            "volume_h24": m["vol24"], "price_usd": m["price"],
            "holder_count": m["holders"],
            "change": {"5m": m["change"].get(300), "1h": m["change"].get(3600),
                       "4h": m["change"].get(14400), "24h": m["change"].get(86400)},
            "age_minutes": age_minutes(m["created"])}


def shortlist(fomo: Fomo, ids: list[str]) -> list[dict]:
    """Pass one over everything FOMO knows. No network beyond FOMO itself:
       one call per twenty tokens, and not a single request per token."""
    out = []
    for tid, m in fomo.tokens(ids).items():             # 20 per call
        try:
            t = normalise(tid, m)
        except (KeyError, TypeError) as e:
            log.debug("skip %s, malformed row: %s", tid, e)
            continue
        if t["net"] in GT_NET:
            out.append(t)
    # turnover ranks the queue. It orders work, it does not decide anything.
    # Missing data (None) gets lowest priority (treat as turnover = 0).
    def turnover(t):
        vol, mcap = t["volume_h24"], t["mcap_usd"]
        if vol is None or mcap is None:
            return 0.0  # lowest priority for missing data
        return vol / max(mcap, 1)
    out.sort(key=turnover, reverse=True)
    return out


_EMPTY_TRADES = {"buys_h1": None, "sells_h1": None, "buys_h6": None, "sells_h6": None,
                 "trades_h24": None}


# Known liquid tokens for canary checks (Solana addresses)
CANARY_TOKENS = {
    1399811149: "So11111111111111111111111111111111111111112",  # Wrapped SOL
}


def dex_canary_check(net: int) -> bool:
    """Check if DexScreener is healthy by querying a known liquid token.
    
    Returns True if canary is healthy (has pairs), False if degraded (empty/error).
    """
    canary_addr = CANARY_TOKENS.get(net)
    chain_id = DEX_CHAIN_ID.get(net)
    
    if not canary_addr or not chain_id:
        log.debug("No canary token/chain configured for net %s, skipping canary check", net)
        return True  # no canary available, assume healthy
    
    try:
        resp = requests.get(f"{DEX}/{chain_id}/{canary_addr}", headers=UA, timeout=20)
        if resp.status_code != 200:
            log.warning("DexScreener canary HTTP %d for net %s", resp.status_code, net)
            return False
        
        try:
            data = resp.json()
        except Exception:
            log.warning("DexScreener canary JSON parse error for net %s", net)
            return False
        
        # New endpoint returns array directly - null or empty = degraded
        if data is None or (isinstance(data, list) and len(data) == 0):
            log.warning("DexScreener canary empty/null for net %s (known liquid token %s has no pairs)", 
                        net, canary_addr)
            return False
        
        log.info("DexScreener canary healthy for net %s (%d pairs)", net, len(data) if isinstance(data, list) else 0)
        return True
        
    except Exception as e:
        log.warning("DexScreener canary check failed for net %s: %s", net, e)
        return False


def trade_counts(t: dict, gt_txns_cache: dict = None) -> tuple[dict, str]:
    """buys and sells per window. FOMO does not return them, DexScreener does.
       Called ONLY for tokens that already cleared the free checks. One per token,
       so this runs on tens, never on the whole universe.
       
       When DexScreener fails/degrades, falls back to GT transaction data from universe scan.
       
       Returns (trade_data, status):
         status: 'ok' (got data), 'empty' (no pairs found), 'error' (HTTP/network error),
                 'gt_fallback' (used GT data due to Dex error/empty)
    """
    gt_txns_cache = gt_txns_cache or {}
    chain_id = DEX_CHAIN_ID.get(t["net"])
    
    if not chain_id:
        log.warning("No DexScreener chain_id for net %s, using GT fallback", t["net"])
        return _try_gt_fallback(t, gt_txns_cache, genuine_empty=False)
    
    try:
        resp = requests.get(f"{DEX}/{chain_id}/{t['addr']}", headers=UA, timeout=20)
        
        # Check HTTP status codes - only 200 is ok
        if resp.status_code != 200:
            log.warning("DexScreener HTTP %d for %s (%s)", resp.status_code, t["ticker"], t["addr"])
            # Try GT fallback on HTTP errors
            return _try_gt_fallback(t, gt_txns_cache)
        
        try:
            data = resp.json()
        except Exception as e:
            log.warning("DexScreener JSON parse error for %s (%s): %s", t["ticker"], t["addr"], e)
            return _try_gt_fallback(t, gt_txns_cache)
        
        # New endpoint returns array directly (not wrapped in {"pairs": ...})
        # null or [] = genuinely no pairs found (no_pair)
        if data is None or (isinstance(data, list) and len(data) == 0):
            # Genuinely empty - try GT fallback first
            log.info("DexScreener empty/null for %s, trying GT fallback", t["ticker"])
            return _try_gt_fallback(t, gt_txns_cache, genuine_empty=True)
        
        if not isinstance(data, list):
            log.warning("DexScreener unexpected response type for %s: %s", t["ticker"], type(data))
            return _try_gt_fallback(t, gt_txns_cache)
        
    except requests.exceptions.Timeout:
        log.warning("DexScreener timeout for %s (%s)", t["ticker"], t["addr"])
        return _try_gt_fallback(t, gt_txns_cache)
    except requests.exceptions.RequestException as e:
        log.warning("DexScreener network error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)
    except Exception as e:
        log.warning("DexScreener unexpected error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)
    
    # Parse the pairs data - pick highest liquidity pair
    try:
        if not data:  # Empty list already handled above, but be defensive
            return _try_gt_fallback(t, gt_txns_cache, genuine_empty=True)
        
        # Sort by liquidity, pick highest
        pairs_with_liq = [(p, (p.get("liquidity") or {}).get("usd") or 0) for p in data]
        best_pair, best_liq = max(pairs_with_liq, key=lambda x: x[1])
        
        txns = best_pair.get("txns") or {}
        h1 = txns.get("h1") or {}
        h6 = txns.get("h6") or {}
        h24 = txns.get("h24") or {}
        
        buys_h24 = h24.get("buys") or 0
        sells_h24 = h24.get("sells") or 0
        
        return {
            "buys_h1": h1.get("buys"), 
            "sells_h1": h1.get("sells"),
            "buys_h6": h6.get("buys"), 
            "sells_h6": h6.get("sells"),
            "trades_h24": buys_h24 + sells_h24  # Always a number, never None
        }, 'ok'
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        log.warning("DexScreener data parse error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)


def _try_gt_fallback(t: dict, gt_txns_cache: dict, genuine_empty: bool = False) -> tuple[dict, str]:
    """Try to use GT transaction data as fallback for DexScreener.
    
    Args:
        t: token dict
        gt_txns_cache: {tid: {"h1": {"buys": N, "sells": N}, ...}} from universe()
        genuine_empty: True if Dex returned empty/null (not error), False for errors
    
    Returns (trade_data, status) where status is 'gt_fallback', 'empty', or 'error'
    """
    tid = t.get("tid")
    gt_txns = gt_txns_cache.get(tid) if tid else None
    
    if gt_txns and gt_txns.get("h24"):
        # GT has data, use it
        h1 = gt_txns.get("h1", {})
        h6 = gt_txns.get("h6", {})
        h24 = gt_txns.get("h24", {})
        
        buys_h24 = h24.get("buys") or 0
        sells_h24 = h24.get("sells") or 0
        
        log.info("Using GT fallback for %s: h24 buys=%s sells=%s (DexScreener %s)", 
                 t["ticker"], buys_h24, sells_h24, "empty" if genuine_empty else "error")
        
        return {
            "buys_h1": h1.get("buys"),
            "sells_h1": h1.get("sells"),
            "buys_h6": h6.get("buys"),
            "sells_h6": h6.get("sells"),
            "trades_h24": buys_h24 + sells_h24  # Always a number, even if 0
        }, 'gt_fallback'
    
    # No GT fallback available
    if genuine_empty:
        return dict(_EMPTY_TRADES), 'empty'
    else:
        return dict(_EMPTY_TRADES), 'error'


def _normalize_authority(raw_value) -> tuple[bool | None, str | None]:
    """Normalize GT authority field to tri-state: True=open, False=revoked, None=unknown.
    
    Returns (normalized, raw) where:
    - True means authority is set (open/dangerous)
    - False means authority is revoked or explicitly disabled
    - None means unknown/missing data
    - raw is the original value for debugging
    """
    if raw_value is None:
        return (None, None)
    if isinstance(raw_value, bool):
        return (raw_value, str(raw_value))
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        # Revoked/disabled: 'no', 'false', '', 'null', 'none', '0'
        if normalized in ('no', 'false', '', 'null', 'none', '0'):
            return (False, raw_value)
        # Open/set: 'yes', 'true', or any base58 address (non-empty after stripping)
        if normalized in ('yes', 'true') or (normalized and normalized not in ('no', 'false', 'null', 'none', '0')):
            return (True, raw_value)
        return (None, raw_value)
    # Unexpected type
    return (None, str(raw_value))


def dossier(t: dict, limiter: GTRateLimiter | None = None) -> dict:
    """One GT call per token. Fills what the chain actually has, null where it does not.
    
    If limiter is provided and budget is exhausted, raises DossierRetryNeeded.
    On GT 429, records the backoff with the limiter and raises DossierRetryNeeded."""
    if limiter:
        # Wait for any 429 backoff BEFORE spending a slot (order matters!)
        # This ensures young in-cycle retries honor the backoff period set by record_429()
        wait_start = limiter.time_fn()
        limiter.wait_if_needed(priority=True)
        wait_duration = limiter.time_fn() - wait_start
        
        # Log actual wait duration if we waited
        if wait_duration >= 0.1:  # only log waits >= 100ms
            log.info("dossier for %s waited %.1fs for GT rate limit/429 backoff", 
                     t["ticker"], wait_duration)
        
        if not limiter.spend(1, priority=True):
            log.warning("GT budget exhausted, dossier for %s cannot run this cycle", t["ticker"])
            raise DossierRetryNeeded(f"GT budget exhausted for {t['ticker']}")
    
    net = GT_NET[t["net"]]
    resp = requests.get(f"{GT}/networks/{net}/tokens/{t['addr']}/info", headers=UA, timeout=20)
    if resp.status_code == 429:
        # Extract Retry-After header if present
        retry_after_sec = None
        if hasattr(resp, 'headers') and resp.headers:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and isinstance(retry_after, (str, int, float)):
                try:
                    retry_after_sec = float(retry_after)
                except (ValueError, TypeError):
                    retry_after_sec = None
        
        # Record the 429 with the limiter so it enforces backoff
        if limiter:
            limiter.record_429(retry_after_sec)
        
        log.warning("GeckoTerminal 429 on dossier for %s (Retry-After: %s), will retry next cycle", 
                    t["ticker"], retry_after_sec if retry_after_sec is not None else "missing/0")
        raise DossierRetryNeeded(f"GT 429 for {t['ticker']}")
    
    if limiter:
        limiter.record_success()  # clear saturated flag on success
    
    a = resp.json()["data"]["attributes"]

    # Normalize authority fields for Solana tokens
    mint_auth_normalized, mint_auth_raw = _normalize_authority(a.get("mint_authority"))
    freeze_auth_normalized, freeze_auth_raw = _normalize_authority(a.get("freeze_authority"))

    holders = a.get("holders") or {}
    d = {**t, "chain": net,
         # GT first, FOMO as the fallback. On Robinhood GT is null and FOMO is all you get.
         "holder_count": holders.get("count") or t["holder_count"],
         "top_10_percent": (holders.get("distribution_percentage") or {}).get("top_10"),
         "developer_holding_percentage": a.get("developer_holding_percentage"),
         "gt_score_details": a.get("gt_score_details"),
         "is_honeypot": a.get("is_honeypot"),
         "mint_authority": mint_auth_normalized,
         "mint_authority_raw": mint_auth_raw,
         "freeze_authority": freeze_auth_normalized,
         "freeze_authority_raw": freeze_auth_raw,
         "description": a.get("description"),
         "x_handle": clean_handle(a.get("twitter_handle"))}

    # Solana only: exact top wallet share, free, off the public RPC
    if t["net"] == 1399811149:
        try:
            top_wallet, rpc_ok, rpc_error = sol_top_wallet(t["addr"])
            d["top_wallet_percent"] = top_wallet
            d["rpc_ok"] = rpc_ok
            d["rpc_error"] = rpc_error
            if rpc_error:
                log.warning("solana rpc failed for %s: %s", t["ticker"], rpc_error)
        except Exception as e:
            safe_msg = safe_err(e)
            log.warning("solana rpc failed for %s: %s", t["ticker"], safe_msg)
            d["top_wallet_percent"] = None
            d["rpc_ok"] = False
            d["rpc_error"] = safe_msg

    return d


def clean_handle(h):
    """GT returned 'LuffyX100X/status/2102659581109272876' on a Robinhood token.
       Take the first path segment, or treat the account as missing."""
    if not h:
        return None
    h = h.strip().lstrip("@").split("?")[0].split("/")[0]
    return h if h and h.replace("_", "").isalnum() and len(h) <= 15 else None


def sol_top_wallet(mint: str) -> tuple[float | None, bool, str | None]:
    """Query Solana RPC for top wallet concentration.
    
    Returns (top_wallet_percent, rpc_ok, rpc_error):
      - top_wallet_percent: float or None
      - rpc_ok: True on success, False on failure
      - rpc_error: error description or None
    """
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    
    def q(m, p, retry_on_429=False):
        """Make one RPC call. Returns (result, error_description)."""
        try:
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                timeout=20
            )
            data = resp.json()
            
            if "error" in data:
                error_msg = data["error"]
                if isinstance(error_msg, dict):
                    code = error_msg.get("code")
                    message = error_msg.get("message", "unknown error")
                    error_desc = f"{message} (code {code})" if code else message
                else:
                    error_desc = str(error_msg)
                
                if resp.status_code == 429 and retry_on_429:
                    log.info("Solana RPC 429 on %s for %s, retrying once", m, mint)
                    time.sleep(2)
                    resp_retry = requests.post(
                        rpc_url,
                        json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                        timeout=20
                    )
                    data_retry = resp_retry.json()
                    if "error" in data_retry:
                        return None, error_desc
                    if "result" not in data_retry:
                        return None, "missing result field"
                    return data_retry["result"], None
                
                return None, error_desc
            
            if "result" not in data:
                return None, "missing result field"
            
            return data["result"], None
            
        except Exception as e:
            return None, safe_err(e)
    
    supply_result, supply_error = q("getTokenSupply", [mint])
    if supply_error:
        return None, False, f"getTokenSupply: {supply_error}"
    
    try:
        supply = float(supply_result["value"]["amount"])
    except (TypeError, KeyError, ValueError) as e:
        return None, False, f"getTokenSupply parse: {e}"
    
    top_result, top_error = q("getTokenLargestAccounts", [mint], retry_on_429=True)
    if top_error:
        return None, False, f"getTokenLargestAccounts: {top_error}"
    
    try:
        top = top_result["value"]
        if not supply or not top:
            return None, True, None
        return float(top[0]["amount"]) / supply, True, None
    except (TypeError, KeyError, ValueError, IndexError) as e:
        return None, False, f"getTokenLargestAccounts parse: {e}"


def social_state(d: dict) -> dict:
    """What SOCIAL hands the judge. The X block is filled by the bot's X plugin."""
    return {"x_account": d["x_account"],                 # collected by SOCIAL, not here
            "token": {"ticker": d["ticker"], "narrative": d.get("description")}}
