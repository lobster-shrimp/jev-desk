"""
Shadow PnL Ledger — hypothetical tracking of what the desk would have traded.

Records entry, marks to market over time, and closes positions using the desk's exit rules.
Persists to outbox/shadow_ledger.jsonl (append-only, gitignored).

The owner's success metric is REALIZED PnL from FILLED CLOSES. Shadow results are
hypothetical and must be labeled as such everywhere.
"""
import json
import logging
import os
import pathlib
import time

log = logging.getLogger("shadow_ledger")

OUTBOX = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))
LEDGER_PATH = OUTBOX / "shadow_ledger.jsonl"

# Hypothetical sizing (owner specified $50 for shadow)
SHADOW_TICKET_USD = 50.0

# Exit rule from prompts/RISK.txt: close when volume.h6 / (volume.h24 / 4) < 0.20
# If we cannot measure volume, we close anyway (RISK's rule)
EXIT_VOLUME_RATIO_THRESHOLD = 0.20


def entry(order: dict, fomo_data: dict) -> dict:
    """
    Record a hypothetical entry when the desk would have traded in shadow mode.
    
    Args:
        order: The shadow order dict from pick/single-survivor
        fomo_data: Current FOMO data for this token (has price, volume, etc.)
    
    Returns:
        Entry record dict
    """
    token = order["token"]
    entry_price = fomo_data.get("price")
    
    if entry_price is None or entry_price <= 0:
        log.warning("cannot record shadow entry for %s: missing or invalid price", token["ticker"])
        return None
    
    # Calculate size in tokens based on hypothetical $50 USD and entry price
    size_tokens = SHADOW_TICKET_USD / entry_price
    
    entry_record = {
        "action": "entry",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": token["ticker"],
        "address": token["address"],
        "network_id": token["network_id"],
        "chain": token["chain"],
        "entry_price_usd": entry_price,
        "size_usd": SHADOW_TICKET_USD,
        "size_tokens": size_tokens,
        "size_factor": order.get("size_factor", 1.0),
        "confidence": order.get("confidence"),
        "model": order.get("model"),
        "order_id": order.get("order_id"),
    }
    
    _append(entry_record)
    log.info("shadow entry %s at $%.6f, %s tokens, $%.2f ticket",
             token["ticker"], entry_price, f"{size_tokens:.2f}", SHADOW_TICKET_USD)
    return entry_record


def mark(address: str, network_id: int, current_price: float, volume_h6: float = None, 
         volume_h24: float = None) -> dict:
    """
    Mark an open shadow position to market and check exit conditions.
    
    Args:
        address: Token address
        network_id: Network ID
        current_price: Current price in USD
        volume_h6: 6-hour volume (optional, for exit check)
        volume_h24: 24-hour volume (optional, for exit check)
    
    Returns:
        Mark record dict, or close record if exit triggered
    """
    positions = open_positions()
    pos = next((p for p in positions if p["address"] == address and p["network_id"] == network_id), None)
    
    if not pos:
        return None
    
    if current_price is None or current_price <= 0:
        # Cannot mark without price; close with stale mark rather than guessing
        log.warning("shadow position %s has stale price data, closing", pos["ticker"])
        return close(address, network_id, current_price=None, reason="stale_price")
    
    # Calculate unrealized PnL
    entry_price = pos["entry_price_usd"]
    size_tokens = pos["size_tokens"]
    current_value_usd = size_tokens * current_price
    unrealized_pnl_usd = current_value_usd - pos["size_usd"]
    unrealized_pnl_pct = (current_price / entry_price - 1) * 100
    
    # Check exit condition: volume.h6 / (volume.h24 / 4) < 0.20
    should_exit = False
    exit_reason = None
    
    if volume_h6 is not None and volume_h24 is not None and volume_h24 > 0:
        ratio = volume_h6 / (volume_h24 / 4)
        if ratio < EXIT_VOLUME_RATIO_THRESHOLD:
            should_exit = True
            exit_reason = f"volume_ratio_{ratio:.3f}"
    elif volume_h6 is None or volume_h24 is None:
        # Cannot measure volume, close anyway (RISK's rule)
        should_exit = True
        exit_reason = "volume_missing"
    
    if should_exit:
        return close(address, network_id, current_price=current_price, reason=exit_reason)
    
    mark_record = {
        "action": "mark",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": pos["ticker"],
        "address": address,
        "network_id": network_id,
        "current_price_usd": current_price,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_pct": unrealized_pnl_pct,
    }
    
    _append(mark_record)
    return mark_record


