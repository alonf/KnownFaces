"""Synthetic images, identities, tenant and signing key. No remote network or home data."""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from knownfaces.app import create_app
from knownfaces.remote import build_remote, create_remote
from knownfaces.remote_oauth import EntraVerifier
from knownfaces.remote_policy import RemotePolicy
from knownfaces.remote_store import RemoteStore
from knownfaces.retention import maintain
from knownfaces.store import Store, timestamp
from knownfaces.vision import Face


class SyntheticVision:
    def inspect(self, data):
        if data == b"empty":
            return "image/png", []
        return "image/png", [
            Face(
                [0, 0, 1, 1],
                0.91,
                np.eye(128, dtype=np.float32)[data[0] % 128],
                b"synthetic-crop",
            )
        ]


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def resource(tmp_path, signing_key):
    local = Store(tmp_path / "gallery.db", SyntheticVision())
    person = local.enroll(b"A", 0, full_name="Synthetic Visitor")["person_id"]
    stamps = [
        "2026-11-01T10:00:00Z",
        "2026-11-01T10:02:00Z",
        "2026-11-01T10:03:00Z",
        "2026-11-01T11:00:00Z",
        "2026-11-01T12:00:00Z",
    ]
    cameras = ["node-7", "node-4", "node-3", "node-5", "unmapped"]
    frames = [
        local.recognize(b"A", camera, stamp) for camera, stamp in zip(cameras, stamps)
    ]
    opaque = uuid4()
    policy = RemotePolicy(
        resource="https://agent.example.invalid/mcp",
        tenant_id=uuid4(),
        api_id=uuid4(),
        connector_id=uuid4(),
        authorized_user_oid=uuid4(),
        cameras={
            "node-4": "primary",
            "node-7": "yard",
            "node-3": "entrance",
            "node-5": "service",
        },
        subjects={"synthetic": person},
        snapshots={
            opaque: {
                "snapshot_id": frames[0]["snapshot_id"],
                "camera_id": "node-7",
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "enroll_for": "synthetic",
                "face_index": 0,
            }
        },
        recognize_enabled=True,
        manage_enabled=True,
    )
    store = RemoteStore(local.path, SyntheticVision(), policy)
    verifier = EntraVerifier(policy)
    jwk = jwt.PyJWK.from_dict(
        jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
        | {"kid": "synthetic-key", "use": "sig", "alg": "RS256"}
    )
    verifier.keys.get_signing_keys = lambda refresh=False: [jwk]

    def token(scope="knownfaces.read", **overrides):
        now = int(time.time())
        claims = {
            "iat": now,
            "exp": now + 600,
            "nbf": now - 1,
            "iss": policy.issuer,
            "aud": str(policy.api_id),
            "tid": str(policy.tenant_id),
            "ver": "2.0",
            "oid": str(policy.authorized_user_oid),
            "azp": str(policy.connector_id),
            "sub": "fixture",
            "scp": scope,
        }
        return jwt.encode(
            claims | overrides,
            signing_key,
            algorithm="RS256",
            headers={"kid": "synthetic-key"},
        )

    with TestClient(
        build_remote(policy, store, verifier), base_url="https://agent.example.invalid"
    ) as client:
        yield client, token, policy, store, opaque


def invoke(client, token, tool, args, modern=True):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
    }
    params = {"name": tool, "arguments": args}
    if modern:
        headers.update(
            {
                "Mcp-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": tool,
            }
        )
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "local-connector-fixture",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
    )


WINDOW = {
    "subject": "synthetic",
    "start": "2026-11-01T00:00:00Z",
    "end": "2026-11-02T00:00:00Z",
}


def test_discovery_and_surface(resource):
    c, token, p, _, _ = resource
    for path in [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ]:
        doc = c.get(path).json()
        assert doc["resource"] == p.resource and doc["authorization_servers"] == [
            p.issuer
        ]
    for path in [
        "/",
        "/login",
        "/static/app.js",
        "/docs",
        "/openapi.json",
        "/v1/people",
        "/v1/snapshots",
        "/v1/snapshots/x/image",
        "/v1/samples/x/crop",
        "/mcp/extra",
        "/token",
    ]:
        for method in ["GET", "POST"]:
            assert (
                c.request(
                    method, path, headers={"Authorization": "Bearer " + token()}
                ).status_code
                == 404
            )
    params = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "fixture", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    res = c.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token(),
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": "tools/list",
            "Mcp-Protocol-Version": "2026-07-28",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
    )
    tools = res.json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "visit_history",
        "identify_approved_snapshot",
        "rename_subject",
        "add_approved_reference",
    }
    assert all(t["securitySchemes"] == t["_meta"]["securitySchemes"] for t in tools)


