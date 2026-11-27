# Snapshot recognition

## Purpose

Turn a camera snapshot into a durable record that an agent or home automation can use immediately.

## ADDED Requirements

### Requirement: Accept and record a snapshot

The service SHALL accept JPEG and PNG bytes, camera ID, optional capture time and source event ID through HTTP and MCP. It SHALL save the original, use the receipt time when capture time is absent, and commit the image and observations before reporting success.

#### Scenario: Doorbell rings

- **WHEN** the Ring agent submits an image of two people with a camera ID and capture timestamp
- **THEN** the result includes a snapshot ID, camera ID, UTC capture time, face count of two, and both observations linked to the same saved image.

#### Scenario: Invalid image

- **WHEN** bytes cannot be decoded or exceed configured size limits
- **THEN** the caller receives a validation error and the database contains no partial snapshot or event.

#### Scenario: Source retries after losing a response

- **WHEN** the same camera event ID, bytes and capture metadata arrive again
- **THEN** the service returns the original result with one set of observations and one logical callback event. A conflicting reuse of the key returns an error.

### Requirement: Recognize named and unnamed faces

The service SHALL compare detected faces with named and unnamed identities, return a stable ID per face and include the full name when available. It SHALL return each face's bounding box, detector confidence, match similarity when matched, and latest strictly earlier known capture time or null. It SHALL return image-level face presence, count and the maximum accepted detector score, or zero for an empty result.

#### Scenario: Returning visitor has no name

- **WHEN** a previously unseen face is recorded and a later image confidently matches that identity
- **THEN** both observations share an ID, the second references the first capture time and the name is absent.

#### Scenario: No qualifying match

- **WHEN** no identity meets the configured similarity and ambiguity criteria
- **THEN** the service records an unnamed identity and returns its ID without assigning a candidate's name.

#### Scenario: Empty frame

- **WHEN** no face meets the detector threshold
- **THEN** the saved image yields false presence, zero count, zero detection score and an empty face list.

### Requirement: Deliver completed results

The service SHALL return structured recognition results directly through HTTP and MCP. A configured webhook SHALL receive the committed result with a stable event ID and support redelivery after temporary failure. For clients advertising MCP Tasks, the service SHALL support deferred completion with task lookup and opt-in status notifications.

#### Scenario: Receiver unavailable

- **WHEN** a webhook times out after recognition commits
- **THEN** recognition remains queryable, and redelivery after restart uses the same event ID and original payload.

#### Scenario: Agent requests deferred recognition

- **WHEN** a task-capable agent requests deferred processing and subscribes for the returned task ID
- **THEN** it can receive the completed task with the recognition result and recover the same result by polling after a disconnect.

### Requirement: Local recognition latency

The service SHALL run locally in a Linux container on CPU and SHALL provide a reproducible benchmark for the sub-second recognition target. The reference workload SHALL exercise detection, embedding and gallery matching on warm models with real images.

#### Scenario: Evaluate a deployment profile

- **WHEN** a host runs the documented reference workload
- **THEN** the report gives hardware, backend, corpus, gallery size, p50, p95, maximum, fraction under one second and full request latency, and identifies a missed recognition target if any timed recognition exceeds or equals one second.
