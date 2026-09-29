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
        r = self.s.post(f"{FOMO_API}/proxy/filterTokens", json=chunk, timeout=30,
                        headers={"Authorization": f"Bearer {self.token()}"})
        if r.status_code in (401, 403):
            log.info("FOMO bearer expired, refreshing out of Chrome")
            r = self.s.post(f"{FOMO_API}/proxy/filterTokens", json=chunk, timeout=30,
                            headers={"Authorization": f"Bearer {self.token(force=True)}"})
            if r.status_code in (401, 403):
                raise FomoAuthError(f"FOMO {r.status_code}: log into fomo.family again")
        r.raise_for_status()
        body = r.json()
        # be tolerant about the envelope: list of rows, or {id: row}, or {"data": [...]}
        if isinstance(body, dict) and "data" in body:
            body = body["data"]
        if isinstance(body, dict):
            return {k: v for k, v in body.items() if isinstance(v, dict)}
        rows = {}
        for m in body or []:
            tid = m.get("id") or m.get("tokenId") or (
                f"{m.get('address')}:{m.get('networkId') or m.get('netId')}"
                if m.get("address") else None)
            if tid:
                rows[tid] = m
        return rows

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
