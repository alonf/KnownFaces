import asyncio
import base64
import hmac
import json
import os
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Thread

import httpx
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

from knownfaces.app import create_app
from knownfaces.jobs import deliver, process_job, submit, task_info
from knownfaces.models import verified
from knownfaces.store import Conflict, Store
from knownfaces.vision import MAX_BYTES, Vision
from scripts.fixtures import corpus, photos
from marker_vision import MarkerVision, frame


@pytest.fixture(scope="module")
def vision():
    return MarkerVision() if os.getenv("KF_TEST_MARKERS") == "1" else Vision()


@pytest.fixture(scope="module")
def images():
    if os.getenv("KF_TEST_MARKERS") == "1":
        return [frame(i) for i in range(1, 5)]
    return photos()


@pytest.fixture(scope="module")
def frames(vision, images):
    if isinstance(vision, MarkerVision):
        return [frame(), frame(1), frame(1, 2, 3, 4)]
    return corpus(vision, images)


def test_models(vision, frames, tmp_path):
    wrong = tmp_path / "wrong"
    wrong.write_bytes(b"invalid")
    with pytest.raises(RuntimeError, match="checksum"):
        verified(wrong, "0" * 64)
    for n, data in zip((0, 1, 4), frames):
        _, faces = vision.inspect(data)
        assert len(faces) == n
        assert all(
            0 <= f.box[0] < 1920 and 0 <= f.box[1] < 1080 and 0.8 < f.score <= 1
            for f in faces
        )
    for bad in (b"bad", b"x" * (MAX_BYTES + 1)):
        with pytest.raises(ValueError):
            vision.inspect(bad)


def test_store(vision, frames, tmp_path, images):
    path = tmp_path / "store.db"
    db = Store(path, vision, "http://localhost/callback")
    at = "2026-01-05T12:00:00Z"
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(lambda _: db.recognize(frames[1], "door", at, "event"), range(3))
        )
    assert results[0] == results[1] == results[2]
    pid = results[0]["faces"][0]["person_id"]
    assert results[0]["faces"][0]["previous_seen_at"] is None
    with pytest.raises(Conflict):
        db.recognize(frames[0], "door", at, "event")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
        conn.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.recognize(frames[2], "door", at, "rollback")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        conn.execute("DROP TRIGGER fail")
    old = db.recognize(frames[1], "garden", "2026-01-03T00:00:00Z")
    assert (
        old["faces"][0]["person_id"] == pid
        and old["faces"][0]["previous_seen_at"] is None
    )
    db.rename(pid, "Visitor")
    db = Store(path, vision)
    group = db.recognize(frames[2], "door", "2026-01-06T00:00:00Z")
    known = next(f for f in group["faces"] if f["person_id"] == pid)
    assert (
        known["full_name"] == "Visitor"
        and known["previous_seen_at"] == results[0]["captured_at"]
    )
    assert len({f["person_id"] for f in group["faces"]}) == 4
    assert len(db.companions(pid)["cooccurrences"]) == 3
    assert (
        len(db.sightings(name="Visitor", camera_id="door", limit=2)["sightings"]) == 2
    )
    first = db.sightings(person_id=pid, limit=1)
    assert (
        db.sightings(person_id=pid, cursor=first["next_cursor"], limit=1)["sightings"][
            0
        ]["snapshot_id"]
        == results[0]["snapshot_id"]
    )
    assert (
        db.sightings(person_id=pid, start="2026-01-03T00:00:00Z", end=at)["sightings"][
            0
        ]["snapshot_id"]
        == old["snapshot_id"]
    )
    second = db.enroll(images[0], 0, full_name="Visitor")["person_id"]
    assert len(db.sightings(name="Visitor")["candidates"]) == 2
    ambiguous = db.recognize(frames[1], "door")["faces"][0]
    assert ambiguous["person_id"] not in (pid, second)
    db.reassign(ambiguous["observation_id"], second)
    assert db.sightings(person_id=second)["sightings"]
    db.rename(second, None)
    assert db.people(name="Visitor")["people"][0]["person_id"] == pid


