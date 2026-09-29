"""
One uvicorn process for the bots to reach: the judge plus two book endpoints RISK needs,
because the Grok Bots run in xAI's cloud and the book (desk.db) lives on this machine.

    uvicorn server:app --host 0.0.0.0 --port 8080
    cloudflared tunnel --url http://localhost:8080

  POST /judge          the judge (see judge.py)
  GET  /book/held      {"held": {...}|null}
  POST /book/release   RISK calls this the moment a close is filled. Nothing else does.
"""
from fastapi import APIRouter, Header, HTTPException

import book
from judge import app, DESK_SECRET

router = APIRouter(prefix="/book")


def _auth(authorization: str):
    if authorization != f"Bearer {DESK_SECRET}":
        raise HTTPException(401, "bad desk secret")


@router.get("/held")
async def held(authorization: str = Header("")):
    _auth(authorization)
    return {"held": book.held()}


@router.post("/release")
async def release(authorization: str = Header("")):
    """RISK only. The seat that closed the position is the seat that frees the book."""
    _auth(authorization)
    was = book.held()
    book.release()
    return {"released": was}


app.include_router(router)
