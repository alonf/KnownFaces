"""Standalone MCP resource server for remote AI agents. The local application never imports this module."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool

from .remote_oauth import EntraVerifier
from .remote_policy import RemotePolicy
from .remote_store import RemoteStore
from .vision import Vision

SCOPES = {
    "visit_history": "knownfaces.read",
    "identify_approved_snapshot": "knownfaces.recognize",
    "rename_subject": "knownfaces.manage",
    "add_approved_reference": "knownfaces.manage",
}
METADATA = "/.well-known/oauth-protected-resource"


def build_remote(policy, store, verifier):
    def challenge(scope, error=None):
        value = f'Bearer resource_metadata="{policy.discovery_url}", scope="{policy.scope_uri(scope)}"'
        return value + (f', error="{error}"' if error else "")

    async def authorize(ctx, call_next):
        if ctx.method == "tools/list":
            listing = await call_next(ctx)
            for descriptor in listing["tools"]:
                descriptor["securitySchemes"] = descriptor["_meta"]["securitySchemes"]
            return listing
        if ctx.method != "tools/call":
            return await call_next(ctx)
        principal = ctx.request.state.principal
        name = (ctx.params or {}).get("name")
        required = SCOPES.get(name) if isinstance(name, str) else None
        if required not in policy.enabled_scopes or required not in principal.scopes:
            store.audit(principal.actor, name if required else "unknown_tool", "denied")
            meta = {}
            if required in policy.enabled_scopes:
                meta["mcp/www_authenticate"] = [
                    challenge(required, "insufficient_scope")
                ]
            return types.CallToolResult(
                is_error=True,
                content=[types.TextContent(type="text", text="Permission denied")],
                _meta=meta,
            )
        try:
            result = await call_next(ctx)
        except Exception:
            store.audit(principal.actor, name, "error")
            raise
        failed = (
            result.get("isError", False)
            if isinstance(result, dict)
            else result.is_error
        )
        if required != "knownfaces.manage" or failed:
            store.audit(principal.actor, name, "error" if failed else "ok")
        return result

    mcp = MCPServer(
        "KnownFaces MCP",
        version="1",
        middleware=[authorize],
        subscriptions=False,
    )

    def tool(name):
        scope = SCOPES[name]
        return mcp.tool(
            name=name,
            meta={
                "securitySchemes": [
                    {"type": "oauth2", "scopes": [policy.scope_uri(scope)]}
                ]
            },
            annotations=types.ToolAnnotations(
                read_only_hint=scope != "knownfaces.manage",
                open_world_hint=False,
                idempotent_hint=True,
            ),
        )

    @tool("visit_history")
    def history(subject: str, start: str, end: str, limit: int = 10) -> dict[str, Any]:
        """Summarize possible visits for an owner-approved alias. Use UTC bounds up to seven days apart. Static observations establish last seen only."""
        return store.history(subject, start, end, limit)

    @tool("identify_approved_snapshot")
    def recognize(reference: UUID) -> dict[str, Any]:
        """Compare an individually approved local snapshot with opted-in reference photos. Returns observations without recording a visit."""
        return store.inspect_approved(reference)

    @tool("rename_subject")
    def rename(
        ctx: Context, subject: str, full_name: str, request_id: UUID, issued_at: str
    ) -> dict[str, Any]:
        """Explicitly rename an opted-in library identity. Keep the edit ID and timestamp on retries."""
        return store.edit(
            ctx.request_context.request.state.principal.actor,
            "rename_subject",
            request_id,
            issued_at,
            subject,
            full_name,
        )

    @tool("add_approved_reference")
    def enroll(
        ctx: Context, subject: str, reference: UUID, request_id: UUID, issued_at: str
    ) -> dict[str, Any]:
        """Explicitly enroll the owner-selected face in an approved snapshot for the specified opted-in identity."""
        return store.edit(
            ctx.request_context.request.state.principal.actor,
            "add_approved_reference",
            request_id,
            issued_at,
            subject,
            reference,
        )

    host = urlsplit(policy.resource).netloc
    transport = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=32768,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host], allowed_origins=[f"https://{host}"]
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with transport.router.lifespan_context(transport):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(METADATA)
    @app.get(METADATA + "/mcp")
    def metadata():
        return {
            "resource": policy.resource,
            "authorization_servers": [policy.issuer],
            "scopes_supported": [
                policy.scope_uri(s) for s in sorted(policy.enabled_scopes)
            ],
            "bearer_methods_supported": ["header"],
        }

    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.scope["path"]
        if (
            path not in {"/mcp", METADATA, METADATA + "/mcp"}
            or request.scope["raw_path"] != path.encode()
        ):
            return JSONResponse({"error": "not_found"}, 404)
        if request.headers.get("host") != host:
            return JSONResponse({"error": "invalid_host"}, 421)
        if path == "/mcp":
            headers = request.headers.getlist("authorization")
            scheme, _, token = (headers[0] if len(headers) == 1 else "").partition(" ")
            try:
                if scheme.lower() != "bearer" or not 0 < len(token) <= 16384:
                    raise jwt.InvalidTokenError()
                principal = await run_in_threadpool(verifier.verify, token)
            except jwt.PyJWKClientConnectionError:
                return JSONResponse({"error": "authorization_keys_unavailable"}, 503)
            except (jwt.PyJWTError, ValueError, TypeError):
                return JSONResponse(
                    {"error": "unauthorized"},
                    401,
                    headers={
                        "WWW-Authenticate": challenge(
                            "knownfaces.read", "invalid_token"
                        ),
                        "Cache-Control": "no-store",
                    },
                )
            request.state.principal = principal
            # Modern MCP includes routing headers. The dispatch check above also covers legacy clients.
            name = request.headers.get("mcp-name", "")
            scope = SCOPES.get(name)
            if (
                request.headers.get("mcp-method") == "tools/call"
                and scope
                and (
                    scope not in principal.scopes or scope not in policy.enabled_scopes
                )
            ):
                store.audit(principal.actor, name, "denied")
                headers = {"Cache-Control": "no-store"}
                if scope in policy.enabled_scopes:
                    headers["WWW-Authenticate"] = challenge(scope, "insufficient_scope")
                return JSONResponse(
                    {"error": "insufficient_scope"}, 403, headers=headers
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/", transport)
    return app


def create_remote():
    if os.getenv("KF_REMOTE_ENABLED") != "1":
        return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    policy = RemotePolicy.load(os.environ["KF_REMOTE_POLICY"])
    path = Path(os.getenv("KF_DATA", "data")) / "knownfaces.db"
    if not path.is_file():
        raise RuntimeError("Initialize the local KnownFaces gallery first")
    return build_remote(
        policy, RemoteStore(path, Vision(), policy), EntraVerifier(policy)
    )
