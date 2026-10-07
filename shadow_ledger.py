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
# FOMO provides vol24 but not vol_h6, so we track volume history to compute actual decay.
EXIT_VOLUME_RATIO_THRESHOLD = 0.20

# Shadow ledger fallback exits (NOT trading thresholds, only for shadow tracking):
# These are conservative defaults to ensure shadow positions eventually close.
SHADOW_MAX_HOLD_HOURS = 6.0        # Max hold time before forced exit
SHADOW_STOP_LOSS_PCT = -30.0       # Stop loss at -30% (conservative)
SHADOW_TAKE_PROFIT_PCT = 100.0     # Take profit at +100% (let winners run)


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
        "last_price_usd": entry_price,  # For stale marking
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


def mark(address: str, network_id: int, current_price: float = None, volume_h24: float = None, 
         volume_history: list = None) -> dict:
    """
    Mark an open shadow position to market and check exit conditions.
    
    Args:
        address: Token address
        network_id: Network ID
        current_price: Current price in USD (if None, position flagged as stale, stays open)
        volume_h24: 24-hour volume (optional, for tracking volume history)
        volume_history: List of recent volume snapshots for computing decay
    
    Returns:
        Mark record dict, or close record if exit triggered
    """
    positions = open_positions()
    pos = next((p for p in positions if p["address"] == address and p["network_id"] == network_id), None)
    
    if not pos:
        return None
    
    entry_price = pos["entry_price_usd"]
    entry_time = _parse_ts(pos["ts"])
    held_hours = (time.time() - entry_time) / 3600
    
    # If no current price, flag as stale but keep open
    if current_price is None or current_price <= 0:
        log.warning("shadow position %s has stale price, keeping open with last mark", pos["ticker"])
        mark_record = {
            "action": "mark",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ticker": pos["ticker"],
            "address": address,
            "network_id": network_id,
            "stale": True,
            "last_price_usd": pos.get("last_price_usd", entry_price),
            "held_hours": held_hours,
        }
        _append(mark_record)
        return mark_record
    
    # Calculate unrealized PnL
    size_tokens = pos["size_tokens"]
    current_value_usd = size_tokens * current_price
    unrealized_pnl_usd = current_value_usd - pos["size_usd"]
    unrealized_pnl_pct = (current_price / entry_price - 1) * 100
    
    # Check exit conditions (in order of priority)
    should_exit = False
    exit_reason = None
    
    # 1. Shadow ledger time stop (NOT a trading threshold)
    if held_hours >= SHADOW_MAX_HOLD_HOURS:
        should_exit = True
        exit_reason = f"time_stop_{held_hours:.1f}h"
    
    # 2. Shadow ledger stop-loss (NOT a trading threshold)
    elif unrealized_pnl_pct <= SHADOW_STOP_LOSS_PCT:
        should_exit = True
        exit_reason = f"stop_loss_{unrealized_pnl_pct:.1f}pct"
    
    # 3. Shadow ledger take-profit (NOT a trading threshold)
    elif unrealized_pnl_pct >= SHADOW_TAKE_PROFIT_PCT:
        should_exit = True
        exit_reason = f"take_profit_{unrealized_pnl_pct:.1f}pct"
    
    # 4. Volume decay exit (RISK's rule, requires history)
    elif volume_history and len(volume_history) >= 2:
        # Compute 6-hour volume from recent history
        now = time.time()
        six_hours_ago = now - 6 * 3600
        recent_volumes = [v for v in volume_history if v["ts"] >= six_hours_ago]
        
        if recent_volumes and volume_h24:
            # Approximate h6 volume as sum of recent snapshots
            # (This is a rough estimate; real RISK would have actual h6 data)
            vol_h6_estimate = sum(v["vol24"] for v in recent_volumes) / len(recent_volumes) * 0.25
            avg_h6 = volume_h24 / 4
            
            if avg_h6 > 0:
                ratio = vol_h6_estimate / avg_h6
                if ratio < EXIT_VOLUME_RATIO_THRESHOLD:
                    should_exit = True
                    exit_reason = f"volume_decay_{ratio:.3f}"
    
    if should_exit:
        return close(address, network_id, current_price=current_price, reason=exit_reason)
    
    # Mark to market (stays open)
    mark_record = {
        "action": "mark",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": pos["ticker"],
        "address": address,
        "network_id": network_id,
        "current_price_usd": current_price,
        "volume_h24": volume_h24,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "held_hours": held_hours,
        "stale": False,
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
        "chain": pos.get("chain"),  # Preserve chain from entry for proper links
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
    Includes last_price_usd from most recent mark for stale tracking.
    """
    if not LEDGER_PATH.exists():
        return []
    
    entries = {}
    last_marks = {}  # Track most recent mark per position
    
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
            elif record["action"] == "mark" and key in entries:
                # Track last good price from marks
                if not record.get("stale") and record.get("current_price_usd"):
                    last_marks[key] = record["current_price_usd"]
            elif record["action"] == "close" and key in entries:
                del entries[key]
                if key in last_marks:
                    del last_marks[key]
    
    # Update entries with last good mark prices
    positions = []
    for key, entry in entries.items():
        if key in last_marks:
            entry = dict(entry)  # Copy to avoid mutating ledger data
            entry["last_price_usd"] = last_marks[key]
        positions.append(entry)
    
    return positions


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


def _get_volume_history(address: str, network_id: int) -> list:
    """
    Get volume history for a position from past mark records.
    Returns list of {ts: unix_time, vol24: volume} dicts.
    """
    if not LEDGER_PATH.exists():
        return []
    
    history = []
    with LEDGER_PATH.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if (record.get("action") == "mark" and 
                record.get("address") == address and 
                record.get("network_id") == network_id and
                record.get("volume_h24") is not None):
                history.append({
                    "ts": _parse_ts(record["ts"]),
                    "vol24": record["volume_h24"]
                })
    
    return history


def mark_all_open_positions(fomo_client) -> dict:
    """
    Mark all open shadow positions to market using FOMO data.
    Tracks volume history per position to compute actual decay for exit rule.
    Uses fallback exits (time stop, stop-loss, take-profit) clearly labeled as
    shadow defaults, not trading thresholds.
    
    Args:
        fomo_client: Fomo instance to fetch fresh prices
    
    Returns:
        dict with counts: marked, closed, stale
    """
    opens = open_positions()
    if not opens:
        return {"marked": 0, "closed": 0, "stale": 0}
    
    # Build list of tids for FOMO fetch
    tids = [f"{pos['address']}:{pos['network_id']}" for pos in opens]
    
    # Fetch fresh data from FOMO (cheap, not GeckoTerminal)
    try:
        fomo_data = fomo_client.tokens(tids)
    except Exception as e:
        log.warning("failed to fetch FOMO data for shadow positions: %s", e)
        fomo_data = {}
    
    marked_count = 0
    closed_count = 0
    stale_count = 0
    
    for pos in opens:
        tid = f"{pos['address']}:{pos['network_id']}"
        token_data = fomo_data.get(tid, {})
        
        price = token_data.get("price")
        vol24 = token_data.get("vol24")
        
        # Get volume history for this position
        volume_history = _get_volume_history(pos["address"], pos["network_id"])
        
        # Mark position (will check all exit conditions)
        result = mark(pos["address"], pos["network_id"], 
                     current_price=price, volume_h24=vol24, volume_history=volume_history)
        
        if result:
            if result["action"] == "close":
                closed_count += 1
            elif result.get("stale"):
                stale_count += 1
            else:
                marked_count += 1
    
    log.info("shadow mark: %d marked, %d closed, %d stale", marked_count, closed_count, stale_count)
    return {"marked": marked_count, "closed": closed_count, "stale": stale_count}
