# Remote MCP for AI agents

The existing app stays on loopback port 8093. The opt-in remote MCP process has its own tool catalog, authorization policy and loopback port 8094. Both processes use the home SQLite volume. The relay forwards HTTP to **8094 only**. The resource server needs outbound access to Microsoft's tenant-specific signing keys. The local app has no dependency on this process, Entra or the relay.

## Start with direct OAuth

1. Obtain the **approved** canonical URL `https://<dedicated-host>/mcp`, the MCP client's configuration page, and authorization to activate its private pilot route. Use an isolated gallery volume containing synthetic identities and snapshots for the pilot. The snapshot IDs, person IDs and actual camera mapping come from the owner.
2. Have the tenant administrator configure a single-tenant KnownFaces API registration issuing **v2 access tokens** (`api.requestedAccessTokenVersion = 2`) and an independent connector client registration. Set the API identifier URI to `api://<api-client-id>`. Expose the delegated scopes `knownfaces.read`, `knownfaces.recognize`, and `knownfaces.manage`. Initially grant **read only**. Restrict access to the approved delegated user account. Configure the predefined OAuth client in ChatGPT with the exact redirect URI shown on its management page. Enter its client secret only in the connector's credential UI. KnownFaces needs no client secret. [Entra access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) · [OpenAI connector authentication](https://developers.openai.com/plugins/build/auth)
3. Copy `deploy/policy.example.json` to a private owner file outside source control. Fill in the canonical URL, tenant GUID, API application GUID, connector application GUID and the delegated user's tenant object ID (`authorized_user_oid`). The verifier checks signature, issuer, expiry, audience, delegated scopes, tenant, client and user on bearer requests. The API GUID is the Entra v2 audience for this one resource binding. Keep both feature switches false during the read pilot.
4. Map the owner-selected camera at the destination to `primary`. Assign supporting sources to `yard`, `entrance` and optionally `service`. Keep physical locations and camera IDs in private home configuration. A primary observation supports a possible visit. Supporting cameras alone establish only a sighting. Nearby observations within 15 minutes of a group's first observation form one group. Snapshots contain no directional evidence, so the service reports last seen. An empty interval means no recorded observations in that interval.
5. Map a deliberately chosen lookup alias to each opted-in local person UUID in `subjects`. For the first test use a synthetic alias such as `pilot-visitor`. Nothing is selected automatically. For a later recognition grant, a snapshot approval is a random UUID key in `snapshots` pointing to a specific existing local snapshot, owner-mapped camera ID and timezone-aware expiry. For example:

   ```json
   {
     "snapshot_id": "10000000-0000-4000-8000-000000000001",
     "camera_id": "owner-supplied-camera-id",
     "expires_at": "2030-01-01T00:00:00Z"
   }
   ```

   Choose a short real expiry. An enrollment approval also specifies `enroll_for` (opted-in alias) and `face_index` (zero-based selection after local review). Explicitly adding that reference requires the manage grant. Recognition compares only manually enrolled reference photos of opted-in people. Reload the remote process after changing policy, including revocation.

## Ingress and verification

After approval, set `KF_REMOTE_POLICY_FILE` to the private file's absolute path and `KF_REMOTE_ENABLED=1` for the pilot host. Use a fresh **separate Compose project and volume for synthetic testing**. The example below reserves project name `mcp-pilot`. Check that its volume is new, clear any inherited webhook configuration, and start its local app on an unused loopback port. Through that local app, enroll a synthetic identity and record a synthetic sighting. Use its person UUID and the chosen camera ID in the private policy.

```sh
docker compose -p mcp-pilot build knownfaces
docker compose -p mcp-pilot run -d --no-deps -e KF_WEBHOOK= -e KF_WEBHOOK_SECRET= -p 127.0.0.1:18093:8093 knownfaces
docker compose -p mcp-pilot -f compose.yaml -f deploy/remote.compose.yaml --profile remote-mcp up -d --build --no-deps remote-mcp
```

Reuse an approved HTTPS ingress with a dedicated hostname, exact path allowlist and private relay backend. The Azure App Service and Relay option is documented in [the Azure operations runbook](azure-operations.md). Do not point an existing broad gateway route at the local app. A runnable edge alternative is the dedicated vhost in [the Caddy template](../deploy/Caddyfile.example). It passes the original Host header and streams MCP responses. The edge holds neither the database nor OAuth client credentials. Turn off body, header and query logging on ingress and relay.

