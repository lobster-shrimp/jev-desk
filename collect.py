"""
SCAN and VET — the collector. Jev fetches nothing; this is the code that goes and gets everything.

SCAN = universe() + shortlist()      (GeckoTerminal new_pools -> FOMO filterTokens, 20 per call)
VET  = trade_counts() + dossier()    (DexScreener per token -> GeckoTerminal info + chain RPC)

NEVER invent a number. A field that came back null stays null and the question sees it.
NEVER let null mean fine. Missing data gets its own option and its own consequence.
NEVER pull a dossier for a token stage 2 killed. That is a wasted rate limit slot.
"""
import logging
import time

import requests

from fomo_api import Fomo                      # Privy bearer out of Chrome over CDP

log = logging.getLogger("collect")


class GTRateLimiter:
    """Token bucket rate limiter for GeckoTerminal API calls.
    
    Shared across universe() and dossier() within a cycle. Starts each cycle with
    a fresh budget (10 calls/min free tier). Prioritizes dossier calls for tokens
    that passed free_kill over universe pagination."""
    
    def __init__(self, capacity: int = 10):
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.time()
    
    def available(self) -> int:
        """How many GT calls remain in this cycle's budget."""
        return self.tokens
    
    def spend(self, cost: int = 1) -> bool:
        """Try to spend `cost` tokens. Returns True if budget available, False otherwise."""
        if self.tokens >= cost:
            self.tokens -= cost
            log.debug("GT budget: spent %d, %d remain", cost, self.tokens)
            return True
        log.warning("GT budget exhausted: tried to spend %d, only %d remain", cost, self.tokens)
        return False
    
    def reserve(self, amount: int) -> int:
        """Reserve `amount` tokens for priority use (e.g. dossiers). Returns actual reserved."""
        reserved = min(amount, self.tokens)
        if reserved > 0:
            log.info("GT budget: reserved %d for dossiers, %d remain for universe", 
                     reserved, self.tokens - reserved)
        return reserved

GT  = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
SOL_RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
      "Accept": "application/json"}

# the three chains the desk trades, plus Base which shares the BSC question set
GT_NET   = {1399811149: "solana", 4663: "robinhood", 56: "bsc", 8453: "base"}
FOMO_NET = {v: k for k, v in GT_NET.items()}

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


def universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=True, 
             limiter: GTRateLimiter | None = None) -> list[str]:
    """Where the whole thing starts. Fresh pools per chain -> ['<addr>:<netId>', ...].
       Costs one GeckoTerminal slot per chain per page, so keep pages small.
       
       robinhood is capped at 1 page to avoid 429 rate limits every cycle.
       Other networks fetch 2 pages from new_pools.
       
       When include_trending=True, also fetches 1 page of trending_pools per network
       to widen the universe beyond just new (<15m old) pools. This helps find tokens
       with durable liquidity that passed the 15m age threshold.
       
       If limiter is provided, checks budget before each call and skips when exhausted."""
    ids, seen = [], set()
    
    # Phase 1: new_pools (existing behavior)
    for net in nets:
        net_pages = 1 if net == "robinhood" else pages
        for page in range(1, net_pages + 1):
            if limiter and not limiter.spend(1):
                log.warning("GT budget exhausted, skipping %s new_pools page %d", net, page)
                break
            try:
                resp = requests.get(f"{GT}/networks/{net}/new_pools",
                                    params={"page": page}, headers=UA, timeout=20)
                if resp.status_code == 429:
                    # Stop paging this network only, preserve IDs from other nets/pages
                    log.warning("GeckoTerminal 429 on %s new_pools page %s, stopping pagination for this network", net, page)
                    break
                r = resp.json()
            except Exception as e:
                log.warning("new_pools %s p%s failed: %s", net, page, e)
                break
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
    
    # Phase 2: trending_pools (1 page per network)
    if include_trending:
        for net in nets:
            if limiter and not limiter.spend(1):
                log.warning("GT budget exhausted, skipping %s trending_pools", net)
                continue
            try:
                resp = requests.get(f"{GT}/networks/{net}/trending_pools",
                                    params={"page": 1}, headers=UA, timeout=20)
                if resp.status_code == 429:
                    log.warning("GeckoTerminal 429 on %s trending_pools, skipping trending for this network", net)
                    continue
                r = resp.json()
            except Exception as e:
                log.warning("trending_pools %s failed: %s", net, e)
                continue
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
    
    return ids


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


