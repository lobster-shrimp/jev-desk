"""
THE SHIFT — the process that never stops. Owns the cycle, calls everything in order,
hands finished orders to the seats. Start it with shadow=True and leave it that way
for a week.

The budget is the design. GeckoTerminal gives ten calls a minute, six go to listing
fresh pools across three chains, so three dossiers per cycle is what is left. Want more
dossiers, cut pages to 1 and you get six.

Failure handling (runs unattended):
  429 GeckoTerminal  -> back off a full minute, do not retry in place. Persisting? pages=1.
  429 DexScreener    -> lower DEX_BUDGET.
  429/529 Jev        -> the SDK retries with backoff on its own.
  422 Jev            -> question is malformed. Never retry. Log and stop the cycle.
  dossier throws     -> skip that token. Not a pass, not a retry loop.
  judge unreachable  -> skip the cycle entirely. No judge, no guessing, stand down.
  FOMO token expired -> refresh the Privy bearer out of Chrome and continue.
"""
import logging
import time

import book
from collect import universe, shortlist, trade_counts, dossier, social_state
from filter import free_kill, trade_kill, chain_kill, soft_kill
from pick import pick, size_factor_for

CHAIN_SET     = {1399811149: "solana", 56: "bsc", 8453: "bsc", 4663: "robinhood"}
CYCLE_SECONDS = 900
GT_PER_MINUTE = 10          # free tier
GT_UNIVERSE   = 6           # 3 chains x 2 pages, spent before the funnel starts
GT_DOSSIER    = 3           # what is left for dossiers in the same minute
DEX_BUDGET    = 25          # DexScreener calls per cycle, pass two only
log = logging.getLogger("desk")


class JudgeDown(Exception):
    """The judge is unreachable or a question set is malformed. The cycle stands down."""


def run_once(fomo, judge, desk, bank, shadow=True, gt_dossier=GT_DOSSIER):
    if (h := book.held()):                       # RISK owns the desk right now
        log.info("holding %s for %.0f min, no scan this cycle",
                 h["ticker"], h["minutes"])
        return None, {"held": h["ticker"], "minutes": round(h["minutes"])}

    stats = {"seen": 0, "benched": 0, "free": {}, "trade": {},
             "chain": {}, "soft": {}, "judged": 0}
    survivors = []
    gt_slots, dex_slots = gt_dossier, DEX_BUDGET

    ids = universe()                             # fresh pools, 3 chains, GT_UNIVERSE slots
    for t in shortlist(fomo, ids):               # pass one: free, no per-token requests
        stats["seen"] += 1
        if book.benched(t["tid"]):               # already judged, still serving its time
            stats["benched"] += 1
            continue
        if (k := free_kill(t)):
            book.sit(t["tid"], k)
            stats["free"][k] = stats["free"].get(k, 0) + 1
            continue

        if dex_slots <= 0 or gt_slots <= 0:
            break                                # out of budget, not out of ideas

        t |= trade_counts(t)                     # pass two: one DexScreener call
        dex_slots -= 1
        if (k := trade_kill(t)):
            book.sit(t["tid"], k)
            stats["trade"][k] = stats["trade"].get(k, 0) + 1
            continue

        try:
            d = dossier(t)                       # pass three: one GeckoTerminal slot
            gt_slots -= 1
        except Exception as e:
            log.warning("dossier failed %s: %s", t["ticker"], e)
            gt_slots -= 1                        # a failed call still cost you the slot
            book.sit(t["tid"], "dossier_failed")
            continue                             # missing is missing, not a pass

        if (k := chain_kill(d)):
            book.sit(t["tid"], k)                # facts bench longest
            stats["chain"][k] = stats["chain"].get(k, 0) + 1
            continue

        d["intended_ticket_usd"] = bank * 0.06   # the most SIZE could ever allow
        d["x_account"] = desk.read_x(d["x_handle"]) if d["x_handle"] else None

        ans = {}
        try:
            ans |= judge("market", d)["answers"]                  # pass four
            ans |= judge(CHAIN_SET[d["net"]], d)["answers"]
            if d["x_account"]:
                ans |= judge("social", social_state(d))["answers"]
            stats["judged"] += 1
        except RuntimeError as e:                # 422: the question is wrong and stays wrong
            log.error("malformed question set, stopping cycle: %s", e)
            raise JudgeDown(str(e))
        except Exception as e:
            log.warning("judge failed %s: %s", d["ticker"], e)
            continue                             # no bench: the token is not at fault

        if (k := soft_kill(ans)):
            book.sit(t["tid"], k)
            stats["soft"][k] = stats["soft"].get(k, 0) + 1
            continue

        survivors.append((d, ans))

    log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "
             "trade %(trade)s, chain %(chain)s, soft %(soft)s, judged %(judged)s", stats)

    if not survivors:
        return None, stats
    if len(survivors) == 1:                      # a choice over one option proves nothing
        d, ans = survivors[0]
        order = {"model": "single-survivor", "size_factor": size_factor_for(ans),
                 "confidence": None,
                 "token": {"ticker": d["ticker"], "address": d["addr"],
                           "network_id": d["net"], "chain": d["chain"]},
                 "why": ans}
    else:
        order = pick(judge, survivors)           # pass five

    if order is None:
        return None, stats
    order["order_id"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if shadow:
        desk.log_shadow(order, stats)            # written, never sent
        return None, stats

    book.take(order)          # the desk is now held. No scan until RISK calls release().
    return order, stats


def main(fomo, judge, desk, shadow=True, once=False):
    """desk is your Grok Bot side. It has to provide five things:
         bank()                 -> float, free cash right now
         read_x(handle)         -> the X block SOCIAL collects with its plugin, or None
         log_shadow(order, st)  -> append a row for the shadow week
         report(order, stats)   -> one line to Telegram, trade or no trade
         send_to_seats(order)   -> hand it to SIZE, then FILLS, then RISK, in that order
    """
    while True:
        try:
            fomo.token()                         # Privy bearer lives ~60 min, refresh it
            order, stats = run_once(fomo, judge, desk, desk.bank(), shadow)
            desk.report(order, stats)            # every cycle, trade or not
            if order:
                desk.send_to_seats(order)
        except JudgeDown as e:
            log.error("judge down, standing down this cycle: %s", e)
        except Exception as e:
            log.exception("cycle blew up: %s", e)
        if once:
            return
        time.sleep(CYCLE_SECONDS)
