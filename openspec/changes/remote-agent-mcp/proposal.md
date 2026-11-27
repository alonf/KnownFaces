## Why

An AI agent needs a private way to ask about sightings while the home host retains the images, gallery and administration. The existing local MCP has different credentials and records sightings during recognition.

## What Changes

- Add an opt-in MCP resource server for a registered MCP client, with independent delegated read, recognize and manage grants.
- Add approved snapshot comparison, bounded visit summaries, audited edits, local retention and withdrawal.
- Supply a home relay and dedicated HTTPS route template, synthetic acceptance tests and a read-only pilot runbook.

## Capabilities

### New Capabilities

- `agent-access`: Remote authorization, privacy, visit evidence and pilot acceptance.

### Modified Capabilities

None.

## Impact

Adds an optional home process, two additive SQLite tables and a PyJWT dependency. The local API, MCP, administration and compose entry point retain their existing behavior. Live pilot activation requires separate approval.
