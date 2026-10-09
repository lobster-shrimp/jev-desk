"""
One-off backfill: parse existing outbox/run.log into the cycle-history store.

    python backfill_cycle_history.py
    python backfill_cycle_history.py path/to/run.log

Does not print .env or secrets. Safe to re-run; each invocation inserts another
copy of parsed cycles, so run once per log.
"""
import argparse
import logging
import os
import sys

from secret_utils import safe_err

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill_cycle_history")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill cycle_history.db from outbox/run.log")
    ap.add_argument("log_path", nargs="?", default=None, help="Path to run.log (default: outbox/run.log)")
    args = ap.parse_args(argv)
    try:
        import cycle_history
        n = cycle_history.backfill_log(args.log_path)
    except Exception as e:
        log.error("backfill failed: %s", safe_err(e))
        return 1
    log.info("backfilled %d cycles", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
