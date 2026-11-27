import asyncio
import json
from typing import Any

from mcp import types as t
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.server import require_client_extension
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from .jobs import image_bytes, recognize_args, submit, task_info
from .store import Missing, timestamp


class TaskParams(t.RequestParams):
    task_id: str = Field(alias="taskId")


class Notification(BaseModel):
    method: str
    params: dict


class RecognitionTasks(Extension):
    identifier = "io.modelcontextprotocol/tasks"

    def __init__(self, store, access):
        self.store, self.access = store, access

    def owner(self, ctx):
        require_client_extension(ctx, self.identifier)
        return self.access.principal(ctx.request.headers)

    def read(self, owner, task_id):
        try:
            return task_info(self.store, owner, task_id)
        except Missing as exc:
            raise MCPError(-32602, "Unknown task") from exc

    def methods(self):
        return [
            MethodBinding(
                f"tasks/{method}", TaskParams, self.handle, frozenset({"2026-07-28"})
            )
            for method in ("get", "cancel", "update")
        ]

    async def handle(self, ctx, params):
        owner = self.owner(ctx)
        tid = params.task_id
        if ctx.request.headers.get("mcp-name") != tid:
            raise MCPError(-32602, "Mcp-Name must match taskId")
        current = self.read(owner, tid)
        if ctx.method == "tasks/get":
            return dict(resultType="complete", **current)
        if ctx.method == "tasks/update":
            raise MCPError(
                -32602, "This recognition task has no outstanding input requests"
            )
        if current["status"] != "working":
            raise MCPError(-32602, "Task already terminated")
        with self.store.connection(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET status='cancelled',updated=?,args=NULL WHERE id=? AND owner=? AND status='working'",
                (timestamp(), tid, owner),
            ).rowcount
        if not changed:
            raise MCPError(-32602, "Recognition has already started")
        return dict(resultType="complete", **self.read(owner, tid))

    async def intercept_tool_call(self, params, ctx, call_next):
        args = params.arguments or {}
        caps = ctx.session.client_capabilities
        if (
            params.name == "recognize_snapshot"
            and args.get("deferred") is True
            and ctx.protocol_version == "2026-07-28"
            and caps
            and self.identifier in (caps.extensions or {})
        ):
            owner = self.owner(ctx)
            self.access.allow(owner, "ingest")
            return submit(self.store, owner, args)
        return await call_next(ctx)

    async def listen(self, ctx, call_next):
        if ctx.method != "subscriptions/listen":
            return await call_next(ctx)
        ids = (ctx.params or {}).get("notifications", {}).get("taskIds")
        if not ids:
            return await call_next(ctx)
        if (
            not isinstance(ids, list)
            or len(ids) > 100
            or not all(isinstance(i, str) for i in ids)
        ):
            raise MCPError(-32602, "Expected up to 100 task IDs")
        owner = self.owner(ctx)
        pending = set(ids)
        for tid in pending:
            self.read(owner, tid)
        meta = {"io.modelcontextprotocol/subscriptionId": ctx.request_id}

        async def notify(method, params):
            await ctx.session.send_notification(
                Notification(method=method, params=dict(params, _meta=meta)),
                related_request_id=ctx.request_id,
            )

        await notify(
            "notifications/subscriptions/acknowledged",
            {"notifications": {"taskIds": ids}},
        )
        while pending:
            for tid in list(pending):
                record = self.read(owner, tid)
                if record["status"] != "working":
                    await notify("notifications/tasks", record)
                    pending.remove(tid)
            if pending:
                await asyncio.sleep(0.15)
        return {"_meta": meta, "resultType": "complete"}


