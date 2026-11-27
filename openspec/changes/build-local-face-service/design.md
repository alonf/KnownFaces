# Design

## Context

The local service targets Linux containers and CPU inference. Benchmark recognition on the deployment hardware.

## Goals / Non-Goals

The recognition engine and identity repository own the meaning of a match and a sighting. HTTP, MCP, webhooks and the tagging UI share those definitions. Ring owns the doorbell event and snapshot capture. KnownFaces accepts images from any caller with capture permissions. A sighting represents one captured image. Co-occurrence records visible evidence of shared presence in that image.

## Decisions

### Recognition

Use an in-process Python service with a pinned OpenCV CPU build, YuNet detection and SFace aligned embeddings. Bundle checksum-verified ONNX weights with their license notices at build time so the container can run offline. Retain original-resolution image coordinates after resizing the detector input. Compare normalized vectors by cosine similarity against a bounded number of representative embeddings per identity. Store model and preprocessing versions. A future model change requires rebuilding compatible embeddings from stored samples before switching the gallery. [OpenCV recognition implementation](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/demo.py)

Calibrate a match threshold and minimum gap between the top two distinct identities using labeled doorbell images, including night scenes and people absent from the gallery. An uncertain face gets a stable unnamed ID. Keep detector confidence and identity similarity in separate fields. Prefer manually confirmed exemplars when updating a person's gallery to limit drift. Resolve matches jointly per image to prevent two different visitors being assigned the same identity through independent nearest-neighbor decisions.

### Data and consistency

Start with SQLite in WAL mode on a persistent local volume. Store original image bytes in the database in the initial release so image, observations and outbox entry can commit atomically. SQLite backups can then capture images and records together. Keep the gallery in process for fast matching, refresh on committed changes, and serialize identity assignment and gallery publication through one writer. Perform decode and detection outside that critical section.

| Record | Meaning |
| --- | --- |
| Identity | Stable UUID, optional full name, creation time |
| Snapshot | UUID, camera ID, source event ID, immutable capture and receipt times, bytes, digest, original response |
| Observation | Snapshot ID, identity ID, original-image box, detector score, match similarity and face sample |
| Reference | Admin-selected image and face sample, associated identity and model version |
| Delivery | Stable event ID, destination, immutable committed payload, attempt count and next attempt |
| MCP task | Owner, opaque task ID, operation, durable status, result or error, expiry |

Use a unique camera/event key for retries and a payload fingerprint for conflicts. Persist the original recognition response for exact replay after a rename or delayed image. History lookups resolve the current name and sort by capture time with a snapshot-ID tie breaker. Previous sighting means the most recent stored observation strictly before the supplied capture time. Derive shared-image counts by joining observations on snapshot ID and counting distinct snapshots. Index by identity/time and camera/time. Treat reference photos as a separate source so enrollment cannot create a fictitious visit.

### Interfaces

| Operation | HTTP | MCP tool |
| --- | --- | --- |
| Recognize and save | `POST /v1/snapshots` (multipart) | `recognize_snapshot` (base64 bytes, media type, camera ID) |
| Search and inspect people | `GET /v1/people`, `GET /v1/people/{id}` | `find_people`, `get_person` |
| Recent sightings and filters | `GET /v1/sightings` | `find_sightings` |
| Inspect a saved image and its faces | `GET /v1/snapshots/{id}` and authenticated image route | `get_snapshot` |
| Shared appearances | `GET /v1/people/{id}/co-occurrences` | `get_cooccurrences` |
| Manage names and references | Identity name PUT/DELETE and reference upload/select routes | Admin-scoped equivalents using the same service methods |

Use FastAPI and the official MCP Python SDK with Streamable HTTP, mounted in the same service. Pin a version verified with a real MCP 2026-07-28 client. Local authentication distinguishes query, ingest and admin operations. Bind the published port to loopback by default. Protect the web UI with an authenticated session and CSRF protection. The browser sends images to local endpoints only.

### MCP interactions and callbacks

- **Immediate recognition.** The Ring agent sends the camera ID, image and source event ID. KnownFaces commits once and returns a complete tool result. Unknown people are valid results and receive an ID without blocking on a human.
- **Input during a call.** When a single-person history query finds two matching names, a form-capable client receives `resultType: "input_required"` with a keyed `elicitation/create` in `inputRequests`. The host handles the choice and retries the original tool with the same arguments, keyed `inputResponses` and echoed `requestState`. Bind state to the authenticated caller, candidate set, original arguments and expiry. Validate the selected ID before returning history. Clients without form support get an explicit ambiguity result. [MCP MRTR specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) · [Python SDK callback handling](https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/)
- **Deferred result for an MCP agent.** If the caller requests deferred mode and advertises `io.modelcontextprotocol/tasks`, issue a durable task handle. Serve the terminal result through `tasks/get` and support opt-in `subscriptions/listen` for that task ID, publishing `notifications/tasks`. Check task ownership on both routes. Clients without Tasks receive the direct result. [MCP Tasks extension](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks)
- **Webhook delivery.** A worker signs and posts the committed result to an administrator-configured destination. Use timeouts, backoff, bounded retries, visible failure status and manual redelivery. Receivers deduplicate on event ID. Webhook availability stays off the recognition request path.

### Latency and deployment

Provide a multi-architecture Linux image for amd64 and arm64. Start with a warm CPU process on each host. Measure the GTX 1080 separately only with a runtime that supports the card and actually reports executing inference on it. Report the actual backend for each run.

Proposed acceptance workload is 100 timed local calls at concurrency one after 10 warm-ups, using 1080p JPEGs with zero, one and four detectable faces against 1,000 identities. Measure decode through gallery matching separately from arrival through durable response. Record p50, p95, maximum and fraction below one second, plus cold start. Check expected counts and IDs so a fast detector that misses people cannot pass. Publish the model versions, thresholds, input sizes, gallery composition and host details with results. This workload is a proposed baseline for the user's latency requirement. No host has been benchmarked yet.

## Risks / Trade-offs

- Small or backlit faces can disappear at lower detector resolutions. Evaluate real doorbell crops before selecting inference size and thresholds.
- An incorrect match can spread to later sightings. Preserve per-observation evidence and support administrative reassignment of a mistaken observation to the correct identity.
- Local images and embeddings occupy disk over time. Report storage use and provide a consistent volume backup/restore procedure.
- MCP hosts vary in version and supported capabilities. Exercise complete, interactive and deferred flows separately with capability checks and a compatible SDK client.

## Migration Plan

Initialize a versioned empty database on the mounted volume at first startup. Smoke-test a blank frame and a labeled fixture, restart the container and verify both records. For subsequent upgrades, back up the volume first and verify schema and model compatibility at startup. Roll back the image and matching backup together.