def test_token_checks(resource):
    c, mint, p, _, _ = resource
    for bad in [
        None,
        "local-admin-key",
        mint(exp=1),
        mint(nbf=int(time.time()) + 3600),
        mint(iss="https://other.invalid"),
        mint(aud=str(uuid4())),
        mint(tid=str(uuid4())),
        mint(azp=str(uuid4())),
        mint(oid=str(uuid4())),
        mint(ver="1.0"),
        mint(idtyp="app"),
        mint(scp=None),
    ]:
        h = {"Authorization": "Bearer " + bad} if bad else {}
        response = c.post("/mcp", headers=h, json={})
        assert (
            response.status_code == 401
            and p.discovery_url in response.headers["WWW-Authenticate"]
        )
    untrusted = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        jwt.decode(mint(), options={"verify_signature": False}),
        untrusted,
        algorithm="RS256",
        headers={"kid": "synthetic-key"},
    )
    assert (
        c.post(
            "/mcp", headers={"Authorization": "Bearer " + forged}, json={}
        ).status_code
        == 401
    )


def test_read_and_denied_edit(resource):
    c, mint, _, store, ref = resource
    res = invoke(c, mint(), "visit_history", WINDOW)
    summary = res.json()["result"]["structuredContent"]
    assert summary["interpretation"] == "last_seen"
    assert [g["evidence_quality"] for g in summary["observations"]] == [
        "corroboration_only",
        "possible_visit",
    ]
    assert summary["observations"][1]["cameras"] == ["node-7", "node-4", "node-3"]
    assert summary["observations"][0]["cameras"] == ["node-5"]
    assert "unmapped" not in str(summary)
    empty = invoke(
        c,
        mint(),
        "visit_history",
        WINDOW | {"start": "2026-11-03T00:00:00Z", "end": "2026-11-04T00:00:00Z"},
    ).json()["result"]["structuredContent"]
    assert empty == {"interpretation": "no_observations_in_window", "observations": []}
    edit = {
        "subject": "synthetic",
        "request_id": str(uuid4()),
        "issued_at": timestamp(),
    }
    for tool, args in (
        ("identify_approved_snapshot", {"reference": str(ref)}),
        ("rename_subject", edit | {"full_name": "Denied name"}),
        ("add_approved_reference", edit | {"reference": str(ref)}),
    ):
        assert invoke(c, mint(), tool, args).status_code == 403
    with store.connection() as db:
        assert (
            db.execute("SELECT full_name FROM people").fetchone()[0]
            == "Synthetic Visitor"
        )
        assert db.execute("SELECT count(*) FROM remote_edits").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == 6
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 5
    # Legacy requests have no routing headers. The SDK dispatch check still enforces the exact tool's scope.
    legacy = invoke(c, mint(), "rename_subject", {}, modern=False)
    assert legacy.json()["result"]["isError"]
    assert (
        invoke(c, mint("knownfaces.recognize"), "visit_history", WINDOW).status_code
        == 403
    )
    assert invoke(c, mint(), "visit_history", WINDOW | {"limit": 21}).json()["result"][
        "isError"
    ]
    with store.connection() as db:
        log = list(db.execute("SELECT at,actor,tool,outcome FROM remote_audit"))
        assert any(r["outcome"] == "ok" for r in log) and any(
            r["outcome"] == "denied" for r in log
        )
        assert all(len(r["actor"]) == 64 and len(r.keys()) == 4 for r in log)


def test_approved_recognition_is_nonmutating(resource):
    c, mint, p, store, ref = resource

    def counts():
        with store.connection() as db:
            return tuple(
                db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("people", "samples", "snapshots", "deliveries", "jobs")
            )

    before = counts()
    for _ in range(2):
        res = invoke(
            c,
            mint("knownfaces.recognize"),
            "identify_approved_snapshot",
            {"reference": str(ref)},
        ).json()["result"]["structuredContent"]
        assert res["faces"] == [
            {
                "match": "synthetic",
                "evidence_quality": {"detection_score": 0.91, "similarity": 1.0},
            }
        ]
        assert set(res) == {"observed_at", "camera", "faces"}
    for bad in (str(uuid4()), "https://example.invalid/file.png"):
        assert invoke(
            c,
            mint("knownfaces.recognize"),
            "identify_approved_snapshot",
            {"reference": bad},
        ).json()["result"]["isError"]
    assert before == counts()
    # A local capture-born exemplar alone cannot enroll someone remotely.
    with store.connection(write=True) as db:
        db.execute("DELETE FROM samples WHERE snapshot_id IS NULL")
        db.execute("UPDATE metadata SET gallery_revision=gallery_revision+1")
    assert store.inspect_approved(ref)["faces"][0]["match"] == "unknown"


