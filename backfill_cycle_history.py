"""
One-off backfill: parse existing outbox/run.log into the cycle-history store.

    python backfill_cycle_history.py
    python backfill_cycle_history.py path/to/run.log

Does not print .env or secrets. Safe to re-run: already-stored cycles (live or
backfill) are skipped by timestamp.
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
        result = cycle_history.backfill_log(args.log_path)
    except Exception as e:
        log.error("backfill failed: %s", safe_err(e))
        return 1
    log.info(
        "backfilled %d cycles (skipped %d, unparsed soft=%d chain=%d)",
        result.get("inserted", 0), result.get("skipped", 0),
        result.get("unparsed_soft", 0), result.get("unparsed_chain", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
