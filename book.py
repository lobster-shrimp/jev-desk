"""
THE BOOK — what the desk remembers between cycles. Creates desk.db on first run.

One position at a time. While a position is open, the scan does not run at all.
A rejected token stays rejected for a while, and the length depends on WHAT rejected it.

RISK CALLS release() AND NOBODY ELSE DOES.
BENCH ON THE FAILED CHECK, NOT ON THE TOKEN.
A HELD POSITION MEANS NO SCAN AT ALL.
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("DESK_DB", "desk.db")
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS position(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  ticker TEXT, addr TEXT, net INT, opened_at REAL);
CREATE TABLE IF NOT EXISTS bench(
  tid TEXT PRIMARY KEY, reason TEXT, until REAL);
""")

# how long a rejection stands, by what fired it (minutes)
BENCH_MINUTES = {
    # facts that will not change while this token exists
    "honeypot": 100_000, "authority_open": 100_000,
    "top_wallet": 100_000, "sell_side": 100_000,
    # slow to change
    "recycled_account": 360, "account_is_the_project": 360,
    # can change as the float moves
    "top_10": 90, "holders": 90, "dev_still_loaded": 90,
    "concentration_is_exit_risk": 90,
    # can change inside the hour, keep it short or you miss the token maturing
    "shape": 25, "shape_weak": 25, "momentum_already_spent": 25,
    "liquidity_fits_ticket": 25, "liquidity": 25, "volume": 25,
    "trades": 25, "mcap": 25, "age": 20,
}
DEFAULT_BENCH = 45


def held():
    r = DB.execute("SELECT ticker, opened_at FROM position WHERE id=1").fetchone()
    return {"ticker": r[0], "minutes": (time.time() - r[1]) / 60} if r else None


def take(order):
    t = order["token"]
    DB.execute("INSERT OR REPLACE INTO position VALUES (1,?,?,?,?)",
               (t["ticker"], t["address"], t["network_id"], time.time()))
    DB.commit()


def release():
    """RISK calls this the moment a close is filled. Nothing else calls it."""
    DB.execute("DELETE FROM position")
    DB.commit()


def benched(tid: str) -> bool:
    r = DB.execute("SELECT until FROM bench WHERE tid=?", (tid,)).fetchone()
    return bool(r and r[0] > time.time())


def sit(tid: str, reason: str):
    mins = BENCH_MINUTES.get(reason, DEFAULT_BENCH)
    DB.execute("INSERT OR REPLACE INTO bench VALUES (?,?,?)",
               (tid, reason, time.time() + mins * 60))
    DB.commit()


def bench_report(limit: int = 50) -> list[tuple]:
    """For the evening read: what is sitting out and why."""
    return DB.execute("SELECT tid, reason, ROUND((until-?)/60) FROM bench WHERE until>? "
                      "ORDER BY until DESC LIMIT ?", (time.time(), time.time(), limit)).fetchall()
