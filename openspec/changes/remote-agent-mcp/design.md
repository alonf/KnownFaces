## Context

Local recognition ingests images, persists sightings and clusters unknown identities. The remote consumer has narrower permission and data needs. A dedicated gateway forwards the remote MCP routes.

## Goals / Non-Goals

Run KnownFaces as its own resource server. Bind a single Entra tenant, API audience, connector application and delegated user. Minimize exported data. Continuous capture, directional inference and physical controls are outside this change.

## Decisions

- Run a separate stateless Streamable HTTP MCP service on home loopback with an allowlisted HTTPS edge and a home-initiated relay. Existing local clients continue using the local app through outages.
- Publish RFC 9728 metadata. Validate Entra v2 access tokens against tenant keys with PyJWT. Enforce the tool permission during SDK dispatch, including legacy clients. Later recognition and management grants also require local feature switches.
- Map camera roles and participating aliases locally. Resolve opaque expiring snapshot approvals through the existing database. Match opted-in reference samples in memory. Persist only minimal audits for recognition and history.
- Group observations in a bounded window anchored on the first sighting. Primary-camera evidence supports a possible visit. Supporting cameras alone establish last seen.
- Serialize edits and idempotency receipts in the same database transaction as the audit. Keep photos, crops and embeddings local. Use local prune and withdrawal commands with a preview before deletion.

## Risks / Trade-offs

The hosted connector's Entra metadata, client registration, PKCE resource handling and refresh require live testing at the intended URL. If real connector testing establishes incompatibility, review an isolated adapter with a separate issuer/resource trust configuration and dedicated grants. Shared captures are removed in full on withdrawal. External backups and prior recipients require independent cleanup.

## Migration Plan

Deploy no public route by default. After approval, use a synthetic volume, a dedicated resource and read-only connector. Verify one read and one denied edit plus refresh. Withdraw the route, tunnel, process and connector grant for rollback. See [the operator runbook](../../../docs/remote-mcp.md).