def test_http(vision, images, frames, tmp_path):
    app = create_app(tmp_path, vision)
    keys = json.loads((tmp_path / "credentials.json").read_text())

    def headers(role):
        return {"Authorization": "Bearer " + keys[role]}

    with TestClient(app) as client:
        assert client.get("/v1/people").status_code == 401
        data = {"files": {"image": ("frame.jpg", frames[2])}}
        res = client.post(
            "/v1/snapshots",
            headers=headers("ingest"),
            data={"camera_id": "front"},
            **data,
        ).json()
        sid = res["snapshot_id"]
        assert (
            client.get(f"/v1/snapshots/{sid}/image", headers=headers("read")).content
            == frames[2]
        )
        pid = res["faces"][0]["person_id"]
        path = f"/v1/people/{pid}/name"
        assert (
            client.put(
                path, json={"full_name": "Name"}, headers=headers("read")
            ).status_code
            == 403
        )
        assert client.post(
            "/login", json={"token": keys["admin"]}, headers={"X-KnownFaces-UI": "1"}
        ).is_success
        assert client.put(path, json={"full_name": "Name"}).status_code == 403
        assert client.put(
            path, json={"full_name": "Name"}, headers={"X-KnownFaces-UI": "1"}
        ).is_success
        assert client.get("/v1/people/" + pid).json()["full_name"] == "Name"
        assert client.delete(path, headers={"X-KnownFaces-UI": "1"}).is_success
        choices = client.post(
            "/v1/references/preview", headers=headers("admin"), **data
        ).json()
        assert len(choices["faces"]) == 4
        enroll = client.post(
            "/v1/references",
            headers=headers("admin"),
            data={"index": "2", "full_name": "Reference"},
            **data,
        ).json()
        assert not client.get(
            "/v1/sightings",
            params={"person_id": enroll["person_id"]},
            headers=headers("read"),
        ).json()["sightings"]
        assert (
            client.post(
                "/v1/snapshots",
                headers=headers("ingest"),
                files={"image": ("bad", b"?")},
                data={"camera_id": "test"},
            ).status_code
            == 422
        )


def test_delivery(vision, frames, tmp_path):
    store = Store(tmp_path / "gallery.db", vision, "http://localhost/hook")
    captured = store.recognize(frames[0], "door")
    attempts = []

    def receiver(request):
        attempts.append(request)
        if len(attempts) <= 8:
            raise httpx.ReadTimeout("Test receiver timed out")
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receiver)) as client:
        for _ in range(8):
            deliver(store, "secret", client)
            with store.connection() as conn:
                conn.execute("UPDATE deliveries SET next_at=0")
        with store.connection() as conn:
            assert (
                conn.execute("SELECT state FROM deliveries").fetchone()[0] == "failed"
            )
            conn.execute("UPDATE deliveries SET state='pending',attempts=0")
        deliver(Store(store.path, vision, store.webhook), "secret", client)
    assert len(attempts) == 9 and len({r.content for r in attempts}) == 1
    assert all(
        r.headers["X-KnownFaces-Event"] == captured["snapshot_id"] for r in attempts
    )
    assert (
        attempts[-1].headers["X-KnownFaces-Signature"]
        == "sha256=" + hmac.new(b"secret", attempts[-1].content, sha256).hexdigest()
    )


def test_task_recovery(vision, frames, tmp_path):
    path = tmp_path / "gallery.db"
    store = Store(path, vision)
    handle = submit(
        store,
        "ingest",
        {"camera_id": "ring", "image_base64": base64.b64encode(frames[1]).decode()},
    )
    assert process_job(Store(path, vision))
    assert (
        task_info(Store(path, vision), "ingest", handle["taskId"])["result"][
            "structuredContent"
        ]["face_count"]
        == 1
    )


@pytest.fixture
def live(tmp_path, vision, request, monkeypatch):
    if getattr(request, "param", None) == "pause_jobs":
        monkeypatch.setattr("knownfaces.app.process_job", lambda store: False)
    app = create_app(tmp_path, vision)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield (
            url,
            json.loads((tmp_path / "credentials.json").read_text()),
            app.state.store,
        )
    finally:
        server.should_exit = True
        thread.join(8)
        sock.close()


def test_sdk(live, images, frames):
    url, keys, store = live
    pid = store.enroll(images[0], 0, full_name="Visitor")["person_id"]

    async def scenario():
        async def choose(ctx, params):
            return types.ElicitResult(action="accept", content={"person_id": pid})

        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + keys["admin"]}
        ) as http:
            transport = streamable_http_client(url + "/mcp", http_client=http)
            async with Client(transport, elicitation_callback=choose) as client:
                for n in range(2):
                    args = {
                        "camera_id": "ring",
                        "image_base64": base64.b64encode(frames[2]).decode(),
                        "event_id": str(n),
                    }
                    response = await client.call_tool("recognize_snapshot", args)
                    assert not response.is_error, response.content
                    faces = response.structured_content["faces"]
                    assert len(faces) == 4 and sum("full_name" in f for f in faces) == 1
                replay = await client.call_tool("recognize_snapshot", args)
                assert replay.structured_content == response.structured_content
                store.enroll(images[1], 0, full_name="Visitor")
                history = await client.call_tool(
                    "find_sightings", {"name": "Visitor", "limit": 2}
                )
                assert not history.is_error, history.content
                value = history.structured_content or json.loads(
                    history.content[0].text
                )
                assert len(value["sightings"]) == 2

    asyncio.run(scenario())


