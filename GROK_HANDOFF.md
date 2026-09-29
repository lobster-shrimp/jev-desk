# Grok Bot handoff

Written 2026-09-29 against `main` at `1ee89a5`. Repo: https://github.com/lobster-shrimp/jev-desk (private).

This document is the operating brief for whoever runs the desk from here. It covers what
exists, what is proven, what is not, how to deploy it, and what to settle before any real
money moves. `prompts/HANDOFF.txt` is a different file: it is the text pasted above each
judging seat's prompt. This one is for the person or agent running the deployment.

## 1. What this is

A memecoin launch desk. Code fetches, Jev (a TypeSafe model) judges, code decides.

```
0. UNIVERSE  GeckoTerminal new_pools, 3 chains          -> fresh launches
1. LIST      FOMO filterTokens, 20 per call             -> hundreds, one batch
2. FREE CUT  age, liquidity, volume, mcap. No network   -> tens
3. TRADE CUT DexScreener buys and sells, one per token  -> a handful
4. DOSSIER   GeckoTerminal info + chain RPC + X         -> three per cycle
5. JUDGE     market + chain + social per token          -> scored shortlist
6. PICK      one choice over the shortlist              -> one token, or none
```

A finished order then goes SIZE -> FILLS -> RISK, with CHIEF logging. Every threshold is
in `thresholds.py` and is the original guide author's, not tuned to you.

## 2. Status

**Built and tested (16 tests, CI green on Python 3.10-3.14):**
filter kill order, question wire shape, pick gating, book invariants, two faked cycles,
collector helpers. Dependencies are pinned. Assertions were mutation-verified to fail when
the code they cover breaks.

**Not proven. Nothing in this repo has touched a live market.**

- Every network path. `universe`, `shortlist`, `trade_counts` and `dossier` are faked in
  tests. `fomo_api.py` (CDP, Privy token, refresh) has no coverage, and the FOMO
  `filterTokens` response envelope is undocumented and unverified.
- The real judge. `judge.py` and `server.py` never run under test, so `DESK_SECRET` auth
  and the `/book/*` endpoints are unproven. `mock_judge.py` stands in everywhere.
- The `social` question set. The test desk returns no X account, so that branch is never
  entered.
- Live mode. Every cycle test is shadow or held, so `book.take(order)` is never reached.
- Base chain routing (net 8453 uses the `bsc` set on purpose; fixtures are Solana only).
- `desk.py` and `run.py`'s flags.

**Not deployed.** No judge is running, no tunnel exists, no `.env` has been created.

## 3. What runs where

Two processes on one machine (yours), plus bots in xAI's cloud.

| piece | where | what it does |
|---|---|---|
| judge (`server.py`) | your machine | Holds the only TypeSafe key. Serves `/judge` and `/book/*`. |
| shift (`run.py`) | your machine | Runs the whole funnel in Python. Needs Chrome logged in to FOMO, or `FOMO_BEARER`. |
| `desk.db` | your machine | The book. The judge process and the shift process **must point at the same file**. The path is `$DESK_DB`, default `desk.db` relative to the working directory, so start both from the repo root or set `DESK_DB` to one absolute path. Started from different directories, `/book/release` frees a book the shift never reads. |
| SOCIAL, CHIEF, SIZE, FILLS, RISK bots | xAI cloud | Reach the judge only over the tunnel. They never see the repo or the TypeSafe key. |

The shift does the scanning and judging itself. The bots are the read-X seat and the
execution seats; they receive orders from the shift, they do not generate them.

## 4. Deployment, in order

**1. Judge.** On your machine, from the repo root:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY="ts-..."                 # console.typesafe.ai -> Keys
export DESK_SECRET="$(openssl rand -hex 24)"     # what the bots get. NOT the key.
uvicorn server:app --host 127.0.0.1 --port 8080
```

Bind `127.0.0.1`, not the README's `0.0.0.0`. `cloudflared` connects locally, so nothing
needs the LAN to reach the judge.

**2. Tunnel.** `cloudflared tunnel --url http://localhost:8080` prints a URL. Two values
matter and they are not the same:

- `BASE` = `https://<tunnel>` (no path)
- `JUDGE_URL` = `$BASE/judge`

A quick tunnel gets a **new URL every restart**, which silently breaks every bot skill
that hard-coded the old one. Use a named tunnel for anything that stays up.

**3. Prove the key, then prove the link from a bot's terminal, not your laptop.** Use the
two `curl` commands in README section 1. Stop if any of these is off: `answers.shape.choice`
not one of the listed options, probabilities not summing to about 1, or `model` being an
alias instead of a version string such as `jev-1.13.0`. Also `curl $BASE/health` should
report `"mock": false`.

**4. Shift.** In a second terminal, same repo root so `desk.db` is shared:

```bash
export JUDGE_URL="$BASE/judge" DESK_SECRET="..." BANK_USD=1000
# FOMO: Chrome with --remote-debugging-port=9222 and a fomo.family tab open,
#       or set FOMO_BEARER (expires in about an hour)
python run.py --once        # one shadow cycle; read the log and outbox/shadow.jsonl
python run.py               # shadow, every 15 min
```

