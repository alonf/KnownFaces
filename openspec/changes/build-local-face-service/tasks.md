# Tasks

## 1. Recognition engine and face database

- [x] 1.1 Pin the CPU runtime and model artifacts, including checksums and licenses. Verify both models load in a clean offline runtime and checksum mismatches fail startup.
- [x] 1.2 Implement bounded image decode, detection, alignment and embeddings. Exercise original-coordinate boxes and scores on labeled empty, one-face and group fixtures plus invalid inputs.
- [x] 1.3 Build the versioned SQLite repository and gallery. Test new, repeated, enrolled and ambiguous identities and two distinct people in one image. Verify persistence after restart.
- [x] 1.4 Commit the original, observations, previous capture times, response and delivery record atomically. Verify concurrent retries, conflicting event IDs, failed writes and out-of-order arrivals against a real database.
- [ ] 1.5 Calibrate candidate acceptance on held-out day and night images. Document thresholds and recognition errors separately from detection errors and measure warm latency before integrating transports.

## 2. HTTP and callback delivery

- [x] 2.1 Expose authenticated snapshot ingestion and retrieval with a documented request/result example. Round-trip a labeled image through HTTP and confirm saved bytes, result fields and ingestion limits.
- [x] 2.2 Deliver signed webhook payloads through a persistent outbox. Verify timeout, restart, duplicate delivery, exhausted retries and manual redelivery with a local test receiver.

## 3. Administration and history

- [x] 3.1 Implement identity browsing, name CRUD, observation reassignment and the admin UI. Exercise assign, rename, remove and correct operations and verify history, gallery and permission boundaries.
- [x] 3.2 Add reference photo upload and face selection. Verify an admin can enroll one selected face from a group photo and that enrollment leaves camera sighting counts and dates unchanged.
- [x] 3.3 Implement filtered history, name resolution, cursor pagination, co-occurrence counts and supporting image lookup. Test duplicate names, time-window edges, tied captures, camera filters and the latest-two query.

## 4. MCP access

- [x] 4.1 Expose recognition, directory, admin and history operations through Streamable HTTP with a pinned SDK. Compare structured results and authorization against their HTTP equivalents using a real MCP client.
- [x] 4.2 Implement MCP 2026-07-28 MRTR for ambiguous history queries. Test form-capability detection, accepted choice, decline, cancellation, invalid state, caller binding and the candidate-list fallback.
- [x] 4.3 Implement deferred recognition for clients advertising the Tasks extension, including durable task ownership, polling, subscriptions and cancellation handling. Verify terminal results after restart and reconnect, and direct results for callers without Tasks.
- [x] 4.4 Run the agent scenarios against a local Ring-style fixture. Verify recognition returns multiple named and unnamed faces, a retry preserves the event, and an agent can retrieve the selected person's two latest appearances.

## 5. Container and acceptance

- [x] 5.1 Package amd64 and arm64 Linux variants, loopback publishing, persistent volume and first-run credentials. Document startup, health check, storage usage, and backup/restore. Smoke-test a native container and restoration of its images and identities.
- [x] 5.2 Deliver a repeatable benchmark with labeled 1080p samples and a seeded gallery. Verify outputs and report recognition and end-to-end distributions with actual backend and hardware. Test available hosts, identify unmeasured profiles explicitly and report any missed sub-second target.
