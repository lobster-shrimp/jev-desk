# Bugbot Review Rules for jev-desk

Memecoin launch desk: fetches, Jev judges, code decides. Enforce desk conventions and trading safety.

## Core Safety

**1. Shadow-First, No Demo Data**
- Never invent fake cycle data in `outbox/state.json` or ops state (`demo: false` for real runs)
- Test fixtures may fake data; production state files never

**2. Secrets Never Committed**
Flag PRs adding/logging: `.env`, `DESK_SECRET`, `FOMO_BEARER`, `TYPESAFE_API_KEY`, `TELEGRAM_BOT_TOKEN`, any credential in code/logs/comments

**3. Real State Over Dashboards**
Prefer `/api/state`, `outbox/state.json`, `/ops` panel. Flag ad-hoc logging bypassing structured endpoints.

## Kill Stages (Free → Trade → Chain → Soft)

**4. Filter Order Is Sacred**
Respect cost-ordered gates:
- **Free**: age 15min–72hr, liquidity ≥$12k, volume ≥$40k, mcap $60k–$8M
- **Trade**: ≥150 trades/24h, sells exist if >20 buys/1h (DexScreener)
- **Chain**: top wallet ≤5% (Solana), top10 ≤60%, holders ≥80, authority closed, not honeypot
- **Soft**: judge thresholds in `thresholds.py`

Flag: reordering stages, threshold changes without test updates, conditional gate skips

**5. Bench Durations Match Kill Severity**
Facts bench ~69 days, social 6hr, momentum 25min. Flag accidental duration changes.

## Network Hardening

**6. FOMO/Gecko 429 Handling Is Intentional**
- `collect.py`: stop-paging per network on 429, preserve other chains
- `fomo_api.py`: hourly token refresh via CDP
- Budgets: GT 6 universe + 3 dossiers, DexScreener 25/cycle

Flag: removing backoff, retrying 422 (malformed questions fail permanently), increasing pagination without budget accounting

**7. RISK Book Release Stays HTTP**
`POST /book/release` with `DESK_SECRET` is the only release path. Never reintroduce direct `book.release()` from bot prompts (xAI bots cannot import Python). Shared `desk.db` between judge and shift.

## Python Discipline

**8. Lockfile Integrity**
- `requirements.txt` = compiled lock from `requirements.in` (never hand-edit)
- Recompile: `uv pip compile requirements.in -o requirements.txt --universal --python-version 3.10`
- CI: pytest on 3.10–3.14, checks lockfile freshness

Flag: direct `requirements.txt` edits, lowering Python floor below 3.10

## Trading Paths

**9. Shadow/Mock Guards Required**
Flag PRs adding/modifying live trading without:
- `shadow=True` default, `CONFIRM_LIVE=yes` guard for `--live`
- Tests with mocked collectors/judge
- Docs update (README/GROK_HANDOFF.md)

Examples: seat handoff, `book.take()`, FILLS execution

**10. Size Factors Applied Once**
`DARK_TICKET_CUT` (0.40), `NO_SOCIAL_CUT` (0.60) applied in `pick.py` for both single-survivor and multi-survivor orders. Flag SIZE seat reapplication (double-cuts).

## Schema Stability

**11. Question Sets Require Tests**
Three sets: `market`, chain (`solana`/`bsc`/`robinhood`), optional `social`. `questions.py` + `tests/test_questions.py`.

Flag: adding/changing questions without test updates, changing question `type` without updating `filter.soft_kill()`

**12. Thresholds Centralized**
All numeric gates in `thresholds.py`: filter limits, soft kill thresholds, pick gates, size factors. Flag hardcoded thresholds elsewhere.
