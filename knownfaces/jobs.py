import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time

import httpx

from .store import one, timestamp, uid
from .vision import MAX_BYTES

log = logging.getLogger(__name__)


def image_bytes(value):
    if len(value) > (MAX_BYTES + 2) // 3 * 4:
        raise ValueError("Image exceeds 12 MiB")
    return base64.b64decode(value, validate=True)


def recognize_args(store, args):
    return store.recognize(
        image_bytes(args["image_base64"]),
        args["camera_id"],
        args.get("captured_at"),
        args.get("event_id"),
    )


def task_info(store, owner, task_id):
    with store.connection() as db:
        row = one(db, "SELECT * FROM jobs WHERE id=? AND owner=?", (task_id, owner))
    status = "working" if row["status"] == "running" else row["status"]
    obj = dict(
        taskId=row["id"],
        status=status,
        createdAt=row["created"],
        lastUpdatedAt=row["updated"],
        ttlMs=None,
    )
    if row["result"]:
        result = json.loads(row["result"])
        obj["result"] = {
            "content": [{"type": "text", "text": row["result"]}],
            "structuredContent": result,
        }
    if row["error"]:
        obj["error"] = {"code": -32603, "message": row["error"]}
    return obj


def submit(store, owner, args):
    task_id, now = uid(), timestamp()
    args = dict(args)
    if args.get("event_id") is None:
        args["event_id"] = task_id
    with store.connection(write=True) as db:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,'working',?,?,?,NULL,NULL)",
            (task_id, owner, now, now, json.dumps(args)),
        )
    return dict(
        resultType="task",
        taskId=task_id,
        status="working",
        createdAt=now,
        lastUpdatedAt=now,
        ttlMs=None,
        pollIntervalMs=250,
    )


def process_job(store):
    # A single service worker owns this queue. Event idempotency covers restart after commit.
    with store.connection(write=True) as db:
        row = db.execute(
            "SELECT id,args FROM jobs WHERE status='working' ORDER BY created LIMIT 1"
        ).fetchone()
        if row:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
    if not row:
        return False
    try:
        result = recognize_args(store, json.loads(row["args"]))
        values = ("completed", json.dumps(result), None)
    except Exception as exc:
        values = ("failed", None, str(exc))
    with store.connection(write=True) as db:
        db.execute(
            "UPDATE jobs SET status=?,result=?,error=?,updated=?,args=NULL WHERE id=? AND status='running'",
            (*values, timestamp(), row["id"]),
        )
    return True


def deliver(store, secret, client):
    with store.connection() as db:
        row = db.execute(
            """SELECT d.*,s.result FROM deliveries d JOIN snapshots s ON s.id=d.id
                         WHERE d.state='pending' AND next_at<=? ORDER BY next_at LIMIT 1""",
            (time.time(),),
        ).fetchone()
    if not row:
        return False
    payload = row["result"].encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    try:
        response = client.post(
            row["url"],
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-KnownFaces-Event": row["id"],
                "X-KnownFaces-Signature": "sha256=" + signature,
            },
        )
        response.raise_for_status()
        state, error = "sent", None
    except httpx.HTTPError as exc:
        state = "failed" if row["attempts"] >= 7 else "pending"
        error = type(exc).__name__
    with store.connection(write=True) as db:
        db.execute(
            "UPDATE deliveries SET attempts=attempts+1,next_at=?,state=?,error=? WHERE id=?",
            (
                time.time() + min(3600, 2 ** (row["attempts"] + 1)),
                state,
                error,
                row["id"],
            ),
        )
    return True


async def worker(fn, *args):
    while True:
        try:
            busy = await asyncio.to_thread(fn, *args)
        except Exception:
            log.exception("Background worker failed")
            busy = False
        await asyncio.sleep(0 if busy else 0.25)
