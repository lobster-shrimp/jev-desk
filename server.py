"""
One uvicorn process for the bots to reach: the judge plus two book endpoints RISK needs,
because the Grok Bots run in xAI's cloud and the book (desk.db) lives on this machine.

    uvicorn server:app --host 0.0.0.0 --port 8080
    cloudflared tunnel --url http://localhost:8080

  POST /judge          the judge (see judge.py)
  GET  /book/held      {"held": {...}|null}
  POST /book/release   RISK calls this the moment a close is filled. Nothing else does.
  GET  /ops            ops panel HTML
  GET  /api/state      outbox/state.json for ops panel
"""
import json
import os
import pathlib

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import book
from judge import app, DESK_SECRET

HERE = pathlib.Path(__file__).parent
OUTBOX = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))
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


@app.get("/ops", response_class=HTMLResponse)
async def ops_panel():
    """Serve the ops panel HTML."""
    html_path = HERE / "ops.html"
    if not html_path.exists():
        raise HTTPException(404, "ops.html not found")
    return html_path.read_text()


@app.get("/api/state")
async def api_state():
    """Serve outbox/state.json for the ops panel."""
    state_path = OUTBOX / "state.json"
    if not state_path.exists():
        return JSONResponse({"demo": False, "tokens": [], "cycle": None}, status_code=404)
    return JSONResponse(json.loads(state_path.read_text()))
