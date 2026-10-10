"""
THE FILTER — the order the kills fire in. The order is the whole point.

free_kill  touches no network, runs on hundreds.
trade_kill costs one DexScreener call, runs on tens.
concentration_kill is the cheap chain subset (top_wallet / top_10 / EVM
               unverified). It runs before the GT /info dossier so a whale
               does not spend a rate-limit slot. holder_count, authority and
               honeypot stay in chain_kill after GT.
chain_kill the rest of the on-chain facts, after the dossier.
soft_kill  costs a judgement, runs on a handful.

Facts kill before judgements do, which is why authority_open and honeypot are
comparisons here and not questions in questions.py.

Log every rejection with the check that fired. Under ten rejections a day and your
filter is misconfigured, not your market.
"""
from secret_utils import safe_err
from thresholds import HARD, SOFT, SHAPE_MIN_CROWD


def free_kill(t) -> str | None:
    """Pass one. Runs on the whole universe, costs nothing, touches no network.
       Everything it reads came back with the FOMO batch.
       
       None means missing data (FOMO has no value). Distinct kill reasons for None
       let ops tell missing data from real thin books."""
    if not HARD["min_age_minutes"] <= t["age_minutes"] <= HARD["max_age_hours"] * 60:
        return "age"
    # Missing data gets distinct reason (no_liq vs liquidity, etc.)
    if t["liquidity_usd"] is None:
        return "no_liq"
    if t["liquidity_usd"] < HARD["min_liquidity_usd"]:
        return "liquidity"
    if t["volume_h24"] is None:
        return "no_vol"
    if t["volume_h24"] < HARD["min_volume_h24"]:
        return "volume"
    if t["mcap_usd"] is None:
        return "no_mcap"
    if not (HARD["min_mcap_usd"] <= t["mcap_usd"] <= HARD["max_mcap_usd"]):
        return "mcap"
    return None


def trade_kill(t) -> str | None:
    """Pass two. One DexScreener call already spent on this token. Tens, not hundreds.
    
    t now has 'dex_status' field: 'ok', 'empty', 'error', or 'gt_fallback'.
    """
    dex_status = t.get("dex_status", "ok")
    
    # Error: requeue (will be handled by caller, not benched here)
    if dex_status == "error":
        return "dex_error"
    
    # None trades_h24 = no data available, treat as no_pair regardless of status
    # (handles cases where GT fallback has no data, or other edge cases)
    if t["trades_h24"] is None:
        return "no_pair"
    
    # Now we have a real number for trades_h24
    if t["trades_h24"] < HARD["min_trades_h24"]:
        return "trades"
    
    if t["sells_h1"] == 0 and (t["buys_h1"] or 0) > 20:
        return "no_sells"
    
    return None


def concentration_kill(d) -> str | None:
    """Wallet-concentration subset of chain_kill. Same thresholds and reasons.

    Safe to run before the GT /info dossier: it only reads top_wallet_percent,
    top_10_percent, and the EVM fail-closed flags. holder_count, authority and
    honeypot stay in chain_kill, which needs GT fields.
    """
    # EVM chains: fail closed without top_wallet verification
    # Bug #12: Use holders_pending for transient failures, top_wallet_unverified for definitive
    chain = d.get("chain")
    if chain and chain != "solana":
        if d.get("top_wallet_percent") is None:
            # Check if transient failure
            if d.get("evm_holder_transient"):
                return "holders_pending"  # 15 min bench for retry
            else:
                return "top_wallet_unverified"  # 6h bench for definitive failure

    if d.get("top_wallet_percent") is not None and \
       d["top_wallet_percent"] > HARD["max_top_wallet"]:
        return "top_wallet"
    if d.get("top_10_percent") is not None and \
       float(d["top_10_percent"]) / 100 > HARD["max_top_10"]:
        return "top_10"
    return None


def chain_kill(d) -> str | None:
    """After the dossier, still free. Facts, not judgements."""
    if (k := concentration_kill(d)):
        return k
    if d.get("holder_count") is not None and d["holder_count"] < HARD["min_holders"]:
        return "holders"
    
    # Solana authority check: only kill when either authority is True (explicitly open)
    if d.get("chain") == "solana":
        mint_auth = d.get("mint_authority")
        freeze_auth = d.get("freeze_authority")
        
        # If both are explicitly True (open), kill
        if mint_auth is True or freeze_auth is True:
            return "authority_open"
        
        # If both are None (unknown from GT) and we have addr, optionally check RPC as fallback
        if mint_auth is None and freeze_auth is None and d.get("addr"):
            import logging
            import os
            log = logging.getLogger("filter")
            
            # Optional RPC fallback (cheap, public mainnet endpoint)
            rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            try:
                import requests
                resp = requests.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [d["addr"], {"encoding": "jsonParsed"}]
                    },
                    timeout=10
                )
                result = resp.json().get("result")
                if result and result.get("value"):
                    parsed = result["value"].get("data", {}).get("parsed", {})
                    mint_info = parsed.get("info", {})
                    rpc_mint_auth = mint_info.get("mintAuthority")
                    rpc_freeze_auth = mint_info.get("freezeAuthority")
                    
                    # If RPC confirms either authority is set (non-null), kill
                    if rpc_mint_auth or rpc_freeze_auth:
                        ticker = d.get("ticker", d.get("addr", "unknown"))
                        log.info("authority_open %s via RPC fallback: mint=%s freeze=%s", 
                                ticker, rpc_mint_auth, rpc_freeze_auth)
                        return "authority_open"
                    else:
                        ticker = d.get("ticker", d.get("addr", "unknown"))
                        log.debug("authority_check %s via RPC: both revoked", ticker)
            except Exception as e:
                # RPC failure means unknown, don't kill on absence of proof
                ticker = d.get("ticker", d.get("addr", "unknown"))
                log.debug("authority_check %s RPC failed: %s, treating as unknown", ticker, safe_err(e))
    
    if d.get("chain") in ("bsc", "base") and d.get("is_honeypot") is True:
        return "honeypot"                # also a fact
    return None


def soft_kill(ans, age_minutes=None) -> tuple[str, float] | None:
    """Jev's answers against SOFT. First failure wins.
    
    Returns (reason, noul_value) on kill, None on pass.
    
    Age-aware for momentum_already_spent: young tokens (<60m) naturally show
    higher momentum during healthy launches. Compare against age-appropriate
    threshold rather than treating all ages the same.
    """
    for name, (direction, limit) in SOFT.items():
        a = ans.get(name)
        if a is None:
            continue                     # question not asked for this chain
        v = a.get("noul", a.get("score"))
        if v is None:
            continue
        
        # Age-aware momentum threshold for young tokens
        effective_limit = limit
        if name == "momentum_already_spent" and age_minutes is not None and age_minutes < 60:
            # Young tokens (<60m): use higher threshold (0.85) to allow healthy early launches
            # Old tokens (>=60m): keep existing strict threshold (0.60)
            effective_limit = 0.85
        
        if direction == "max" and v > effective_limit: return (name, v)
        if direction == "min" and v < effective_limit: return (name, v)

    shape = ans.get("shape")
    if shape:
        prob_crowd = shape["probabilities"]["crowd"]
        if shape["choice"] in ("fading", "one_buyer"):        return ("shape", prob_crowd)
        if prob_crowd < SHAPE_MIN_CROWD: return ("shape_weak", prob_crowd)

    chain = ans.get("sell_side_risk")
    if chain and chain["choice"] in ("flagged", "suspicious"):
        # For categorical kills, return None as the value (no numeric score)
        return ("sell_side", None)
    return None
