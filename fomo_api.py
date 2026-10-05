"""
FOMO client. FOMO has no public API, so the collector reads your own logged-in session.

The bearer is a Privy access token that lives about an hour. Two ways to get it:

  1. Chrome over CDP (the guide's way). Launch the Chrome profile you log into FOMO with:
        open -a "Google Chrome" --args --remote-debugging-port=9222      # mac
        google-chrome --remote-debugging-port=9222                       # linux
     keep a fomo.family tab open, and leave that profile alone. Fomo.token() pulls the
     token out of that tab's localStorage and refreshes it on its own.

  2. FOMO_BEARER env var. Paste the token yourself (DevTools -> Application ->
     Local Storage -> privy:token). Dies hourly, fine for a one-off test.

    POST prod-api.fomo.family/proxy/filterTokens
    body: ["<address>:<netId>", ...]      20 at a time
    -> marketCap, liquidity, volume24, holders, priceUSD,
       change5m / change1 / change4 / change12 / change24, createdAt

netIds: Solana 1399811149 · Robinhood 4663 · BSC 56 · Base 8453 · ETH 1 · Monad 143
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("fomo")

FOMO_API   = os.environ.get("FOMO_API", "https://prod-api.fomo.family")
CDP_URL    = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
BATCH      = 20
TOKEN_TTL  = 50 * 60            # refresh before the ~60 min Privy expiry
# Privy keeps the access token in localStorage under these keys (first hit wins)
PRIVY_KEYS = ("privy:token", "privy:access_token")

# FOMO -> desk field names. normalise() in collect.py reads exactly these.
CHANGE_WINDOWS = {"change5m": 300, "change1": 3600, "change4": 14400,
                  "change12": 43200, "change24": 86400}


class FomoAuthError(RuntimeError):
    pass


class Fomo:
    def __init__(self, bearer: str | None = None):
        self._bearer = bearer or os.environ.get("FOMO_BEARER")
        self._fetched_at = time.time() if self._bearer else 0.0
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json",
                               "Origin": "https://fomo.family",
                               "Referer": "https://fomo.family/",
                               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                             "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"})

    # ---- bearer ----------------------------------------------------------------
    def token(self, force: bool = False) -> str:
        """Current bearer, refreshed out of Chrome when stale. main() calls this every cycle."""
        if self._bearer and not force and time.time() - self._fetched_at < TOKEN_TTL:
            return self._bearer
        fresh = self._from_chrome()
        if fresh:
            self._bearer, self._fetched_at = fresh, time.time()
        elif not self._bearer:
            raise FomoAuthError("no FOMO bearer: log into fomo.family in Chrome started with "
                                "--remote-debugging-port=9222, or set FOMO_BEARER")
        return self._bearer

    def _from_chrome(self) -> str | None:
        """Read the Privy token from the logged-in fomo.family tab via the DevTools protocol."""
        try:
            tabs = requests.get(f"{CDP_URL}/json", timeout=5).json()
        except Exception as e:
            log.debug("CDP not reachable at %s: %s", CDP_URL, e)
            return None
        tab = next((t for t in tabs if t.get("type") == "page" and "fomo.family" in t.get("url", "")),
                   None)
        if not tab:
            log.warning("no fomo.family tab open in the CDP Chrome profile")
            return None
        try:
            import websocket                      # websocket-client
        except ImportError:
            log.error("pip install websocket-client to read the bearer out of Chrome")
            return None
        expr = ("(() => { for (const k of %s) { const v = localStorage.getItem(k); "
                "if (v) return v; } return null; })()" % json.dumps(list(PRIVY_KEYS)))
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=5)
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            res = json.loads(ws.recv())
            ws.close()
        except Exception as e:
            log.warning("CDP evaluate failed: %s", e)
            return None
        raw = ((res.get("result") or {}).get("result") or {}).get("value")
        if not raw:
            return None
        try:
            raw = json.loads(raw)                 # Privy stores it JSON-encoded
        except (TypeError, ValueError):
            pass
        return raw if isinstance(raw, str) and raw.count(".") == 2 else None

    # ---- data ------------------------------------------------------------------
    def tokens(self, ids: list[str]) -> dict[str, dict]:
        """{'<addr>:<netId>': {symbol, mcap, liq, vol24, price, holders, change{sec:pct}, created}}
           Twenty per call. Hundreds of candidates cost you a handful of requests."""
        out = {}
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            rows = self._filter_tokens(chunk)
            for tid, m in rows.items():
                out[tid] = self._row(m)
        return out

    def _filter_tokens(self, chunk: list[str]) -> dict[str, dict]:
        # Retry logic for transient network errors with exponential backoff
        max_retries = 4
        base_delay = 0.5
        for attempt in range(max_retries + 1):
            try:
                r = self.s.post(f"{FOMO_API}/proxy/filterTokens", json=chunk, timeout=30,
                                headers={"Authorization": f"Bearer {self.token()}"})
                if r.status_code in (401, 403):
                    log.info("FOMO bearer expired, refreshing out of Chrome")
                    r = self.s.post(f"{FOMO_API}/proxy/filterTokens", json=chunk, timeout=30,
                                    headers={"Authorization": f"Bearer {self.token(force=True)}"})
                    if r.status_code in (401, 403):
                        raise FomoAuthError(f"FOMO {r.status_code}: log into fomo.family again")
                # Retry on 502 Bad Gateway and similar transient failures
                if r.status_code in (502, 503, 504) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    log.warning("FOMO %d gateway error, retrying in %.1fs (attempt %d/%d)", 
                               r.status_code, delay, attempt + 1, max_retries + 1)
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                break
            except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    log.warning("Connection error on filterTokens: %s, retrying in %.1fs (attempt %d/%d)", 
                               e, delay, attempt + 1, max_retries + 1)
                    # Close adapters to reset connection pool; session will reconnect on next request
                    for adapter in self.s.adapters.values():
                        adapter.close()
                    time.sleep(delay)
                    continue
                else:
                    raise
        
        body = r.json()
        # be tolerant about the envelope: list of rows, or {id: row}, or {"data": [...]} or {"responseObject": [...]}
        if isinstance(body, dict):
            if "responseObject" in body:
                body = body["responseObject"]
            elif "data" in body:
                body = body["data"]
        if isinstance(body, dict):
            return {k: v for k, v in body.items() if isinstance(v, dict)}
        rows = {}
        for m in body or []:
            # Handle nested token structure: live API returns {token: {address, networkId, symbol, ...}}
            token_data = m.get("token") if isinstance(m.get("token"), dict) else None
            if token_data:
                # Nested structure - extract tid from token.address:token.networkId
                tid = (f"{token_data.get('address')}:{token_data.get('networkId') or token_data.get('netId')}"
                       if token_data.get('address') else None)
                if tid:
                    # Flatten the nested structure for _row processing
                    rows[tid] = self._flatten_nested_token(m, token_data)
            else:
                # Flat structure (backward compatibility)
                tid = m.get("id") or m.get("tokenId") or (
                    f"{m.get('address')}:{m.get('networkId') or m.get('netId')}"
                    if m.get("address") else None)
                if tid:
                    rows[tid] = m
        return rows

    @staticmethod
    def _flatten_nested_token(m: dict, token_data: dict) -> dict:
        """Flatten nested {token: {...}} structure to flat dict for _row processing.
        
        Preserve top-level market metrics (marketCap, liquidity, etc.) when they exist,
        only overwriting with nested values if the nested value is not None.
        This handles the case where FOMO returns metrics at top level with a nested
        token object containing only address/networkId.
        """
        flattened = dict(m)  # Copy top-level fields (may include market metrics)
        
        # Map nested token fields to flat structure, but ONLY if non-None
        # This preserves top-level values when nested object lacks them
        updates = {}
        
        # Always update address/networkId from token (these define the token identity)
        if token_data.get("address") is not None:
            updates["address"] = token_data["address"]
        netid = token_data.get("networkId") or token_data.get("netId")
        if netid is not None:
            updates["networkId"] = netid
        
        # For market metrics, only update if present in nested token
        # (preserves top-level values when nested lacks them)
        sym = token_data.get("symbol") or token_data.get("ticker")
        if sym is not None:
            updates["symbol"] = sym
        
        mcap = token_data.get("marketCap") or token_data.get("mcap")
        if mcap is not None:
            updates["marketCap"] = mcap
        
        liq = token_data.get("liquidity") or token_data.get("liq")
        if liq is not None:
            updates["liquidity"] = liq
        
        vol = token_data.get("volume24") or token_data.get("volume24h")
        if vol is not None:
            updates["volume24"] = vol
        
        price = token_data.get("priceUSD") or token_data.get("price")
        if price is not None:
            updates["priceUSD"] = price
        
        holders = token_data.get("holders") or token_data.get("holderCount")
        if holders is not None:
            updates["holders"] = holders
        
        created = token_data.get("createdAt") or token_data.get("created")
        if created is not None:
            updates["createdAt"] = created
        
        flattened.update(updates)
        
        # Copy change fields if present in nested token
        for change_key in CHANGE_WINDOWS.keys():
            if change_key in token_data and token_data[change_key] is not None:
                flattened[change_key] = token_data[change_key]
        
        return flattened

    @staticmethod
    def _row(m: dict) -> dict:
        g = lambda *ks: next((m[k] for k in ks if m.get(k) is not None), None)
        return {"symbol":  g("symbol", "ticker", "name") or "?",
                "mcap":    _f(g("marketCap", "mcap")),
                "liq":     _f(g("liquidity", "liq")),
                "vol24":   _f(g("volume24", "volume24h", "vol24")),
                "price":   _f(g("priceUSD", "price")),
                "holders": _i(g("holders", "holderCount")),
                "change":  {sec: _f(m.get(k)) for k, sec in CHANGE_WINDOWS.items()},
                "created": g("createdAt", "created", "pairCreatedAt")}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