With a trusted SSH relay host co-located with that proxy, the home host initiates the following forward after approval. Preinstall the pinned server host key. Use a dedicated key restricted on the relay to remote forwarding on loopback port 18094, with no shell, PTY, agent or X11 forwarding. Keep `GatewayPorts no`. Configure restart supervision only after the pilot passes.

```sh
ssh -NT -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:18094:127.0.0.1:8094 knownfaces-relay
```

The only forwarded paths are `/mcp`, `/.well-known/oauth-protected-resource/mcp` and its root discovery fallback. Entra hosts authorization-server discovery, token exchange and refresh. Keep the home router closed. Verify an uncredentialed `/mcp` returns a 401 challenge naming the exact metadata URL. Check the metadata resource URL, issuer and read scope. Check that `/`, `/login`, `/v1/snapshots` and a media URL return 404 from the edge.

**ChatGPT connector acceptance**

1. In the actual private ChatGPT connector, enter the canonical URL. Record sanitized timestamps, issuer, redirect URI and the discovered client configuration. Verify PKCE S256 is advertised on the selected issuer metadata and used in the authorization request. Check the resource parameter and API-qualified read scope, and request `offline_access` for refresh. Complete sign-in and consent in the connector. Observe the Entra exchange and a successful KnownFaces call with the intended binding. [ChatGPT OAuth flow](https://developers.openai.com/plugins/build/auth) · [Entra scopes and refresh](https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc)
2. Make **one read** using `visit_history` with the synthetic alias and a bounded interval. Then attempt **one edit** with `rename_subject` against the same synthetic alias. Expect denial and verify the name, samples, snapshots and receipt count are unchanged. Confirm the local audit shows the read and denial. Keep manage disabled throughout.
3. After access-token expiry, verify refresh and repeat the read without an interactive sign-in. Record the outcome and sanitized correlation IDs. Never save authorization codes, tokens, PKCE verifiers or household images in test evidence. Separately approve any later grant. Enabling recognition or management requires both an Entra scope and its server-side policy switch.
4. If a stage fails, record the stage, exact HTTP/OAuth error, advertised metadata and request correlation ID. Preserve the original resource binding. An isolated OAuth adapter is a fallback for a **demonstrated** compatibility failure. It would need its own KnownFaces issuer/resource binding, client registration, grants and tests. Keep the connector's resource and grants isolated.

Direct ChatGPT-to-Entra OAuth is experimental. Verify S256 discovery, token exchange and refresh with the actual connector before enabling additional grants.

## Local verification

Local verification on 2026-11-25 covered 20 passing tests, strict OpenSpec validation and a Linux image build. An isolated container verified discovery and the unauthenticated challenge. With the relay absent, the HTTPS edge returned 404 for eight excluded paths and 502 for the three permitted paths. The edge regression is reproducible with no network or published ports:

```sh
docker run --rm --network none -v "$PWD/deploy/Caddyfile.example:/etc/caddy/Caddyfile:ro" -v "$PWD/tests/check-remote-edge.sh:/check:ro" caddy:2-alpine sh /check
```

## Retention and rollback

Continuous capture is off for this pilot. Before enabling any capture automation, agree on the limits, rehearse restoration and withdrawal with synthetic data, and schedule the local maintenance command. This initial policy retains camera snapshots and their samples for seven days from receipt, jobs for one day, audit rows for 30 days and replay receipts for two days. Original edits can be retried for 24 hours. Daily maintenance adds up to one interval to these limits. Opted-in reference photos persist until withdrawal. An approval expires independently of its source image.

Run `python -m knownfaces.manage prune` inside the local container for a preview. For actual removal, stop both database users, run the same command as a one-off maintenance container with `--apply` on the correct volume, then restart the selected services. For withdrawal, revoke the subject and snapshot approvals first, drain/cancel pending jobs, preview `python -m knownfaces.manage forget <person-uuid>` and explicitly add `--apply` after review. Withdrawal deletes references, original shared frames containing that identity, all observations in those frames, associated deliveries and result jobs. Other people's shared sightings in those frames are also removed. SQLite secure deletion and WAL truncation apply to the live DB. Restrict backup lifetimes to seven days, delete affected backup copies or reapply withdrawal **before** making a restored copy accessible. Filesystem snapshots, exported data, and previously delivered downstream results have separate lifecycles.

Rollback remote access by removing the dedicated edge route, stopping the outbound forward, and stopping the `remote-mcp` Compose service. Revoke the connector grants, disable its Entra client and remove the pilot policy. Keep the local app and its volume. Remote tables are additive and can remain for the audit retention period. Any already committed edit needs an explicit local correction or an owner-approved restore.