def test_snapshot_approval_is_specific(resource):
    _, _, policy, store, ref = resource
    original = policy.snapshots[ref]
    for overrides in (
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"camera_id": "node-4"},
    ):
        altered = original.model_copy(update=overrides)
        scoped = RemoteStore(
            store.path,
            store.vision,
            policy.model_copy(update={"snapshots": {ref: altered}}),
        )
        with pytest.raises(ValueError):
            scoped.inspect_approved(ref)
    comparison_only = original.model_copy(
        update={"face_index": None, "enroll_for": None}
    )
    scoped = RemoteStore(
        store.path,
        store.vision,
        policy.model_copy(update={"snapshots": {ref: comparison_only}}),
    )
    assert scoped.inspect_approved(ref)["faces"]
    with pytest.raises(ValueError, match="specific owner approval"):
        scoped.edit(
            "fixture", "add_approved_reference", uuid4(), timestamp(), "synthetic", ref
        )


def test_concurrent_enrollment_replay(resource):
    from concurrent.futures import ThreadPoolExecutor

    _, _, _, store, ref = resource
    request_id, issued = uuid4(), timestamp()

    def enroll(_):
        return store.edit(
            "fixture", "add_approved_reference", request_id, issued, "synthetic", ref
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(enroll, range(3))) == [{"applied": True}] * 3
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM remote_edits").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM samples WHERE snapshot_id IS NULL"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) FROM remote_audit WHERE outcome='replayed'"
            ).fetchone()[0]
            == 2
        )


def test_library_replay(resource):
    c, mint, p, store, ref = resource
    token = mint("knownfaces.manage")
    for tool, extra in [
        ("add_approved_reference", {"reference": str(ref)}),
        ("rename_subject", {"full_name": "Fixture B"}),
    ]:
        args = dict(
            subject="synthetic", request_id=str(uuid4()), issued_at=timestamp(), **extra
        )
        for _ in range(2):
            assert invoke(c, token, tool, args).json()["result"][
                "structuredContent"
            ] == {"applied": True}
        assert invoke(
            c, token, "rename_subject", args | {"full_name": "conflict"}
        ).json()["result"]["isError"]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM remote_edits").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT count(*) FROM samples WHERE snapshot_id IS NULL"
            ).fetchone()[0]
            == 2
        )
        assert db.execute("SELECT full_name FROM people").fetchone()[0] == "Fixture B"


def test_pilot_flags(resource):
    c, mint, p, store, ref = resource
    read_only = p.model_copy(
        update={"recognize_enabled": False, "manage_enabled": False}
    )
    verifier = EntraVerifier(read_only)
    with TestClient(
        build_remote(read_only, store, verifier),
        base_url=p.resource.removesuffix("/mcp"),
    ) as pilot:
        assert pilot.get("/.well-known/oauth-protected-resource").json()[
            "scopes_supported"
        ] == [p.scope_uri("knownfaces.read")]
        from unittest.mock import patch

        from knownfaces.remote_oauth import Principal

        with patch.object(
            verifier,
            "verify",
            return_value=Principal("fixture", frozenset(["knownfaces.manage"])),
        ):
            assert invoke(pilot, mint(), "rename_subject", {}).status_code == 403


def test_local_without_relay(tmp_path, monkeypatch):
    monkeypatch.delenv("KF_REMOTE_ENABLED", raising=False)
    with TestClient(create_remote()) as disabled:
        assert disabled.get("/.well-known/oauth-protected-resource").status_code == 404

    def unavailable(*args):
        raise AssertionError("Local operations contacted Entra")

    monkeypatch.setattr(EntraVerifier, "verify", unavailable)
    app = create_app(tmp_path, SyntheticVision())
    key = json.loads((tmp_path / "credentials.json").read_text())["admin"]
    with TestClient(app, headers={"Authorization": "Bearer " + key}) as local:
        res = local.post(
            "/v1/snapshots",
            data={"camera_id": "test"},
            files={"image": ("local.png", b"A", "image/png")},
        )
        assert res.status_code == 200 and res.json()["face_count"] == 1
        pid = res.json()["faces"][0]["person_id"]
        assert (
            local.put(
                f"/v1/people/{pid}/name", json={"full_name": "Local only"}
            ).status_code
            == 200
        )


def test_deletion(resource):
    c, mint, p, store, ref = resource
    invoke(c, mint(), "visit_history", WINDOW)
    old = timestamp((datetime.now(UTC) - timedelta(days=40)).isoformat())
    with store.connection(write=True) as db:
        db.execute(
            "UPDATE snapshots SET received_at=? WHERE camera_id=?", (old, "node-7")
        )
        db.execute("UPDATE remote_audit SET at=?", (old,))
    preview = maintain(store.path)
    assert preview["snapshots"] == 1 and preview["audit"] == 1
    assert store.inspect_approved(ref)["faces"]
    maintain(store.path, apply=True)
    with pytest.raises(ValueError):
        store.inspect_approved(ref)
    assert (
        maintain(store.path, forget=p.subjects["synthetic"], apply=True)["snapshots"]
        == 4
    )
    with store.connection() as db:
        for table in ("people", "samples", "snapshots", "deliveries", "remote_audit"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