def rpc(url, key, method, params, capabilities=None, ident=1):
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": capabilities or {},
    }
    headers = {
        "MCP-Method": method,
        "MCP-Protocol-Version": "2026-07-28",
        "Authorization": "Bearer " + key,
        "Accept": "application/json, text/event-stream",
    }
    name = params.get("name") or params.get("taskId")
    if name:
        headers["Mcp-Name"] = name
    res = httpx.post(
        url + "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": ident,
            "method": method,
            "params": dict(params, _meta=meta),
        },
    )
    if res.is_error and "error" not in res.json():
        raise AssertionError(res.text)
    return res


def test_tasks_wire(live, frames):
    url, keys, store = live
    caps = {"extensions": {"io.modelcontextprotocol/tasks": {}}}
    result = rpc(
        url,
        keys["ingest"],
        "tools/call",
        {
            "name": "recognize_snapshot",
            "arguments": {
                "camera_id": "ring",
                "image_base64": base64.b64encode(frames[1]).decode(),
                "deferred": True,
            },
        },
        caps,
    ).json()
    assert result.get("result", {}).get("resultType") == "task", result
    task = result["result"]["taskId"]
    assert "error" in rpc(url, keys["read"], "tasks/get", {"taskId": task}, caps).json()
    stream = rpc(
        url,
        keys["ingest"],
        "subscriptions/listen",
        {"notifications": {"taskIds": [task]}},
        caps,
    ).text
    assert (
        "notifications/subscriptions/acknowledged" in stream
        and "notifications/tasks" in stream
    ), stream
    assert (
        rpc(url, keys["ingest"], "tasks/get", {"taskId": task}, caps).json()["result"][
            "result"
        ]["structuredContent"]["face_count"]
        == 1
    )


def test_mrtr_decisions(live, images):
    url, keys, store = live
    for photo in images[:2]:
        store.enroll(photo, 0, full_name="Duplicate")
    call = {"name": "find_sightings", "arguments": {"name": "Duplicate", "limit": 2}}
    caps = {"elicitation": {"form": {}}}
    fallback = rpc(url, keys["read"], "tools/call", call).json()["result"][
        "structuredContent"
    ]
    assert len(fallback["candidates"]) == 2
    for action in ("cancel", "decline", "accept"):
        r = rpc(url, keys["read"], "tools/call", call, caps).json()["result"]
        assert r["resultType"] == "input_required"
        answer = {"action": action}
        if action == "accept":
            answer["content"] = {"person_id": "outside-choices"}
        retry = dict(
            call, requestState=r["requestState"], inputResponses={"identity": answer}
        )
        response = rpc(url, keys["read"], "tools/call", retry, caps).json()["result"]
        if action == "accept":
            assert response["isError"]
        else:
            assert response["structuredContent"]["status"] == action
    assert "error" in rpc(url, keys["admin"], "tools/call", retry, caps).json()
    retry["requestState"] += "invalid"
    assert "error" in rpc(url, keys["read"], "tools/call", retry, caps).json()


def test_history_ties(vision, frames, tmp_path):
    store = Store(tmp_path / "ties.db", vision)
    stamp = "2026-11-24T12:00:00Z"
    left = store.recognize(frames[2], "left", stamp)
    right = store.recognize(frames[2], "right", stamp)
    pid = left["faces"][0]["person_id"]
    first = store.sightings(person_id=pid, limit=1)
    second = store.sightings(person_id=pid, cursor=first["next_cursor"], limit=1)
    assert {
        first["sightings"][0]["snapshot_id"],
        second["sightings"][0]["snapshot_id"],
    } == {left["snapshot_id"], right["snapshot_id"]}
    assert all(r["count"] == 2 for r in store.companions(pid)["cooccurrences"])
    assert all(
        r["snapshot_ids"] == [right["snapshot_id"]]
        for r in store.companions(pid, camera_id="right")["cooccurrences"]
    )


@pytest.mark.parametrize("live", ["pause_jobs"], indirect=True)
def test_cancel_queued_job(live, frames):
    url, keys, store = live
    caps = {"extensions": {"io.modelcontextprotocol/tasks": {}}}
    handle = submit(
        store,
        "ingest",
        {"camera_id": "ring", "image_base64": base64.b64encode(frames[1]).decode()},
    )
    response = rpc(
        url, keys["ingest"], "tasks/cancel", {"taskId": handle["taskId"]}, caps
    ).json()
    assert response["result"]["status"] == "cancelled", response
