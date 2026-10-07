"""
THE SHIFT — the process that never stops. Owns the cycle, calls everything in order,
hands finished orders to the seats. Start it with shadow=True and leave it that way
for a week.

The budget is the design. GeckoTerminal gives ten calls a minute. A shared rate limiter
allocates them: reserve slots for dossiers (the priority work), then use what remains
for universe pagination. When a dossier fails due to 429/budget exhaustion, requeue
the token via defer for immediate retry next cycle.

Failure handling (runs unattended):
  429 GeckoTerminal  -> dossier: requeue via defer for next cycle.
                        universe: skip remaining pages, continue with what we have.
  429 DexScreener    -> lower DEX_BUDGET.
  429/529 Jev        -> the SDK retries with backoff on its own.
  422 Jev            -> question is malformed. Never retry. Log and stop the cycle.
  dossier throws     -> if DossierRetryNeeded: defer for retry. else: skip that token.
  judge unreachable  -> skip the cycle entirely. No judge, no guessing, stand down.
  FOMO token expired -> refresh the Privy bearer out of Chrome and continue.
"""
import logging
import os
import time

import book
from collect import universe, shortlist, trade_counts, dossier, social_state, GTRateLimiter, DossierRetryNeeded
from filter import free_kill, trade_kill, chain_kill, soft_kill
from pick import pick, size_factor_for
from thresholds import HARD, SOFT

CHAIN_SET     = {1399811149: "solana", 56: "bsc", 8453: "bsc", 4663: "robinhood"}
CYCLE_SECONDS = 900
GT_CALLS_PER_MIN = int(os.environ.get("GT_CALLS_PER_MIN", "8"))  # conservative default, configurable
GT_DOSSIER_RESERVE = 3      # reserve this many slots for dossiers before calling universe
DEX_BUDGET    = 25          # DexScreener calls per cycle, pass two only
log = logging.getLogger("desk")

# Shared GT rate limiter for the entire process (not per-cycle)
_gt_limiter = GTRateLimiter(calls_per_min=GT_CALLS_PER_MIN)


class JudgeDown(Exception):
    """The judge is unreachable or a question set is malformed. The cycle stands down."""


