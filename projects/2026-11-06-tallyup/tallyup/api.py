"""FastAPI application: JSON API under /api plus the static TypeScript UI at /."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .money import CURRENCY_EXPONENT, format_amount
from .settle import greedy_plan, optimal_plan
from .store import IdempotencyMismatch, Invalid, Ledger, NotFound, VersionConflict

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupIn(Strict):
    name: str
    currency: str = "USD"


class MemberIn(Strict):
    name: str


class SplitPart(Strict):
    member_id: int
    value: Optional[str | int] = None


class SplitIn(Strict):
    kind: Literal["equal", "shares", "percent", "exact"]
    parts: list[SplitPart] = Field(min_length=1, max_length=100)


class ExpenseIn(Strict):
    description: str
    payer_id: int
    amount: str = Field(description='Decimal string, e.g. "12.30". Numbers are rejected on purpose.')
    split: SplitIn


class PreviewIn(ExpenseIn):
    expense_id: Optional[int] = None


class PaymentIn(Strict):
    from_member: int
    to_member: int
    amount: str


_ETAG_RE = re.compile(r'^(?:W/)?"?(\d+)"?$')


def _version(if_match: Optional[str]) -> int:
    if if_match is None:
        raise HTTPException(428, "If-Match header with the expense version is required")
    m = _ETAG_RE.match(if_match.strip())
    if not m:
        raise HTTPException(400, "If-Match must be a version number such as \"3\"")
    return int(m.group(1))


def _idem(key: Optional[str]) -> Optional[str]:
    if key is not None and not (1 <= len(key) <= 200):
        raise HTTPException(400, "Idempotency-Key must be 1-200 characters")
    return key


def create_app(db_path: str | None = None, *, drift_aware: bool = True) -> FastAPI:
    ledger = Ledger(db_path or os.environ.get("TALLYUP_DB", "tallyup.db"), drift_aware=drift_aware)
    app = FastAPI(title="tallyup", version="1.0.0")
    app.state.ledger = ledger

    @app.exception_handler(NotFound)
    async def _nf(_, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Invalid)
    async def _inv(_, exc: Invalid):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(IdempotencyMismatch)
    async def _idm(_, exc: IdempotencyMismatch):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(VersionConflict)
    async def _vc(_, exc: VersionConflict):
        headers = {"ETag": f'"{exc.current}"'} if exc.current is not None else {}
        return JSONResponse({"detail": "expense was changed by someone else; reload and retry",
                             "current_version": exc.current}, status_code=412, headers=headers)

    # ------------------------------------------------------------------ API
    @app.post("/api/groups", status_code=201)
    def create_group(body: GroupIn):
        return ledger.create_group(body.name, body.currency)

    @app.get("/api/groups/{gid}")
    def get_group(gid: int):
        return ledger.get_group(gid)

    @app.post("/api/groups/{gid}/members", status_code=201)
    def add_member(gid: int, body: MemberIn):
        return ledger.add_member(gid, body.name)

    @app.post("/api/groups/{gid}/preview")
    def preview(gid: int, body: PreviewIn):
        return ledger.preview_split(gid, body.model_dump())

    @app.get("/api/groups/{gid}/expenses")
    def list_expenses(gid: int):
        return ledger.list_expenses(gid)

    @app.post("/api/groups/{gid}/expenses", status_code=201)
    def create_expense(gid: int, body: ExpenseIn, response: Response,
                       idempotency_key: Optional[str] = Header(None)):
        out, replayed = ledger.create_expense(gid, body.model_dump(), _idem(idempotency_key))
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        response.headers["ETag"] = f'"{out["version"]}"'
        return out

    @app.get("/api/expenses/{eid}")
    def get_expense(eid: int, response: Response):
        out = ledger.get_expense(eid)
        response.headers["ETag"] = f'"{out["version"]}"'
        return out

    @app.put("/api/expenses/{eid}")
    def update_expense(eid: int, body: ExpenseIn, response: Response,
                       if_match: Optional[str] = Header(None)):
        out = ledger.update_expense(eid, body.model_dump(), _version(if_match))
        response.headers["ETag"] = f'"{out["version"]}"'
        return out

    @app.delete("/api/expenses/{eid}", status_code=204)
    def delete_expense(eid: int, if_match: Optional[str] = Header(None)):
        ledger.delete_expense(eid, _version(if_match))
        return Response(status_code=204)

    @app.post("/api/groups/{gid}/payments", status_code=201)
    def record_payment(gid: int, body: PaymentIn, response: Response,
                       idempotency_key: Optional[str] = Header(None)):
        out, replayed = ledger.record_payment(gid, body.model_dump(), _idem(idempotency_key))
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return out

    @app.get("/api/groups/{gid}/payments")
    def list_payments(gid: int):
        return ledger.list_payments(gid)

    @app.get("/api/groups/{gid}/balances")
    def balances(gid: int):
        g = ledger.get_group(gid)
        exp = g["exponent"]
        bal = ledger.balances(gid)
        drift = ledger.rounding_drift(gid)
        names = {m["id"]: m["name"] for m in g["members"]}
        return {
            "currency": g["currency"],
            "balances": [{"member_id": m, "name": names[m], "balance": format_amount(b, exp),
                          "balance_minor": b, "rounding_drift_minor": float(drift.get(m, 0))}
                         for m, b in bal.items()],
            "sum_minor": sum(bal.values()),
        }

    @app.get("/api/groups/{gid}/settle")
    def settle(gid: int, method: Literal["optimal", "greedy"] = Query("optimal")):
        g = ledger.get_group(gid)
        exp = g["exponent"]
        names = {m["id"]: m["name"] for m in g["members"]}
        bal = ledger.balances(gid)
        greedy = greedy_plan(bal)
        plan = greedy if method == "greedy" else optimal_plan(bal)
        return {
            "method": plan.method,
            "transfers": [{"from_member": t.from_member, "from_name": names[t.from_member],
                           "to_member": t.to_member, "to_name": names[t.to_member],
                           "amount": format_amount(t.amount, exp), "amount_minor": t.amount}
                          for t in plan.transfers],
            "transfer_count": len(plan.transfers),
            "greedy_transfer_count": len(greedy.transfers),
        }

    @app.get("/api/currencies")
    def currencies():
        return CURRENCY_EXPONENT

    # ------------------------------------------------------------------- UI
    if (WEB_DIR / "dist").is_dir():
        app.mount("/dist", StaticFiles(directory=WEB_DIR / "dist"), name="dist")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    def styles():
        return FileResponse(WEB_DIR / "styles.css")

    return app

