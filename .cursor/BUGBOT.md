# Bugbot Review Rules for jev-desk

This memecoin launch desk fetches, judges with TypeSafe Jev, and trades. Bugbot reviews should enforce desk-specific conventions and operational safety.

## Core Principles

### 1. Shadow-First, No Demo Data
- Never invent or commit fake cycle data in `outbox/state.json` or ops panel state
- The `demo: false` constraint must hold for all real runs
- Test fixtures in `tests/` may fake data, but never production state files

### 2. Secrets Never Touch the Repo
Flag any PR that adds, logs, or commits:
- `.env` files (already gitignored, but catch attempts to track them)
- `DESK_SECRET` values in code, logs, or test output
- FOMO/Privy bearer tokens (`FOMO_BEARER`, CDP extraction results)
- Telegram bot tokens (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- TypeSafe API keys (`TYPESAFE_API_KEY`)
- Any credential in plaintext comments or docstrings

### 3. State Sources: Real Over Marketing
- Prefer `/api/state`, `outbox/state.json`, and `/ops` panel for observability
- The ops panel (`ops.html` + `server.py /api/state`) shows real cycle data only
- Flag PRs that bypass structured state endpoints in favor of ad-hoc logging

## Kill Stage Integrity

### 4. Four-Stage Filter Order (Free → Trade → Chain → Soft)
The desk applies kills in strict cost order. Respect existing gates:
- **Free kills**: age (15 min – 72 hr), liquidity ≥$12k, volume ≥$40k, mcap $60k–$8M
- **Trade kills**: ≥150 trades/24h, sells must exist if >20 buys/1h
- **Chain kills**: top wallet ≤5% (Solana), top 10 ≤60%, holders ≥80, authority closed, not honeypot
- **Soft kills**: judge answer thresholds in `thresholds.py`

Flag PRs that:
- Reorder kill stages (breaks cost discipline)
- Change numeric thresholds without corresponding test updates in `tests/test_filter.py`
- Skip gates conditionally without clear justification

### 5. Bench Durations Are Intentional
`book.py` defines `BENCH_MINUTES` per kill reason:
- Facts (honeypot, authority_open, concentration) bench for ~69 days (100,000 min)
- Social checks bench for 6 hours (360 min)
- Momentum checks bench for 25 minutes

Flag accidental changes to these durations without explanation.

## Network Resilience

### 6. FOMO/GeckoTerminal Retry and 429 Handling
Existing backoff and per-network stop-paging behavior is hardening, not cruft:
- `collect.py` stops pagination on 429 per network, preserves other chains' data
- `fomo_api.py` refreshes Privy tokens hourly via CDP or fails gracefully
- GeckoTerminal budget: 6 slots for universe (3 chains × 2 pages), 3 for dossiers
- DexScreener budget: `DEX_BUDGET = 25` per cycle

Flag PRs that:
- Remove 429 handling or backoff logic
- Retry 422 responses (malformed questions are permanent failures)
- Increase pagination without accounting for rate limits

### 7. RISK Book Release Must Stay HTTP
`server.py` `/book/release` endpoint is the only correct way to free the desk:
- RISK seat calls `POST $JUDGE_URL/../book/release` with `Authorization: Bearer $DESK_SECRET`
- Never reintroduce direct `book.release()` calls from bot prompts (bots run in xAI cloud, cannot import Python)
- The book SQLite file (`desk.db`) must be shared by judge and shift processes

## Python Environment

### 8. Python 3.10+ Floor and Lockfile Discipline
- `requirements.txt` is a compiled lock: never hand-edit, always recompile from `requirements.in`
- CI runs `python -m pytest` on 3.10–3.14 and checks lockfile freshness
- Recompilation command: `uv pip compile requirements.in -o requirements.txt --universal --python-version 3.10`

Flag PRs that:
- Edit `requirements.txt` directly instead of `requirements.in` + recompile
- Lower the Python floor below 3.10

## Trading Safety

### 9. Shadow/Mock Guards for Live Execution Paths
Flag PRs that introduce or modify executable trading/live-order paths without:
- Clear `shadow=True` default in `main.py` or flags in `run.py`
- `CONFIRM_LIVE=yes` environment guard for `--live` mode
- Corresponding tests in `tests/` that exercise the new path with mocked collectors/judge
- Documentation update in README or GROK_HANDOFF.md explaining the change

Examples: adding new seat handoff logic, modifying `book.take()`, changing FILLS order execution.

### 10. Single-Survivor vs Pick Paths
Both produce orders, both apply size factors:
- **Single survivor**: no `pick()` call, `model: "single-survivor"`, `confidence: null`
- **Multiple survivors**: `pick()` returns order after passing `worth_trading_at_all ≥ 0.60` and `confidence ≥ 0.55`

Size factors (`DARK_TICKET_CUT`, `NO_SOCIAL_CUT`) are applied in `pick.py` for both paths. Flag PRs that reapply them in SIZE seat logic (double-cuts the ticket).

## Questions and Thresholds

### 11. Question Sets Are Immutable Without Schema Tests
The desk asks three question sets per token: `market`, `solana`/`bsc`/`robinhood`, and optionally `social`.
- `questions.py` defines every question structure
- `tests/test_questions.py` asserts wire shape

Flag PRs that:
- Add or change questions without updating `tests/test_questions.py`
- Change question `type` (noul/score/choice) without updating `filter.soft_kill()` thresholds

### 12. Thresholds Live in One Place
All numeric gates live in `thresholds.py`:
- Filter limits (age, liquidity, volume, mcap, trades)
- Soft kill thresholds (concentration, momentum, social)
- Pick gates (`PICK_MIN_WORTH`, `PICK_MIN_CONF`)
- Size factors (`DARK_TICKET_CUT`, `NO_SOCIAL_CUT`)

Flag PRs that hardcode thresholds elsewhere.