Optional wiring: `SOCIAL_URL` (endpoint the SOCIAL bot serves), `SEATS_WEBHOOK_URL`
(CHIEF's inbox; unset means orders are written to `outbox/orders/`),
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**5. Seats.** Paste `prompts/HANDOFF.txt` above SCAN, VET, SOCIAL and CHIEF, then each
seat's own file. SIZE, FILLS and RISK get no judge call and no key. Record one judge call
by hand in front of Grok Bot and save it as a skill; skills are shared across the desk.

## 5. What each seat may and may not do

- **Judging seats** (SCAN, VET, SOCIAL, CHIEF): build a state, call the judge once, act on
  the numbers. Never call `api.typesafe.ai`. Never ask for a question set that is not
  theirs. Never retry a 422. A missing answer is missing, not neutral.
- **SOCIAL**: one X account in, one X block out. Never summarise posts, never substitute a
  similar handle. No handle means `{"x_account": null}`.
- **SIZE**: arithmetic only. Multiply by `size_factor` **once**. The "dark cuts to 0.40,
  missing X cuts to 0.60" line in `prompts/SIZE.txt` describes how `size_factor` was built.
  `pick.py` has already applied both, so SIZE reapplying them double-cuts the ticket.
- **FILLS**: fee floor check first, then one market order through FOMO, nowhere else.
- **RISK**: polls every 5 minutes, closes on `volume.h6 / (volume.h24 / 4) < 0.20`, and
  closes anyway if it cannot measure. After a filled close it must release the book (next
  section).
- **CHIEF**: works the order in sequence, logs the order id, model id and every answer,
  sends one line to Telegram. No order is also an order: send the line with the reason.

## 6. The order contract

The order the shift hands over has this shape. Two cases differ from `prompts/CHIEF.txt`:

```json
{
  "order_id": "2026-09-29T12:00:00Z",
  "model": "jev-1.13.0",
  "token": {"ticker": "...", "address": "...", "network_id": 1399811149, "chain": "solana"},
  "size_factor": 0.6,
  "confidence": 0.78,
  "runner_up": [["T0", 0.131]],
  "why": {"shape": {"type": "choice", "choice": "crowd", "probabilities": {}}, "...": "raw wire objects"}
}
```

- **Single-survivor orders** have `"model": "single-survivor"` and `"confidence": null`.
  Anything that formats or compares `confidence` must handle `null`.
- **`why` holds raw judge answers** (`{"type": "noul", "noul": 0.14}`), not the flattened
  `crowd_p` / `authority_risk` fields the CHIEF prompt example shows. Nobody reads `why` as
  an input, so this only affects logging, but a parser written from the example will break.

## 7. Book control

The desk does not scan while a position is held. Only RISK frees it.

```bash
curl $BASE/book/held    -H "Authorization: Bearer $DESK_SECRET"     # {"held": {...} | null}
curl -X POST $BASE/book/release -H "Authorization: Bearer $DESK_SECRET"
```

If a close happened and nothing reported it, the desk is stalled, not broken. Run the
release by hand after confirming in FOMO that the position is actually closed.

## 8. Shadow first, then live

Shadow mode (`python run.py`, the default) does everything except take the book and hand
the order over. It writes one row per would-be trade to `outbox/shadow.jsonl`. Fill in
`your_call` by hand and read only the rows where you disagree; that is where your
thresholds come from. Leave it a week.

In shadow the seats receive nothing, so SIZE, FILLS and RISK are unexercised until live.
Plan a separate dry run of that path at minimum size before trusting it.

The shadow week only means something against the real judge. `JUDGE_MOCK=1` and
`--mock-judge` return deterministic synthetic answers that carry no information about any
token; they are for testing the funnel, never for tuning.

Go live only with `CONFIRM_LIVE=yes python run.py --live`, after the week.

## 9. Settle before live

1. **Every bot holds `DESK_SECRET`, and it authorises `/book/release`.** "Only RISK
   releases" is a convention, not an enforced rule. Any seat, or anything that leaks the
   secret, can free the book mid-position. Consider a separate release secret.
2. **Can a Grok Bot serve an HTTP endpoint?** `SOCIAL_URL` and `SEATS_WEBHOOK_URL` both
   assume so. This was not checked. If they cannot, SOCIAL is permanently absent (every
   ticket shrinks by `NO_SOCIAL_CUT`) and orders only reach the seats through
   `outbox/orders/`.
3. **Nothing exercises the FOMO order path.** FILLS sends real market orders and no test
   or dry run has.
4. **RISK's volume source is unspecified.** The prompt needs `volume.h24` and `volume.h6`
   but does not say where they come from.
5. **`order_id` is a per-second timestamp.** Two orders in the same second share an id.

Resolved: `prompts/RISK.txt` used to tell RISK to call the Python function
`book.release()`, which the cloud bots cannot reach. It now has RISK `POST` to
`/book/release` with `DESK_SECRET`, release only when flat, retry on failure, and report
the book as still held rather than claim it is free if the call never returns 200.

## 10. If something breaks

| symptom | meaning |
|---|---|
| 401 from `/judge` | wrong `DESK_SECRET` |
| 422 from `/judge` | malformed question. Never retry. The cycle stops (`JudgeDown`). |
| 429 GeckoTerminal | back off a full minute; persisting, set `universe(pages=1)` |
| 429 DexScreener | lower `DEX_BUDGET` in `main.py` |
| FOMO 401/403 | bearer expired; refreshed from Chrome automatically |
| every token "malformed row" | FOMO envelope differs; print one raw row and adjust `fomo_api._row` |
| "HOLDING ... no scan" forever | a close was never released (section 7) |
| bots suddenly get connection errors | tunnel restarted and the URL changed |
| judge unreachable | the cycle stands down. No guessing. |

`python run.py --bench` prints what is benched and why.

## 11. Secrets

- `TYPESAFE_API_KEY` lives on the judge machine only. No bot gets it, ever.
- `DESK_SECRET` is what bots get. Rotate it if it leaks: restart the judge and the shift
  with a new value, then update every bot skill.
- `.env` is gitignored; `.env.example` holds placeholders only.
