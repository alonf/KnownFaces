import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .auth import Access
from .jobs import deliver, process_job, worker
from .protocol import create_mcp, preview
from .store import Conflict, Missing, Store, one
from .vision import MAX_BYTES, Vision


class Name(BaseModel):
    full_name: str


class Target(BaseModel):
    person_id: str


class Login(BaseModel):
    token: str


def create_app(directory=None, vision=None):
    directory = Path(directory or os.getenv("KF_DATA", "data"))
    access = Access(directory)
    webhook = os.getenv("KF_WEBHOOK") or None
    secret = os.getenv("KF_WEBHOOK_SECRET", "")
    if webhook and (not secret or urlsplit(webhook).scheme not in ("http", "https")):
        raise ValueError("Set a valid webhook URL and KF_WEBHOOK_SECRET")
    store = Store(directory / "knownfaces.db", vision or Vision(), webhook)
    mcp_app = create_mcp(store, access)

    @asynccontextmanager
    async def lifespan(app):
        with store.connection(write=True) as db:
            db.execute("UPDATE jobs SET status='working' WHERE status='running'")
        with httpx.Client(timeout=4, trust_env=False) as client:
            async with mcp_app.router.lifespan_context(mcp_app):
                workers = [asyncio.create_task(worker(process_job, store))]
                if webhook:
                    workers.append(
                        asyncio.create_task(worker(deliver, store, secret, client))
                    )
                try:
                    yield
                finally:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)

    app = FastAPI(title="KnownFaces", version="1.0", lifespan=lifespan)
    app.state.store = store
    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        if path.startswith(("/v1/", "/mcp")):
            try:
                if request.headers.get("authorization") or path.startswith("/mcp"):
                    role = access.principal(request.headers)
                else:
                    sid = request.cookies.get("kf_session", "")
                    if access.sessions.get(sid, 0) < time.time():
                        access.sessions.pop(sid, None)
                        raise PermissionError("Sign in")
                    if (
                        request.method not in ("GET", "HEAD")
                        and request.headers.get("x-knownfaces-ui") != "1"
                    ):
                        return JSONResponse(
                            {"detail": "Missing UI request header"}, 403
                        )
                    role = "admin"
                if path.startswith("/v1/"):
                    action = "read" if request.method == "GET" else "admin"
                    if path == "/v1/snapshots" and request.method == "POST":
                        action = "ingest"
                    if path.startswith("/v1/deliveries"):
                        action = "admin"
                    try:
                        access.allow(role, action)
                    except PermissionError as exc:
                        return JSONResponse({"detail": str(exc)}, 403)
            except PermissionError as exc:
                return JSONResponse(
                    {"detail": str(exc)}, 401, headers={"WWW-Authenticate": "Bearer"}
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        code = (
            404
            if isinstance(exc, Missing)
            else 409
            if isinstance(exc, Conflict)
            else 422
        )
        return JSONResponse({"detail": str(exc)}, code)

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/health")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ready", "backend": "OpenCV CPU"}

    @app.post("/login")
    def login(payload: Login, request: Request):
        if request.headers.get("x-knownfaces-ui") != "1":
            return JSONResponse({"detail": "Missing UI header"}, 403)
        try:
            access.allow(
                access.principal({"authorization": "Bearer " + payload.token}), "admin"
            )
        except PermissionError:
            return JSONResponse({"detail": "Invalid admin credential"}, 401)
        sid = secrets.token_urlsafe(32)
        access.sessions = {k: v for k, v in access.sessions.items() if v > time.time()}
        access.sessions[sid] = time.time() + 8 * 3600
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "kf_session",
            sid,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=8 * 3600,
        )
        return response

    @app.post("/v1/logout")
    def logout(request: Request):
        access.sessions.pop(request.cookies.get("kf_session", ""), None)
        response = Response(status_code=204)
        response.delete_cookie("kf_session")
        return response

    @app.post("/v1/snapshots")
    async def ingest(
        image: UploadFile = File(),
        camera_id: str = Form(),
        event_id: str | None = Form(None),
        captured_at: str | None = Form(None),
    ):
        data = await image.read(MAX_BYTES + 1)
        return await run_in_threadpool(
            store.recognize, data, camera_id, captured_at, event_id
        )

    @app.get("/v1/snapshots/{sid}")
    def snapshot(sid: str):
        return store.snapshot(sid)

    @app.get("/v1/snapshots/{sid}/image")
    def image(sid: str):
        with store.connection() as db:
            row = one(db, "SELECT image,media FROM snapshots WHERE id=?", (sid,))
        return Response(row[0], media_type=row[1])

    @app.get("/v1/samples/{sample}/crop")
    def crop(sample: str):
        with store.connection() as db:
            row = one(db, "SELECT crop FROM samples WHERE id=?", (sample,))
        return Response(row[0], media_type="image/jpeg")

    @app.get("/v1/people")
    def people(name: str | None = None, cursor: str = "", limit: int = 50):
        return store.people(name, after=cursor, limit=limit)

    @app.get("/v1/people/{person_id}")
    def person(person_id: str):
        rows = store.people(person_id=person_id)["people"]
        if not rows:
            raise Missing("Person not found")
        return rows[0]

    @app.get("/v1/sightings")
    def sightings(
        person_id: str | None = None,
        name: str | None = None,
        camera_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ):
        return store.sightings(person_id, name, camera_id, start, end, limit, cursor)

    @app.get("/v1/people/{person_id}/co-occurrences")
    def companions(
        person_id: str,
        camera_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ):
        return store.companions(person_id, camera_id, start, end)

    @app.put("/v1/people/{person_id}/name")
    def rename(person_id: str, payload: Name):
        return store.rename(person_id, payload.full_name)

    @app.delete("/v1/people/{person_id}/name")
    def clear(person_id: str):
        return store.rename(person_id, None)

    @app.put("/v1/observations/{observation_id}/person")
    def correct(observation_id: str, payload: Target):
        return store.reassign(observation_id, payload.person_id)

    @app.post("/v1/references/preview")
    async def inspect_reference(image: UploadFile = File()):
        return await run_in_threadpool(preview, store, await image.read(MAX_BYTES + 1))

    @app.post("/v1/references")
    async def enroll(
        image: UploadFile = File(),
        index: int = Form(),
        person_id: str | None = Form(None),
        full_name: str | None = Form(None),
    ):
        return await run_in_threadpool(
            store.enroll, await image.read(MAX_BYTES + 1), index, person_id, full_name
        )

    @app.get("/v1/deliveries")
    def deliveries():
        with store.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,state,attempts,error FROM deliveries ORDER BY rowid DESC LIMIT 100"
                )
            ]

    @app.post("/v1/deliveries/{sid}/retry")
    def retry(sid: str):
        with store.connection(write=True) as db:
            one(db, "SELECT id FROM deliveries WHERE id=?", (sid,))
            db.execute(
                "UPDATE deliveries SET state='pending',attempts=0,next_at=0,error=NULL WHERE id=?",
                (sid,),
            )
        return {"state": "pending"}

    app.mount("/", mcp_app)
    return app
