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
from collect import universe, shortlist, trade_counts, dossier, social_state, GTRateLimiter, DossierRetryNeeded, dex_canary_check, _clear_sol_owner_cache
from filter import free_kill, trade_kill, chain_kill, soft_kill
from fomo_api import FomoAuthError
from pick import pick, size_factor_for
from secret_utils import safe_err
import shadow_ledger
import collect  # For fallback access
from thresholds import HARD, SOFT

CHAIN_SET     = {1399811149: "solana", 56: "bsc", 8453: "bsc", 4663: "robinhood"}
CYCLE_SECONDS = 900
GT_CALLS_PER_MIN = int(os.environ.get("GT_CALLS_PER_MIN", "5"))  # ~5/min for keyless GT
GT_MIN_SPACING_SEC = float(os.environ.get("GT_MIN_SPACING_SEC", str(60.0 / GT_CALLS_PER_MIN)))  # ~12s between calls
GT_DOSSIER_RESERVE = max(1, GT_CALLS_PER_MIN - 4) if GT_CALLS_PER_MIN >= 5 else 1  # reserve 1 for dossiers (5/min -> 1, 10/min -> 6)
GT_UNIVERSE_BUDGET = int(os.environ.get("GT_UNIVERSE_BUDGET", "5"))  # universe scan budget per cycle
CYCLE_TIME_BUDGET_SEC = float(os.environ.get("CYCLE_TIME_BUDGET_SEC", "660"))  # ~11 minutes per cycle
DEX_BUDGET    = int(os.environ.get("DEX_BUDGET", "60"))  # DexScreener calls per cycle, raised from 25
log = logging.getLogger("desk")

# Injectable clock and sleep for tests (module-level so tests can patch)
_now = time.time
_sleep = time.sleep

# Shared GT rate limiter for the entire process (not per-cycle)
_gt_limiter = GTRateLimiter(calls_per_min=GT_CALLS_PER_MIN, min_spacing_sec=GT_MIN_SPACING_SEC)


class JudgeDown(Exception):
    """The judge is unreachable or a question set is malformed. The cycle stands down."""


def _fmt_evm_exclusions(excluded: list[tuple[str, float, str]]) -> str:
    """Format EVM holder exclusions for logging.
    
    Args:
        excluded: List of (address, pct, reason) tuples where pct is 0-100
    
    Returns:
        Formatted string like "0x1234...pair_multicall:20.0%, 0x5678...burn:5.0%"
    """
    if not excluded:
        return "none"
    return ", ".join(f"{addr[:6]}...{reason}:{pct:.1f}%" for addr, pct, reason in excluded)