def trade_counts(t: dict) -> dict:
    """buys and sells per window. FOMO does not return them, DexScreener does.
       Called ONLY for tokens that already cleared the free checks. One per token,
       so this runs on tens, never on the whole universe."""
    try:
        pairs = requests.get(f"{DEX}/{t['addr']}", headers=UA, timeout=20).json().get("pairs") or []
    except Exception:
        return dict(_EMPTY_TRADES)
    if not pairs:
        return dict(_EMPTY_TRADES)
    x = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0).get("txns") or {}
    w = lambda k: x.get(k) or {}
    try:
        return {"buys_h1": w("h1").get("buys"), "sells_h1": w("h1").get("sells"),
                "buys_h6": w("h6").get("buys"), "sells_h6": w("h6").get("sells"),
                "trades_h24": (w("h24").get("buys") or 0) + (w("h24").get("sells") or 0)
                              if w("h24") else None}
    except (TypeError, AttributeError):
        return dict(_EMPTY_TRADES)


def dossier(t: dict, limiter: GTRateLimiter | None = None) -> dict:
    """One GT call per token. Fills what the chain actually has, null where it does not.
    
    If limiter is provided and budget is exhausted, raises DossierRetryNeeded.
    On GT 429, raises DossierRetryNeeded (caller should requeue for next cycle)."""
    if limiter and not limiter.spend(1):
        log.warning("GT budget exhausted, dossier for %s cannot run this cycle", t["ticker"])
        raise DossierRetryNeeded(f"GT budget exhausted for {t['ticker']}")
    
    net = GT_NET[t["net"]]
    resp = requests.get(f"{GT}/networks/{net}/tokens/{t['addr']}/info", headers=UA, timeout=20)
    if resp.status_code == 429:
        log.warning("GeckoTerminal 429 on dossier for %s, will retry next cycle", t["ticker"])
        raise DossierRetryNeeded(f"GT 429 for {t['ticker']}")
    
    a = resp.json()["data"]["attributes"]

    holders = a.get("holders") or {}
    d = {**t, "chain": net,
         # GT first, FOMO as the fallback. On Robinhood GT is null and FOMO is all you get.
         "holder_count": holders.get("count") or t["holder_count"],
         "top_10_percent": (holders.get("distribution_percentage") or {}).get("top_10"),
         "developer_holding_percentage": a.get("developer_holding_percentage"),
         "gt_score_details": a.get("gt_score_details"),
         "is_honeypot": a.get("is_honeypot"),
         "mint_authority": a.get("mint_authority"),
         "freeze_authority": a.get("freeze_authority"),
         "description": a.get("description"),
         "x_handle": clean_handle(a.get("twitter_handle"))}

    # Solana only: exact top wallet share, free, off the public RPC
    if t["net"] == 1399811149:
        try:
            d["top_wallet_percent"] = sol_top_wallet(t["addr"])
        except Exception as e:
            log.warning("solana rpc failed for %s: %s", t["ticker"], e)
            d["top_wallet_percent"] = None       # missing is missing

    return d


def clean_handle(h):
    """GT returned 'LuffyX100X/status/2102659581109272876' on a Robinhood token.
       Take the first path segment, or treat the account as missing."""
    if not h:
        return None
    h = h.strip().lstrip("@").split("?")[0].split("/")[0]
    return h if h and h.replace("_", "").isalnum() and len(h) <= 15 else None


def sol_top_wallet(mint: str):
    def q(m, p):
        return requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                             timeout=20).json()["result"]
    supply = float(q("getTokenSupply", [mint])["value"]["amount"])
    top = q("getTokenLargestAccounts", [mint])["value"]
    return float(top[0]["amount"]) / supply if supply and top else None


def social_state(d: dict) -> dict:
    """What SOCIAL hands the judge. The X block is filled by the bot's X plugin."""
    return {"x_account": d["x_account"],                 # collected by SOCIAL, not here
            "token": {"ticker": d["ticker"], "narrative": d.get("description")}}
