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
CREATE TABLE IF NOT EXISTS defer(
  tid TEXT PRIMARY KEY,
  ready REAL NOT NULL,
  drop_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations(
  name TEXT PRIMARY KEY,
  applied_at REAL NOT NULL
);
""")

# One-time migration: clear authority_open bench entries (falsely benched due to 'no' string bug)
def _apply_migration(name: str, fn):
    """Apply migration if not already applied."""
    applied = DB.execute("SELECT 1 FROM migrations WHERE name=?", (name,)).fetchone()
    if not applied:
        fn()
        DB.execute("INSERT INTO migrations VALUES (?,?)", (name, time.time()))
        DB.commit()

def _clear_authority_open_bench():
    """Remove bench entries with reason='authority_open' (bug fix migration)."""
    deleted = DB.execute("DELETE FROM bench WHERE reason='authority_open'").rowcount
    if deleted > 0:
        import logging
        log = logging.getLogger("book")
        log.info("migration clear_authority_open_bench: removed %d falsely benched tokens", deleted)

_apply_migration("clear_authority_open_bench", _clear_authority_open_bench)

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
DEFER_CAP = 200


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


def bench_count() -> int:
    """How many tokens are currently benched."""
    r = DB.execute("SELECT COUNT(*) FROM bench WHERE until>?", (time.time(),)).fetchone()
    return r[0] if r else 0


def defer(tid: str, ready: float, drop_at: float):
    """Store a too-young token for later rescoring."""
    import logging
    log = logging.getLogger("desk")
    now = time.time()
    existing = DB.execute("SELECT 1 FROM defer WHERE tid=?", (tid,)).fetchone()
    if existing:
        DB.execute("UPDATE defer SET ready=?, drop_at=? WHERE tid=?", (ready, drop_at, tid))
        DB.commit()
        log.info("defer update tid=%s ready=%.0f drop_at=%.0f", tid, ready, drop_at)
        return
    active_count = DB.execute("SELECT COUNT(*) FROM defer WHERE drop_at>?", (now,)).fetchone()[0]
    if active_count >= DEFER_CAP:
        log.warning("defer full, cannot insert tid=%s", tid)
        return
    DB.execute("INSERT INTO defer VALUES (?,?,?)", (tid, ready, drop_at))
    DB.commit()


def defer_due() -> list[str]:
    """Tids where ready <= now and drop_at > now."""
    now = time.time()
    rows = DB.execute("SELECT tid FROM defer WHERE ready<=? AND drop_at>?", (now, now)).fetchall()
    return [r[0] for r in rows]


def forget_defer(tid: str):
    """Delete that defer row."""
    DB.execute("DELETE FROM defer WHERE tid=?", (tid,))
    DB.commit()


def expire_defer():
    """Delete rows with drop_at <= now."""
    DB.execute("DELETE FROM defer WHERE drop_at<=?", (time.time(),))
    DB.commit()
