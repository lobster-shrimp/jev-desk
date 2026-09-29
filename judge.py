"""
JUDGE — the only process that holds the TypeSafe (Jev) key.

Runs on one machine, answers typed questions, makes no trading decision.
Bots authenticate with DESK_SECRET and never see the TypeSafe key.

    pip install fastapi uvicorn typesafe-sdk
    export TYPESAFE_API_KEY="ts-..."
    export DESK_SECRET="$(openssl rand -hex 24)"
    uvicorn judge:app --host 0.0.0.0 --port 8080
    cloudflared tunnel --url http://localhost:8080     # bots run in xAI's cloud

Set JUDGE_MOCK=1 to run without a TypeSafe key (deterministic fake answers,
for testing the funnel and for the shadow week before you have a key).
"""
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from questions import SETS

log = logging.getLogger("judge")

DESK_SECRET = os.environ.get("DESK_SECRET")  # for the bots. NOT the TypeSafe key.
if not DESK_SECRET:
    raise SystemExit("DESK_SECRET is not set. export DESK_SECRET=\"$(openssl rand -hex 24)\"")

if os.environ.get("JUDGE_MOCK") == "1":
    from mock_judge import MockTypeSafeClient
    client = MockTypeSafeClient()
    log.warning("JUDGE_MOCK=1: answers are synthetic, not from Jev")
else:
    from typesafe_sdk import AsyncTypeSafeClient
    client = AsyncTypeSafeClient()             # reads TYPESAFE_API_KEY itself

app = FastAPI(title="desk judge")


class Ask(BaseModel):
    question_set: str
    state: dict


@app.get("/health")
async def health():
    return {"ok": True, "sets": sorted(SETS), "mock": os.environ.get("JUDGE_MOCK") == "1"}


@app.post("/judge")
async def judge(ask: Ask, authorization: str = Header("")):
    if authorization != f"Bearer {DESK_SECRET}":
        raise HTTPException(401, "bad desk secret")
    if ask.question_set not in SETS:
        raise HTTPException(422, f"unknown question set {ask.question_set}")

    qs = SETS[ask.question_set]
    qs = qs(ask.state) if callable(qs) else qs        # pick builds options at call time

    try:
        r = await client.system_one(state=ask.state, questions=qs)
    except Exception as e:                            # surface the upstream status, never guess
        status = getattr(e, "status_code", None) or getattr(e, "status", None)
        if status in (401, 422, 429, 529):
            raise HTTPException(status, str(e))
        raise HTTPException(502, f"judge upstream failed: {e}")

    # raw answers out. never flattened, never thresholded here.
    log.info("judge %s model=%s tokens=%s", ask.question_set, r.model, r.usage.input_tokens)
    return {"model": r.model,
            "answers": {k: v.model_dump() for k, v in r.answers.items()},
            "usage": r.usage.model_dump()}
