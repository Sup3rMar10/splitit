from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import services as svc
from .db import connect, init_db, tx
from .money import Rates, exponent
from .ratelimit import RateLimiter
from .schemas import ExpenseIn, GroupIn, JoinIn, LoginIn, RegisterIn, SettlementIn

ROOT = Path(__file__).resolve().parent.parent
COOKIE = "session"

# Bounded path ids: a 30-digit id would overflow SQLite and turn into a 500.
GroupId = Annotated[int, PathParam(ge=1, le=2**53)]
ExpenseId = Annotated[int, PathParam(ge=1, le=2**53)]

# No inline script or style, nothing external: user text can't execute even if it reached the DOM.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_app(db_path: str | None = None, rates_path: str | None = None, *, hasher: PasswordHasher | None = None,
               auth_limit: tuple[float, float] = (10, 1 / 6), secure_cookies: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("SPLITIT_DB", str(ROOT / "splitit.db"))
    rates = Rates.load(rates_path or ROOT / "data" / "rates.json")
    hasher = hasher or PasswordHasher()
    secure = secure_cookies if secure_cookies is not None else os.environ.get("SPLITIT_SECURE_COOKIES") == "1"
    init_db(db_path)

    app = FastAPI(title="Splitit", docs_url=None, redoc_url=None, openapi_url=None)
    auth_limiter = RateLimiter(*auth_limit)   # per client IP: register, login
    join_limiter = RateLimiter(*auth_limit)   # per user: invite-code guesses

    # --- plumbing ---

    def get_db():
        db = connect(db_path)
        try:
            yield db
        finally:
            db.close()

    def current_user(request: Request, db=Depends(get_db)) -> dict:
        user = svc.user_for_token(db, request.cookies.get(COOKIE))
        if user is None:
            raise svc.AppError(401, "Not signed in")
        return user

    def ip_limited(request: Request) -> None:
        if not auth_limiter.allow(request.client.host if request.client else "?"):
            raise svc.AppError(429, "Too many attempts, slow down")

    @app.exception_handler(svc.AppError)
    async def app_error(_: Request, e: svc.AppError):
        return JSONResponse({"detail": e.message}, status_code=e.status)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # CSRF defence in depth on top of SameSite=Lax + JSON-only bodies: a browser
        # that sends an Origin on a write must be sending it from our own site.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def set_session(response: Response, db, user_id: int) -> None:
        token = svc.create_session(db, user_id)
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure,
                            max_age=svc.SESSION_TTL, path="/")

    # --- auth ---

    @app.post("/api/register", status_code=201, dependencies=[Depends(ip_limited)])
    def register(body: RegisterIn, response: Response, db=Depends(get_db)):
        with tx(db):
            user = svc.create_user(db, hasher, body.email, body.name, body.password)
            set_session(response, db, user["id"])
        return user

    @app.post("/api/login", dependencies=[Depends(ip_limited)])
    def login(body: LoginIn, response: Response, db=Depends(get_db)):
        user = svc.authenticate(db, hasher, body.email, body.password)
        with tx(db):
            set_session(response, db, user["id"])
        return user

    @app.post("/api/logout", status_code=204)
    def logout(request: Request, response: Response, db=Depends(get_db)):
        with tx(db):
            svc.delete_session(db, request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/currencies")
    def currencies(user=Depends(current_user)):
        return {"base": rates.base, "as_of": rates.as_of,
                "currencies": [{"code": c, "exponent": exponent(c)} for c in rates.currencies()]}

    # --- groups ---

    @app.get("/api/groups")
    def my_groups(user=Depends(current_user), db=Depends(get_db)):
        return svc.list_groups(db, user["id"])

    @app.post("/api/groups", status_code=201)
    def new_group(body: GroupIn, user=Depends(current_user), db=Depends(get_db)):
        with tx(db):
            return svc.create_group(db, rates, user["id"], body.name, body.currency)

    @app.post("/api/groups/join")
    def join(body: JoinIn, user=Depends(current_user), db=Depends(get_db)):
        if not join_limiter.allow(str(user["id"])):
            raise svc.AppError(429, "Too many attempts, slow down")
        with tx(db):
            return svc.join_group(db, user["id"], body.code)

    @app.get("/api/groups/{gid}")
    def group(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        svc.require_member(db, gid, user["id"])
        return svc.group_detail(db, gid)

    # --- expenses ---

    @app.get("/api/groups/{gid}/expenses")
    def expenses(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        svc.require_member(db, gid, user["id"])
        return svc.list_expenses(db, gid)

    @app.post("/api/groups/{gid}/expenses", status_code=201)
    def add_expense(gid: GroupId, body: ExpenseIn, response: Response, user=Depends(current_user), db=Depends(get_db),
                    idempotency_key: str | None = Header(default=None)):
        svc.require_member(db, gid, user["id"])  # checked before any replay, so a stale key can't bypass it
        data = body.model_dump()

        def write():
            eid = svc.add_expense(db, rates, gid, user["id"], data)
            return 201, svc.get_expense(db, gid, eid)

        status, payload, replayed = svc.run_idempotent(
            db, user["id"], idempotency_key, f"POST /groups/{gid}/expenses", data, write)
        response.status_code = status
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return payload

    @app.delete("/api/groups/{gid}/expenses/{eid}", status_code=204)
    def remove_expense(gid: GroupId, eid: ExpenseId, user=Depends(current_user), db=Depends(get_db)):
        with tx(db):
            svc.delete_expense(db, gid, eid, user["id"])

    # --- balances, settlements, activity, export ---

    @app.get("/api/groups/{gid}/balances")
    def balances(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        g = svc.require_member(db, gid, user["id"])
        return svc.balances_view(db, gid, g["currency"])

    @app.get("/api/groups/{gid}/settlements")
    def settlements(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        svc.require_member(db, gid, user["id"])
        return svc.list_settlements(db, gid)

    @app.post("/api/groups/{gid}/settlements", status_code=201)
    def add_settlement(gid: GroupId, body: SettlementIn, response: Response, user=Depends(current_user),
                       db=Depends(get_db), idempotency_key: str | None = Header(default=None)):
        svc.require_member(db, gid, user["id"])
        data = body.model_dump()

        def write():
            sid = svc.add_settlement(db, gid, user["id"], data["from_user"], data["to_user"], data["amount_minor"])
            return 201, {"id": sid, **data}

        status, payload, replayed = svc.run_idempotent(
            db, user["id"], idempotency_key, f"POST /groups/{gid}/settlements", data, write)
        response.status_code = status
        if replayed:
            response.headers["Idempotent-Replay"] = "true"
        return payload

    @app.get("/api/groups/{gid}/activity")
    def activity(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        svc.require_member(db, gid, user["id"])
        return svc.activity_list(db, gid)

    @app.get("/api/groups/{gid}/export.csv")
    def export(gid: GroupId, user=Depends(current_user), db=Depends(get_db)):
        g = svc.require_member(db, gid, user["id"])
        return Response(svc.export_csv(db, gid, g["currency"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="splitit-group-{gid}.csv"'})

    # --- static client ---

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    return app