def run_once(fomo, judge, desk, bank, shadow=True, gt_dossier_reserve=GT_DOSSIER_RESERVE, 
             gt_limiter=None):
    if (h := book.held()):                       # RISK owns the desk right now
        log.info("holding %s for %.0f min, no scan this cycle",
                 h["ticker"], h["minutes"])
        return None, {"held": h["ticker"], "minutes": round(h["minutes"])}

    stats = {"seen": 0, "benched": 0, "free": {}, "trade": {},
             "chain": {}, "soft": {}, "judged": 0, "tokens": [], "requeued": 0}
    survivors = []
    dex_slots = DEX_BUDGET
    
    # Use shared GT rate limiter (default to global singleton)
    if gt_limiter is None:
        gt_limiter = _gt_limiter
    gt_limiter.reserve(gt_dossier_reserve)  # Log how much reserved

    def record(t, stage, reason=None, soft_noul=None, soft_scores=None):
        """One row per token for the log and the ops panel: where it stopped and why."""
        row = {
            "tid": t.get("tid"), "ticker": t.get("ticker"), "chain": t.get("chain"),
            "mcap_usd": t.get("mcap_usd"), "liquidity_usd": t.get("liquidity_usd"),
            "age_minutes": t.get("age_minutes"),
            "stage": stage, "reason": reason,
            "verdict": "PASS" if reason is None else "DROP"
        }
        if soft_noul is not None:
            row["soft_noul"] = soft_noul
        if soft_scores:
            row["soft_scores"] = soft_scores
        stats["tokens"].append(row)

    ids = universe(limiter=gt_limiter)           # fresh + trending pools, budget-aware
    book.expire_defer()                          # drop rows past max age
    due = book.defer_due()                       # ids ready for rescoring
    seen_ids = set()
    combined_ids = []
    for tid in due + ids:                        # due first, then universe, first occurrence wins
        if tid not in seen_ids:
            seen_ids.add(tid)
            combined_ids.append(tid)
    
    shortlist_result = list(shortlist(fomo, combined_ids))
    shortlist_tids = {t["tid"] for t in shortlist_result}
    for tid in due:
        if tid not in shortlist_tids:
            log.info("defer outcome tid=%s reason=miss", tid)
            book.forget_defer(tid)
    
    # Age-prioritize for dossier work: separate young (<60m) vs old (≥60m) tokens.
    # Within each group, preserve turnover ordering from shortlist.
    # Process young tokens first to ensure they get dossier budget before old requeues.
    young_tokens = [t for t in shortlist_result if t.get("age_minutes", 0) < 60]
    old_tokens = [t for t in shortlist_result if t.get("age_minutes", 0) >= 60]
    age_prioritized = young_tokens + old_tokens
    
    # Track young tokens that still need dossiers this cycle (for in-cycle retry logic)
    young_pending_dossier = []
    
    for t in age_prioritized:                    # pass one: free, no per-token requests
        stats["seen"] += 1
        t.setdefault("chain", CHAIN_SET.get(t["net"]))
        if book.benched(t["tid"]):               # already judged, still serving its time
            stats["benched"] += 1
            continue
        if (k := free_kill(t)):
            # Log FOMO metrics on every free kill (missing values stay None)
            log.info("free tid=%s reason=%s age_minutes=%s liquidity_usd=%s volume_usd=%s mcap_usd=%s",
                     t["tid"], k, 
                     t.get("age_minutes"), 
                     t.get("liquidity_usd"), 
                     t.get("volume_h24"), 
                     t.get("mcap_usd"))
            if k == "age" and t["age_minutes"] < HARD["min_age_minutes"]:
                # Gate defer on liquidity: only defer if liquidity is known and >= threshold
                liq_usd = t.get("liquidity_usd")
                if liq_usd is not None and liq_usd >= HARD["min_liquidity_usd"]:
                    now = time.time()
                    ready = now + max(0, (HARD["min_age_minutes"] - t["age_minutes"]) * 60)
                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                    book.defer(t["tid"], ready, drop_at)
                    log.info("defer insert tid=%s age_minutes=%.1f liquidity_usd=%.0f",
                             t["tid"], t["age_minutes"], liq_usd)
                    stats["free"][k] = stats["free"].get(k, 0) + 1
                    record(t, "free", k)
                    continue
                # Below gate: stay age-killed without defer
                log.info("defer gate_reject tid=%s age_minutes=%.1f liquidity_usd=%s reason=%s",
                         t["tid"], t["age_minutes"], liq_usd, "missing" if liq_usd is None else "below_threshold")
            book.sit(t["tid"], k)
            log.info("defer outcome tid=%s reason=%s", t["tid"], k)
            book.forget_defer(t["tid"])
            stats["free"][k] = stats["free"].get(k, 0) + 1
            record(t, "free", k)
            continue

        # Log FOMO metrics on free pass
        log.info("free tid=%s reason=pass age_minutes=%s liquidity_usd=%s volume_usd=%s mcap_usd=%s",
                 t["tid"], 
                 t.get("age_minutes"), 
                 t.get("liquidity_usd"), 
                 t.get("volume_h24"), 
                 t.get("mcap_usd"))
        book.forget_defer(t["tid"])
        log.info("defer outcome tid=%s reason=pass", t["tid"])

        if dex_slots <= 0 or gt_limiter.available() <= 0:
            break                                # out of budget, not out of ideas

        t |= trade_counts(t)                     # pass two: one DexScreener call
        dex_slots -= 1
        if (k := trade_kill(t)):
            # Log trade kill with ticker, age, and tid for visibility
            log.info("trade tid=%s ticker=%s reason=%s age_minutes=%s",
                     t["tid"], t.get("ticker", "?"), k, t.get("age_minutes"))
            book.sit(t["tid"], k)
            log.info("defer outcome tid=%s reason=%s", t["tid"], k)
            book.forget_defer(t["tid"])
            stats["trade"][k] = stats["trade"].get(k, 0) + 1
            record(t, "trade", k)
            continue

        # Passed free & trade checks; track young tokens for in-cycle retry priority
        is_young = t.get("age_minutes", 0) < 60
        if is_young:
            young_pending_dossier.append(t)
        
        # If this is an old token and young tokens still need dossiers, defer to next cycle
        if not is_young and young_pending_dossier:
            log.info("defer old token %s (age %.1fm) while young tokens pending dossier", 
                     t["ticker"], t.get("age_minutes", 0))
            now = time.time()
            drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
            book.defer(t["tid"], now, drop_at)
            stats["requeued"] += 1
            record(t, "chain", "deferred_for_young")
            continue

        d = None
        try:
            d = dossier(t, limiter=gt_limiter)   # pass three: one GeckoTerminal slot (budget-aware)
            # Success: remove from young pending if applicable
            if is_young and t in young_pending_dossier:
                young_pending_dossier.remove(t)
        except DossierRetryNeeded as e:
            log.info("dossier retry needed for %s (age %.1fm): %s", 
                     t["ticker"], t.get("age_minutes", 0), e)
            
            # Young tokens get one in-cycle retry after waiting for rate limit recovery
            if is_young:
                log.info("young token %s (age %.1fm) hit 429, backing off before in-cycle retry", 
                         t["ticker"], t.get("age_minutes", 0))
                gt_limiter.wait_if_needed(priority=True)
                try:
                    d = dossier(t, limiter=gt_limiter)
                    # Success on retry: remove from young pending
                    if t in young_pending_dossier:
                        young_pending_dossier.remove(t)
                    log.info("young token %s dossier succeeded on in-cycle retry", t["ticker"])
                except DossierRetryNeeded as retry_e:
                    log.info("young token %s dossier failed on in-cycle retry, deferring: %s", 
                             t["ticker"], retry_e)
                    # Still failed after in-cycle retry: defer to next cycle
                    # Keep in young_pending so old tokens don't get slots this cycle
                    now = time.time()
                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                    book.defer(t["tid"], now, drop_at)
                    stats["requeued"] += 1
                    record(t, "chain", "requeued_after_retry")
                    continue
                except Exception as retry_e:
                    log.warning("young token %s dossier retry failed: %s", t["ticker"], retry_e)
                    # Keep in young_pending so old tokens don't get slots this cycle
                    book.sit(t["tid"], "dossier_failed")
                    log.info("defer outcome tid=%s reason=dossier_failed", t["tid"])
                    book.forget_defer(t["tid"])
                    record(t, "chain", "dossier_failed")
                    continue
            else:
                # Old token: defer without in-cycle retry
                now = time.time()
                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                book.defer(t["tid"], now, drop_at)
                stats["requeued"] += 1
                record(t, "chain", "requeued")
                continue
        except Exception as e:
            log.warning("dossier failed %s: %s", t["ticker"], e)
            # Keep young tokens in pending list so old tokens don't get slots
            book.sit(t["tid"], "dossier_failed")
            log.info("defer outcome tid=%s reason=dossier_failed", t["tid"])
            book.forget_defer(t["tid"])
            record(t, "chain", "dossier_failed")
            continue                             # missing is missing, not a pass
        
        # If we get here without a dossier, it was a non-retry exception path
        if d is None:
            continue

        if (k := chain_kill(d)):
            # Log chain kill with ticker, age, and tid for visibility
            log.info("chain tid=%s ticker=%s reason=%s age_minutes=%s",
                     d.get("tid"), d.get("ticker", "?"), k, d.get("age_minutes"))
            book.sit(t["tid"], k)                # facts bench longest
            log.info("defer outcome tid=%s reason=%s", t["tid"], k)
            book.forget_defer(t["tid"])
            stats["chain"][k] = stats["chain"].get(k, 0) + 1
            record(d, "chain", k)
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

        soft_result = soft_kill(ans, age_minutes=d.get("age_minutes"))
        if soft_result:
            reason, noul = soft_result
            # Collect all SOFT scores that were asked (compact one-line summary)
            soft_scores = {}
            for name in SOFT.keys():
                a = ans.get(name)
                if a:
                    v = a.get("noul", a.get("score"))
                    if v is not None:
                        soft_scores[name] = v
            # Log detailed soft kill with noul and age
            log.info("soft tid=%s ticker=%s reason=%s noul=%s age_minutes=%s soft_scores=%s",
                     d.get("tid"), d.get("ticker"), reason, noul, d.get("age_minutes"),
                     {k: round(v, 3) for k, v in soft_scores.items()})
            book.sit(t["tid"], reason)
            log.info("defer outcome tid=%s reason=%s", t["tid"], reason)
            book.forget_defer(t["tid"])
            stats["soft"][reason] = stats["soft"].get(reason, 0) + 1
            record(d, "soft", reason, soft_noul=noul, soft_scores=soft_scores)
            continue

        record(d, "judged", None)
        survivors.append((d, ans))

    log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "
             "trade %(trade)s, chain %(chain)s, soft %(soft)s, judged %(judged)s, requeued %(requeued)s", stats)

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
            # JudgeDown: stand down without writing error state (not inventing tokens)
        except Exception as e:
            log.exception("cycle blew up: %s", e)
            # Write error state so ops panel shows ERROR instead of stale last-good
            desk.write_state(None, {
                "error": str(e),
                "seen": 0,
                "benched": 0,
                "judged": 0,
                "free": {},
                "trade": {},
                "chain": {},
                "soft": {},
                "tokens": []
            })
        if once:
            return
        time.sleep(CYCLE_SECONDS)
