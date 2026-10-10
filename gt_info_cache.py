"""SQLite cache for GeckoTerminal /tokens/{addr}/info payloads.

Lives under outbox/ (default outbox/gt_info_cache.db, gitignored). Only successful
complete responses are stored. 429s and errors are never written. Keyed by
chain + address. TTL is GT_INFO_CACHE_TTL_MIN minutes, default 90, clamped 60–120.

Exceptions are routed through safe_err; this module never prints .env or secrets.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import time

from secret_utils import safe_err

log = logging.getLogger("gt_info_cache")

DEFAULT_TTL_MIN = 90
TTL_MIN_FLOOR = 60
TTL_MIN_CEILING = 120

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_time_fn = time.time


def set_time_fn(fn):
    """Inject a clock for deterministic tests. Pass None to restore time.time."""
    global _time_fn
    _time_fn = time.time if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def db_path() -> pathlib.Path:
    override = os.environ.get("GT_INFO_CACHE_DB")
    if override:
        return pathlib.Path(override)
    return outbox_dir() / "gt_info_cache.db"


def ttl_min() -> int:
    """Minutes the /info payload stays valid. Default 90, clamped to 60–120."""
    raw = os.environ.get("GT_INFO_CACHE_TTL_MIN")
    if raw is None or raw == "":
        v = DEFAULT_TTL_MIN
    else:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = DEFAULT_TTL_MIN
    return max(TTL_MIN_FLOOR, min(TTL_MIN_CEILING, v))


def ttl_seconds() -> float:
    return float(ttl_min()) * 60.0


def normalize_key(chain: str, address: str) -> tuple[str, str]:
    """Canonical cache key. Solana addresses are case-sensitive; EVM are not."""
    chain_key = (chain or "").strip().lower()
    addr = (address or "").strip()
    if chain_key != "solana":
        addr = addr.lower()
    return chain_key, addr


def reset() -> None:
    """Close the cached connection. Tests call this between cases."""
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = str(db_path())
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _init_schema(_conn)
    _conn_path = path
    return _conn


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS gt_info (
      chain TEXT NOT NULL,
      address TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      fetched_at REAL NOT NULL,
      PRIMARY KEY (chain, address)
    );
    """)
    db.commit()


def is_complete_attributes(attrs) -> bool:
    """True when a GT /info body yielded a usable attributes object."""
    return isinstance(attrs, dict)


def get(chain: str, address: str, now: float | None = None) -> dict | None:
    """Return cached attributes if present and younger than the TTL, else None."""
    chain_key, addr = normalize_key(chain, address)
    if not chain_key or not addr:
        return None
    ts = now if now is not None else current_time()
    try:
        db = _connect()
        row = db.execute(
            "SELECT payload_json, fetched_at FROM gt_info WHERE chain = ? AND address = ?",
            (chain_key, addr),
        ).fetchone()
        if row is None:
            return None
        age = ts - float(row["fetched_at"])
        if age > ttl_seconds():
            return None
        payload = json.loads(row["payload_json"])
        if not is_complete_attributes(payload):
            return None
        return payload
    except Exception as e:
        log.warning("gt info cache get failed: %s", safe_err(e))
        return None


def put(chain: str, address: str, attrs: dict, now: float | None = None) -> bool:
    """Store a successful complete attributes object. Returns True on write."""
    if not is_complete_attributes(attrs):
        return False
    chain_key, addr = normalize_key(chain, address)
    if not chain_key or not addr:
        return False
    ts = now if now is not None else current_time()
    try:
        db = _connect()
        db.execute(
            """INSERT OR REPLACE INTO gt_info (chain, address, payload_json, fetched_at)
               VALUES (?, ?, ?, ?)""",
            (chain_key, addr, json.dumps(attrs, default=str, sort_keys=True), float(ts)),
        )
        db.commit()
        return True
    except Exception as e:
        log.warning("gt info cache put failed: %s", safe_err(e))
        return False