def close(address: str, network_id: int, current_price: float = None, reason: str = "exit_rule") -> dict:
    """
    Close a shadow position and record realized PnL.
    
    Args:
        address: Token address
        network_id: Network ID
        current_price: Exit price (if None, PnL is marked as stale/unmeasured)
        reason: Why we're closing (exit_rule, stale_price, etc.)
    
    Returns:
        Close record dict
    """
    positions = open_positions()
    pos = next((p for p in positions if p["address"] == address and p["network_id"] == network_id), None)
    
    if not pos:
        log.warning("attempted to close non-existent shadow position %s:%s", address, network_id)
        return None
    
    if current_price is None or current_price <= 0:
        # Stale data, mark PnL as unmeasured
        realized_pnl_usd = None
        realized_pnl_pct = None
        exit_value_usd = None
    else:
        size_tokens = pos["size_tokens"]
        exit_value_usd = size_tokens * current_price
        realized_pnl_usd = exit_value_usd - pos["size_usd"]
        realized_pnl_pct = (current_price / pos["entry_price_usd"] - 1) * 100
    
    close_record = {
        "action": "close",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": pos["ticker"],
        "address": address,
        "network_id": network_id,
        "entry_price_usd": pos["entry_price_usd"],
        "exit_price_usd": current_price,
        "size_usd": pos["size_usd"],
        "exit_value_usd": exit_value_usd,
        "realized_pnl_usd": realized_pnl_usd,
        "realized_pnl_pct": realized_pnl_pct,
        "reason": reason,
        "held_seconds": time.time() - _parse_ts(pos["ts"]),
    }
    
    _append(close_record)
    if realized_pnl_usd is not None:
        log.info("shadow close %s at $%.6f, realized PnL $%.2f (%.1f%%), reason=%s",
                 pos["ticker"], current_price, realized_pnl_usd, realized_pnl_pct, reason)
    else:
        log.info("shadow close %s with stale price, PnL unmeasured, reason=%s", pos["ticker"], reason)
    
    return close_record


def open_positions() -> list[dict]:
    """
    Return currently open shadow positions (entries without corresponding closes).
    """
    if not LEDGER_PATH.exists():
        return []
    
    entries = {}
    with LEDGER_PATH.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            addr = record.get("address")
            net_id = record.get("network_id")
            key = (addr, net_id)
            
            if record["action"] == "entry":
                entries[key] = record
            elif record["action"] == "close" and key in entries:
                del entries[key]
    
    return list(entries.values())


def closed_positions(limit: int = 50) -> list[dict]:
    """
    Return recently closed shadow positions with realized PnL.
    """
    if not LEDGER_PATH.exists():
        return []
    
    # Read all closes, most recent first
    closes = []
    with LEDGER_PATH.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["action"] == "close":
                closes.append(record)
    
    closes.reverse()  # Most recent first
    return closes[:limit]


def summary() -> dict:
    """
    Return summary statistics: total realized PnL, open positions count, etc.
    """
    opens = open_positions()
    closes = closed_positions(limit=None)  # Get all closes
    
    total_realized_pnl = sum(c.get("realized_pnl_usd") or 0 for c in closes if c.get("realized_pnl_usd") is not None)
    total_trades = len(closes)
    winning_trades = sum(1 for c in closes if (c.get("realized_pnl_usd") or 0) > 0)
    losing_trades = sum(1 for c in closes if (c.get("realized_pnl_usd") or 0) < 0)
    unmeasured_trades = sum(1 for c in closes if c.get("realized_pnl_usd") is None)
    
    return {
        "open_positions_count": len(opens),
        "closed_trades_count": total_trades,
        "total_realized_pnl_usd": total_realized_pnl,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "unmeasured_trades": unmeasured_trades,
        "win_rate": winning_trades / total_trades if total_trades > 0 else 0,
    }


def _append(record: dict):
    """Append a record to the ledger file."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _parse_ts(iso: str) -> float:
    """Parse ISO timestamp to unix time."""
    try:
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return time.time()