def create_mcp(store, access):
    tasks = RecognitionTasks(store, access)
    security = RequestStateSecurity(
        keys=[access.state_key()],
        ttl=600,
        bind_principal=lambda ctx: access.principal(ctx.request.headers),
    )
    server = MCPServer(
        "KnownFaces",
        version="1.0",
        extensions=[tasks],
        middleware=[tasks.listen],
        request_state_security=security,
    )

    def allow(ctx, action="read"):
        access.allow(access.principal(ctx.headers), action)

    @server.tool()
    async def recognize_snapshot(
        ctx: Context,
        image_base64: str,
        camera_id: str,
        event_id: str | None = None,
        captured_at: str | None = None,
        deferred: bool = False,
    ) -> dict[str, Any]:
        """Recognize and record a JPEG/PNG snapshot. Use a repeatable event_id for safe retries."""
        allow(ctx, "ingest")
        return await asyncio.to_thread(
            recognize_args,
            store,
            dict(
                image_base64=image_base64,
                camera_id=camera_id,
                event_id=event_id,
                captured_at=captured_at,
            ),
        )

    @server.tool()
    def find_people(
        ctx: Context, name: str | None = None, cursor: str = "", limit: int = 50
    ) -> dict[str, Any]:
        """Page through identities, optionally filtering by exact full name."""
        allow(ctx)
        return store.people(name, after=cursor, limit=limit)

    @server.tool()
    def get_person(ctx: Context, person_id: str) -> dict[str, Any]:
        """Look up a person's name, representative sample and last sighting."""
        allow(ctx)
        rows = store.people(person_id=person_id)["people"]
        if not rows:
            raise Missing("Person not found")
        return rows[0]

    @server.tool()
    def get_snapshot(ctx: Context, snapshot_id: str) -> dict[str, Any]:
        """Retrieve the recognition result recorded at capture."""
        allow(ctx)
        return store.snapshot(snapshot_id)

    @server.tool()
    def find_sightings(
        ctx: Context,
        person_id: str | None = None,
        name: str | None = None,
        camera_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any] | t.InputRequiredResult:
        """Search sightings in descending capture order. Use limit=2 for the two latest."""
        allow(ctx)
        args = dict(
            person_id=person_id,
            name=name,
            camera_id=camera_id,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
        )
        if ctx.request_state is not None:
            answer = (ctx.input_responses or {}).get("identity")
            if not isinstance(answer, t.ElicitResult) or answer.action != "accept":
                return {"status": getattr(answer, "action", "invalid_response")}
            choice = (answer.content or {}).get("person_id")
            current = store.people(name=name, limit=200)["people"]
            if choice not in json.loads(ctx.request_state) or choice not in [
                p["person_id"] for p in current
            ]:
                raise ValueError("Unknown selection")
            args.update(person_id=choice, name=None)
            return store.sightings(**args)
        result = store.sightings(**args)
        candidates = result.get("candidates")
        cap = ctx.client_capabilities.elicitation if ctx.client_capabilities else None
        if (
            not candidates
            or ctx.protocol_version != "2026-07-28"
            or cap is None
            or cap.form is None
        ):
            return result
        options = [
            {
                "const": p["person_id"],
                "title": f"{p['person_id']} · last seen {p['last_seen_at'] or 'never'}",
            }
            for p in candidates
        ]
        form = {
            "type": "object",
            "properties": {"person_id": {"type": "string", "oneOf": options}},
            "required": ["person_id"],
        }
        request = t.ElicitRequest(
            params=t.ElicitRequestFormParams(
                message=f"Which {name}?", requested_schema=form
            )
        )
        return t.InputRequiredResult(
            input_requests={"identity": request},
            request_state=json.dumps([p["person_id"] for p in candidates]),
        )

    @server.tool()
    def get_cooccurrences(
        ctx: Context,
        person_id: str,
        camera_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Find people seen in the same image with shared counts and image IDs."""
        allow(ctx)
        return store.companions(person_id, camera_id, start, end)

    @server.tool()
    def set_name(
        ctx: Context, person_id: str, full_name: str | None = None
    ) -> dict[str, Any]:
        """Assign, change or clear a full name. Requires admin access."""
        allow(ctx, "admin")
        return store.rename(person_id, full_name)

    @server.tool()
    def inspect_reference(ctx: Context, image_base64: str) -> dict[str, Any]:
        """List face indexes and boxes for reference enrollment."""
        allow(ctx, "admin")
        return preview(store, image_bytes(image_base64))

    @server.tool()
    def enroll_reference(
        ctx: Context,
        image_base64: str,
        index: int,
        person_id: str | None = None,
        full_name: str | None = None,
    ) -> dict[str, Any]:
        """Add one reference to an existing ID or create a named identity."""
        allow(ctx, "admin")
        return store.enroll(image_bytes(image_base64), index, person_id, full_name)

    @server.tool()
    def reassign_observation(
        ctx: Context, observation_id: str, person_id: str
    ) -> dict[str, Any]:
        """Correct the identity associated with a camera observation."""
        allow(ctx, "admin")
        return store.reassign(observation_id, person_id)

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=18 * 1024 * 1024,
    )


def preview(store, data):
    import base64

    _, faces = store.vision.inspect(data)
    return {
        "faces": [
            dict(
                index=i,
                box=f.box,
                detection_confidence=f.score,
                crop_base64=base64.b64encode(f.crop).decode(),
            )
            for i, f in enumerate(faces)
        ]
    }
