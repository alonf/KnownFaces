# Proposal

## Why

A Ring agent can obtain a doorbell snapshot, but it needs a local service to tell it who appeared and when each person was last seen. A persistent identity for a returning, unnamed visitor makes the history useful even when an administrator tags that person later.

## What Changes

- Accept camera snapshots through HTTP and MCP, recognize every detected face locally, and save the image and its observations before acknowledging success.
- Return detection confidence, stable identity IDs, names when available, and the previous sighting. Deliver the same result to a configured automation destination.
- Let an administrator review face crops, manage names, and upload reference photos for tagging.
- Let agents search by person, time and camera, retrieve the two latest sightings, and inspect who appeared together.
- Package the service for local Linux containers and measure recognition latency on the intended hardware.

## Capabilities

### New Capabilities

- `snapshot-recognition` covers image ingestion, detection, matching, durable recording, callback delivery and the latency contract.
- `face-directory` covers stable named and unnamed identities, administrative tagging and reference photos.
- `sighting-history` covers API and MCP queries for people, sightings, image groups and co-occurrence.

### Modified Capabilities

## Impact

This is a new service in an empty application workspace. The Ring integration supplies image bytes, camera ID and capture metadata. KnownFaces owns the model runtime, persistent database, stored images, HTTP and MCP interfaces, and admin UI. A local benchmark will determine which hardware profiles meet the latency target.