def run_once(fomo, judge, desk, bank, shadow=True, gt_dossier_reserve=GT_DOSSIER_RESERVE, 
             gt_limiter=None, gt_universe_budget=GT_UNIVERSE_BUDGET, cycle_time_budget=CYCLE_TIME_BUDGET_SEC):
    mode_str = "SHADOW" if shadow else "LIVE"
    judge_str = "MOCK" if os.environ.get("JUDGE_MOCK") == "1" else "LIVE"
    log.info("cycle start: mode=%s judge=%s", mode_str, judge_str)
    
    # Track cycle start time and budget
    cycle_start = _now()
    cycle_deadline = cycle_start + cycle_time_budget
    
    # Clear Solana owner cache at start of each cycle
    _clear_sol_owner_cache()
    
    # Item #13: Prune old EVM holder cache entries
    try:
        import evm_holders
        evm_holders.prune_old_cache(book.DB)
    except Exception as e:
        log.warning("EVM holder cache prune failed: %s", e)
    
    # Mark all open shadow positions to market (uses FOMO, not GT)
    if shadow:
        try:
            shadow_ledger.mark_all_open_positions(fomo)
        except Exception as e:
            log.warning("shadow mark failed: %s", e)
    
    if (h := book.held()):                       # RISK owns the desk right now
        log.info("holding %s for %.0f min, no scan this cycle",
                 h["ticker"], h["minutes"])
        return None, {"held": h["ticker"], "minutes": round(h["minutes"])}

    stats = {"seen": 0, "benched": 0, "free": {}, "trade": {},
             "chain": {}, "soft": {}, "judged": 0, "tokens": [], "requeued": 0,
             "gt_universe": 0, "gt_dossier_attempts": 0, "gt_dossier_ok": 0, "gt_retry": 0,
             "free_passed": 0, "dossiered": 0}
    survivors = []
    dex_slots = DEX_BUDGET
    dex_degraded = False  # track DexScreener health
    free_passers = []  # tokens that passed free checks (for circuit breaker)
    budget_exhausted = False  # track if Dex or time budget exhausted
    carry_candidates = []  # free-passers after budget exhaustion
    
    # Use shared GT rate limiter (default to global singleton)
    if gt_limiter is None:
        gt_limiter = _gt_limiter
    
    # Reset cycle stats
    gt_limiter.reset_cycle_stats()
    
    # Set universe budget for this cycle (prevents bunching)
    gt_limiter.set_universe_budget(gt_universe_budget)
    
    # Reserve slots for dossiers (logged inside reserve())
    gt_limiter.reserve(gt_dossier_reserve)

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

    ids, gt_txns_cache = universe(limiter=gt_limiter, fomo=fomo)  # fresh + trending pools + FOMO feeds, budget-aware
    book.expire_defer()                          # drop rows past max age
    due = book.defer_due()                       # ids ready for rescoring
    
    # Age carry every cycle (including ids that shortlist drops), then retrieve
    book.age_carry()
    carry = book.get_carry()                     # unevaluated ids from previous cycle
    
    # Cap carry admission per cycle: leave room for due ids
    # Due ids get highest priority, so reserve budget for them
    carry_cap_this_cycle = max(0, min(len(carry), DEX_BUDGET * 2) - len(due))
    carry_admitted = carry[:carry_cap_this_cycle]
    
    if carry_admitted:
        log.info("carrying %d of %d unevaluated ids (cap=%d due to %d due ids)",
                 len(carry_admitted), len(carry), carry_cap_this_cycle, len(due))
    
    # Tag tids with tier for priority preservation: due=0, carry=1, universe=2
    tid_tier = {}
    for tid in due:
        tid_tier[tid] = 0
    for tid in carry_admitted:
        tid_tier[tid] = 1
    for tid in ids:
        tid_tier[tid] = 2
    
    seen_ids = set()
    combined_ids = []
    # Priority order: due, carry (admitted), GT, FOMO feeds
    # Cap at 200 ids to limit FOMO batch load
    for tid in due + carry_admitted + ids:
        if tid not in seen_ids:
            seen_ids.add(tid)
            combined_ids.append(tid)
            if len(combined_ids) >= 200:
                log.info("shortlist input capped at 200 ids (due=%d, carry=%d, universe=%d available)",
                         len(due), len(carry_admitted), len(ids))
                break
    
    shortlist_result = list(shortlist(fomo, combined_ids))
    shortlist_tids = {t["tid"] for t in shortlist_result}
    
    # Tag tokens with their tier for priority sorting
    for t in shortlist_result:
        t["_tier"] = tid_tier.get(t["tid"], 2)  # Default to universe tier
    
    for tid in due:
        if tid not in shortlist_tids:
            log.info("defer outcome tid=%s reason=miss", tid)
            book.forget_defer(tid)
    
    # Sort by: young tokens first (globally), then by tier within each age group
    # This ensures young tokens don't get starved, and within age groups due/carry/universe priority is preserved
    def sort_key(t):
        tier = t.get("_tier", 2)
        is_young = t.get("age_minutes", 0) < 60
        # Sort by: (not is_young globally, then tier within age group)
        return (not is_young, tier)
    
    age_prioritized = sorted(shortlist_result, key=sort_key)
    
    # Track young tokens that still need dossiers this cycle (for in-cycle retry logic)
    young_pending_dossier = []
    processed_tids = []  # Track tids whose loop iteration completed (to clear from carry)
    
    evaluated_count = 0  # Track how many tokens we evaluated before budget exhaustion
    for idx, t in enumerate(age_prioritized):    # pass one: free, no per-token requests
        stats["seen"] += 1
        t.setdefault("chain", CHAIN_SET.get(t["net"]))
        
        # Track this tid as processed at start of iteration
        # (will be removed from processed list if we break before completing)
        processed_tids.append(t["tid"])
        
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
        log.info("defer outcome tid=%s reason=pass", t["tid"])
        
        # Track free passers for circuit breaker and stats
        free_passers.append(t)
        stats["free_passed"] += 1

        # If budget exhausted, collect for carry and continue with next token
        if budget_exhausted:
            carry_candidates.append(t)
            continue
        
        # Check DexScreener budget (no waiting, just set flag and continue)
        if dex_slots <= 0:
            log.info("DexScreener budget exhausted at token %d, switching to carry mode", idx + 1)
            budget_exhausted = True
            carry_candidates.append(t)
            continue
        
        # Check cycle time budget before proceeding (leave headroom for judge/pick)
        time_remaining = cycle_deadline - _now()
        if time_remaining < 60:  # Need at least 60s for judge/pick phase
            log.info("cycle time budget exhausted at token %d, switching to carry mode (%.1fs remaining)", 
                     idx + 1, time_remaining)
            budget_exhausted = True
            carry_candidates.append(t)
            continue
        
        # Defer row cleared only after we confirm we're processing this token
        book.forget_defer(t["tid"])

        # Call trade_counts with new signature and GT fallback cache
        trade_data, dex_status = trade_counts(t, gt_txns_cache=gt_txns_cache)
        t.update(trade_data)
        t["dex_status"] = dex_status
        t["trades_source"] = "dex" if dex_status == "ok" else ("gt" if dex_status == "gt_fallback" else "none")
        dex_slots -= 1
        
        # Circuit breaker: check for consecutive empties (2-3 in a row)
        # If we see empties piling up, run canary check BEFORE benching
        if dex_status == "empty" and not dex_degraded:
            # Count consecutive empty results so far
            empty_count = sum(1 for fp in free_passers if fp.get("dex_status") == "empty")
            if empty_count >= 2:  # 2+ empties (current + previous)
                log.warning("DexScreener circuit breaker: %d consecutive empties, running canary check", empty_count + 1)
                canary_net = t.get("net", 1399811149)
                canary_healthy = dex_canary_check(canary_net)
                
                if not canary_healthy:
                    dex_degraded = True
                    log.warning("DexScreener DEGRADED: canary failed for net %s, "
                                "requeuing empties and using GT fallback where available", canary_net)
                    stats["dex_degraded"] = True
                    
                    # Requeue current token instead of benching
                    log.info("requeuing %s due to DexScreener degradation", t["ticker"])
                    now = time.time()
                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                    book.defer(t["tid"], now + 60, drop_at)
                    stats["requeued"] += 1
                    record(t, "trade", "requeued_dex_degraded")
                    continue
                else:
                    log.info("DexScreener canary healthy for net %s, empties are genuine", canary_net)
        
        if (k := trade_kill(t)):
            # Special handling for dex_error: requeue instead of bench
            if k == "dex_error":
                log.warning("trade tid=%s ticker=%s reason=%s age_minutes=%s trades_source=%s (DexScreener error, requeueing)",
                            t["tid"], t.get("ticker", "?"), k, t.get("age_minutes"), t.get("trades_source", "none"))
                now = time.time()
                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                book.defer(t["tid"], now + 60, drop_at)  # requeue after 1 min, no bench
                stats["requeued"] += 1
                log.info("defer outcome tid=%s reason=%s", t["tid"], k)
                book.forget_defer(t["tid"])
                record(t, "trade", k)
                continue
            
            # If DexScreener is degraded and this is a no_pair kill, requeue instead
            if k == "no_pair" and dex_degraded:
                log.info("requeuing %s (no_pair during DexScreener degradation)", t["ticker"])
                now = time.time()
                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                book.defer(t["tid"], now + 60, drop_at)
                stats["requeued"] += 1
                record(t, "trade", "requeued_dex_degraded")
                continue
            
            # Log trade kill with ticker, age, trades count, and pair
            log.info("trade tid=%s ticker=%s reason=%s age_minutes=%s trades_h24=%s buys_h1=%s sells_h1=%s dex_status=%s trades_source=%s",
                     t["tid"], t.get("ticker", "?"), k, t.get("age_minutes"), 
                     t.get("trades_h24"), t.get("buys_h1"), t.get("sells_h1"),
                     dex_status, t.get("trades_source", "none"))
            book.sit(t["tid"], k)
            log.info("defer outcome tid=%s reason=%s", t["tid"], k)
            book.forget_defer(t["tid"])
            stats["trade"][k] = stats["trade"].get(k, 0) + 1
            # Track fallback usage in stats
            if t.get("trades_source") == "gt":
                stats.setdefault("gt_fallback_count", 0)
                stats["gt_fallback_count"] += 1
            record(t, "trade", k)
            continue
        
        # Log passes with trades_source too
        log.info("trade tid=%s ticker=%s reason=pass dex_status=%s trades_source=%s trades_h24=%s",
                 t["tid"], t.get("ticker", "?"), dex_status, t.get("trades_source", "none"), t.get("trades_h24"))

        # Passed free & trade checks; track young tokens for in-cycle retry priority
        is_young = t.get("age_minutes", 0) < 60
        if is_young:
            young_pending_dossier.append(t)
        
        # Defensive guard: old token deferred while young tokens still pending dossier
        # With the try/finally fix (fe4bdaf), this should never fire since young tokens
        # are removed from pending list on all exit paths. Keep as safety check.
        if not is_young and young_pending_dossier:
            log.warning("defer old token %s (age %.1fm) while young tokens pending dossier (should not happen after fe4bdaf fix): %s", 
                        t["ticker"], t.get("age_minutes", 0), 
                        [yt.get("ticker", yt.get("tid")) for yt in young_pending_dossier])
            now = time.time()
            drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
            book.defer(t["tid"], now, drop_at)
            stats["requeued"] += 1
            record(t, "chain", "deferred_for_young")
            continue

        # Ensure young tokens are removed from pending list on ANY exit path
        try:
            d = None
            try:
                stats["gt_dossier_attempts"] += 1  # Track attempt before call
                # Check deadline before waiting (F5)
                time_before_wait = _now()
                if not gt_limiter.wait_if_needed(priority=True, deadline=cycle_deadline - 60):
                    # Would exceed deadline, defer this token
                    log.info("dossier for %s would exceed deadline, deferring", t["ticker"])
                    now = _now()
                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                    book.defer(t["tid"], now, drop_at)
                    stats["requeued"] += 1
                    record(t, "chain", "deferred_deadline")
                    continue
                if not gt_limiter.spend(1, priority=True):
                    # Budget exhausted after wait
                    log.warning("GT budget exhausted after wait for %s", t["ticker"])
                    now = _now()
                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                    book.defer(t["tid"], now, drop_at)
                    stats["requeued"] += 1
                    record(t, "chain", "requeued_budget")
                    continue
                
                d = dossier(t, limiter=gt_limiter)   # pass three: one GeckoTerminal slot (budget-aware)
                stats["gt_dossier_ok"] += 1  # Track successful dossier
                stats["dossiered"] += 1  # Track successfully dossiered tokens
            except DossierRetryNeeded as e:
                log.info("dossier retry needed for %s (age %.1fm): %s", 
                         t["ticker"], t.get("age_minutes", 0), e)
                
                # Young tokens get one in-cycle retry if backoff fits (<=30s cap)
                if is_young:
                    # Check if backoff fits in-cycle (30s cap for in-cycle retry)
                    now = time.time()
                    wait_cap_sec = 30.0
                    
                    if gt_limiter.backoff_until > now:
                        wait_needed = gt_limiter.backoff_until - now
                        
                        if wait_needed <= wait_cap_sec:
                            # Backoff fits, wait and retry
                            log.info("young token %s (age %.1fm) hit 429, waiting %.1fs for in-cycle retry", 
                                     t["ticker"], t.get("age_minutes", 0), wait_needed)
                            time.sleep(wait_needed)
                            
                            try:
                                stats["gt_dossier_attempts"] += 1  # Track retry attempt
                                stats["gt_retry"] += 1  # Track GT retry attempt
                                # Check deadline before retry wait
                                if not gt_limiter.wait_if_needed(priority=True, deadline=cycle_deadline - 60):
                                    log.info("young token %s retry would exceed deadline, deferring", t["ticker"])
                                    now = _now()
                                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                                    book.defer(t["tid"], now, drop_at)
                                    stats["requeued"] += 1
                                    record(t, "chain", "requeued_deadline")
                                    continue
                                if not gt_limiter.spend(1, priority=True):
                                    log.info("young token %s retry budget exhausted, deferring", t["ticker"])
                                    now = _now()
                                    drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                                    book.defer(t["tid"], now, drop_at)
                                    stats["requeued"] += 1
                                    record(t, "chain", "requeued_budget")
                                    continue
                                
                                d = dossier(t, limiter=gt_limiter)
                                stats["gt_dossier_ok"] += 1  # Also count as successful dossier
                                stats["dossiered"] += 1  # Successfully dossiered
                                log.info("young token %s dossier succeeded on in-cycle retry after %.1fs wait", 
                                         t["ticker"], wait_needed)
                            except DossierRetryNeeded as retry_e:
                                log.info("young token %s dossier failed on in-cycle retry, deferring: %s", 
                                         t["ticker"], retry_e)
                                now = time.time()
                                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                                book.defer(t["tid"], now, drop_at)
                                stats["requeued"] += 1
                                record(t, "chain", "requeued_after_retry")
                                continue
                            except Exception as retry_e:
                                log.warning("young token %s dossier retry failed: %s", t["ticker"], safe_err(retry_e))
                                book.sit(t["tid"], "dossier_failed")
                                log.info("defer outcome tid=%s reason=dossier_failed", t["tid"])
                                book.forget_defer(t["tid"])
                                record(t, "chain", "dossier_failed")
                                continue
                        else:
                            # Backoff too long, defer
                            log.info("young token %s (age %.1fm) hit 429, backoff %.1fs > cap %.1fs, deferring", 
                                     t["ticker"], t.get("age_minutes", 0), wait_needed, wait_cap_sec)
                            drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                            book.defer(t["tid"], now, drop_at)
                            stats["requeued"] += 1
                            record(t, "chain", "requeued_429_backoff")
                            continue
                    else:
                        # No backoff, try immediately
                        log.info("young token %s (age %.1fm) retrying dossier (no backoff)", 
                                 t["ticker"], t.get("age_minutes", 0))
                        try:
                            stats["gt_dossier_attempts"] += 1  # Track retry attempt
                            stats["gt_retry"] += 1  # Track GT retry attempt
                            # Check deadline before retry
                            if not gt_limiter.wait_if_needed(priority=True, deadline=cycle_deadline - 60):
                                log.info("young token %s retry would exceed deadline, deferring", t["ticker"])
                                now = _now()
                                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                                book.defer(t["tid"], now, drop_at)
                                stats["requeued"] += 1
                                record(t, "chain", "requeued_deadline")
                                continue
                            if not gt_limiter.spend(1, priority=True):
                                log.info("young token %s retry budget exhausted, deferring", t["ticker"])
                                now = _now()
                                drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                                book.defer(t["tid"], now, drop_at)
                                stats["requeued"] += 1
                                record(t, "chain", "requeued_budget")
                                continue
                            
                            d = dossier(t, limiter=gt_limiter)
                            stats["gt_dossier_ok"] += 1  # Also count as successful dossier
                            stats["dossiered"] += 1  # Successfully dossiered
                            log.info("young token %s dossier succeeded on immediate retry", t["ticker"])
                        except DossierRetryNeeded as retry_e:
                            log.info("young token %s dossier failed on immediate retry, deferring: %s", 
                                     t["ticker"], retry_e)
                            now = time.time()
                            drop_at = now + max(0, (HARD["max_age_hours"] * 60 - t["age_minutes"]) * 60)
                            book.defer(t["tid"], now, drop_at)
                            stats["requeued"] += 1
                            record(t, "chain", "requeued_after_retry")
                            continue
                        except Exception as retry_e:
                            log.warning("young token %s dossier retry failed: %s", t["ticker"], safe_err(retry_e))
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
                log.warning("dossier failed %s: %s", t["ticker"], safe_err(e))
                book.sit(t["tid"], "dossier_failed")
                log.info("defer outcome tid=%s reason=dossier_failed", t["tid"])
                book.forget_defer(t["tid"])
                record(t, "chain", "dossier_failed")
                continue                             # missing is missing, not a pass
            
            # If we get here without a dossier, it was a non-retry exception path
            if d is None:
                continue

            if (k := chain_kill(d)):
                # Log chain kill with holder metrics
                # Item #12: Add EVM holder details
                if d.get("chain") != "solana":
                    # EVM chain - log holder details
                    evm_source = d.get("evm_holder_source", "N/A")
                    evm_raw_top1 = d.get("evm_holder_raw_top_wallet")
                    evm_raw_top10 = d.get("evm_holder_raw_top_10")
                    evm_excluded = d.get("evm_holder_excluded", [])
                    
                    # Format raw values
                    raw_str = f"raw_top1={evm_raw_top1*100:.1f}% raw_top10={evm_raw_top10:.1f}%" if evm_raw_top1 and evm_raw_top10 else "raw=N/A"
                    
                    # Format exclusion reasons (all of them with percentages)
                    exclusion_str = _fmt_evm_exclusions(evm_excluded)
                    
                    log.info("chain tid=%s ticker=%s reason=%s age_minutes=%s top_10_percent=%s top_wallet_percent=%s holder_count=%s "
                             "evm_source=%s %s excluded=%s",
                             d.get("tid"), d.get("ticker", "?"), k, d.get("age_minutes"),
                             d.get("top_10_percent"), d.get("top_wallet_percent"), d.get("holder_count"),
                             evm_source, raw_str, exclusion_str)
                else:
                    # Solana chain
                    log.info("chain tid=%s ticker=%s reason=%s age_minutes=%s top_10_percent=%s top_wallet_percent=%s pools_excluded=%s holder_count=%s",
                             d.get("tid"), d.get("ticker", "?"), k, d.get("age_minutes"),
                             d.get("top_10_percent"), d.get("top_wallet_percent"), 
                             d.get("pools_excluded", False), d.get("holder_count"))
                
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
                log.info("soft tid=%s ticker=%s reason=%s noul=%s age_minutes=%s top_10_percent=%s top_wallet_percent=%s developer_holding_percentage=%s holder_count=%s rpc_ok=%s soft_scores=%s",
                         d.get("tid"), d.get("ticker"), reason, noul, d.get("age_minutes"),
                         d.get("top_10_percent"), d.get("top_wallet_percent"), d.get("developer_holding_percentage"),
                         d.get("holder_count"), d.get("rpc_ok"),
                         {k: round(v, 3) for k, v in soft_scores.items()})
                book.sit(t["tid"], reason, age_minutes=t.get("age_minutes"))
                log.info("defer outcome tid=%s reason=%s", t["tid"], reason)
                book.forget_defer(t["tid"])
                stats["soft"][reason] = stats["soft"].get(reason, 0) + 1
                record(d, "soft", reason, soft_noul=noul, soft_scores=soft_scores)
                continue

            record(d, "judged", None)
            survivors.append((d, ans))
        finally:
            # Remove from young_pending_dossier on any exit path (success, defer, bench, exception)
            if is_young and t in young_pending_dossier:
                young_pending_dossier.remove(t)

    # Clear carry for all processed tokens (any outcome: benched, killed, deferred, evaluated)
    book.clear_carry(processed_tids)
    
    # Save carry candidates (free-passers after budget exhaustion) in priority order
    if carry_candidates:
        # Cap at 60, keep highest priority (first in age_prioritized order)
        carry_tids = [t["tid"] for t in carry_candidates[:60]]
        dropped = len(carry_candidates) - len(carry_tids)
        
        log.info("unevaluated %d ids (dex_slots=%d, time_remaining=%.1fs)",
                 len(carry_candidates), dex_slots, max(0, cycle_deadline - _now()))
        book.save_carry(carry_tids)
        
        if dropped > 0:
            log.info("carry cap: saved %d, dropped %d lowest-priority", len(carry_tids), dropped)
    
    # Calculate cycle duration and GT stats
    cycle_duration = _now() - cycle_start
    
    # Count universe GT calls (from limiter's universe budget usage)
    stats["gt_universe"] = gt_limiter.universe_calls_used
    
    # Get carried count
    unevaluated_count = len(book.get_carry())
    
    # Keep existing log line for external monitoring (F6)
    log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "
             "trade %(trade)s, chain %(chain)s, soft %(soft)s, judged %(judged)s, requeued %(requeued)s", stats)
    
    # New comprehensive cycle logging
    log.info("cycle summary: seen=%d benched=%d free_passed=%d dossiered=%d judged=%d requeued=%d carried=%d",
             stats["seen"], stats["benched"], stats["free_passed"], 
             stats["dossiered"], stats["judged"], stats["requeued"], unevaluated_count)
    log.info("cycle GT calls: universe=%d dossier_attempts=%d dossier_ok=%d retry=%d 429s=%d wait_time=%.1fs",
             stats["gt_universe"], stats["gt_dossier_attempts"], stats["gt_dossier_ok"], stats["gt_retry"],
             gt_limiter.stats_429_count, gt_limiter.stats_wait_time)
    log.info("cycle duration: %.1fs / %.1fs budget", cycle_duration, cycle_time_budget)

    if not survivors:
        return None, stats
    
    # All survivors (including single survivor) go through pick gates
    try:
        order = pick(judge, survivors)
    except Exception as e:
        log.warning("pick failed (NO PICK): %s", safe_err(e))
        order = None  # Failed pick means no order this cycle

    if order is None:
        return None, stats
    order["order_id"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if shadow:
        # Get current FOMO data for the picked token to pass to shadow ledger
        picked_token = order["token"]
        # Find the token in shortlist_result to get FOMO data
        picked_fomo_data = None
        for t in shortlist_result:
            if t.get("addr") == picked_token["address"] and t.get("net") == picked_token["network_id"]:
                picked_fomo_data = {
                    "price": t.get("price"),
                    "vol24": t.get("volume_h24"),
                    "mcap": t.get("mcap_usd"),
                }
                break
        desk.log_shadow(order, stats, fomo_data=picked_fomo_data)  # written, never sent
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
    # Wire fomo client to desk for health tracking
    desk.fomo_client = fomo
    
    next_cycle_start = _now()  # First cycle starts immediately
    
    while True:
        cycle_start_actual = _now()
        try:
            fomo.token()                         # Privy bearer lives ~60 min, refresh it
            order, stats = run_once(fomo, judge, desk, desk.bank(), shadow)
            desk.report(order, stats)            # every cycle, trade or not
            if order:
                desk.send_to_seats(order)
        except FomoAuthError as e:
            log.error("FOMO auth failed: %s", e)
            # Write error state with FOMO error flag for ops panel banner
            desk.write_state(None, {
                "error": f"FomoAuthError: {e}",
                "fomo_error": str(e),
                "seen": 0,
                "benched": 0,
                "judged": 0,
                "free": {},
                "trade": {},
                "chain": {},
                "soft": {},
                "tokens": []
            })
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
        
        # Start-to-start scheduling: next cycle starts CYCLE_SECONDS from this cycle's start
        next_cycle_start += CYCLE_SECONDS
        now = _now()
        sleep_time = next_cycle_start - now
        
        if sleep_time > 0:
            log.info("sleeping %.1fs until next cycle (start-to-start: %.1fs)", 
                     sleep_time, CYCLE_SECONDS)
            _sleep(sleep_time)
        else:
            log.warning("cycle overran by %.1fs, starting next cycle immediately", -sleep_time)
            next_cycle_start = now  # Reset to avoid drift
