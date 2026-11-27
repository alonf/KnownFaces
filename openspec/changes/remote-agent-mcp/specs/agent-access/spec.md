## ADDED Requirements

### Requirement: Dedicated OAuth resource and isolated ingress

The remote server SHALL default to disabled. When enabled it SHALL publish protected-resource discovery and validate Entra-signed delegated access tokens against the configured issuer, audience, connector and user. The HTTPS edge SHALL forward only exact MCP and discovery paths through a home-initiated relay.

#### Scenario: Local operation during outage

- **WHEN** the relay or identity provider is unavailable
- **THEN** local recognition and administration continue using local credentials

#### Scenario: Invalid credential or unapproved route

- **WHEN** a missing, expired, wrongly signed, wrong-issuer or wrong-audience token is presented to MCP
- **THEN** the request is rejected, and requests for admin, ingest and media paths are also excluded from the remote interface

### Requirement: Independent tool grants and local custody

The server SHALL enforce knownfaces.read, knownfaces.recognize and knownfaces.manage independently on every tool invocation. It SHALL retain photos, crops, embeddings and the database on the home host.

#### Scenario: Read pilot

- **WHEN** an AI agent has only a read grant and requests an allowed history range followed by an edit
- **THEN** history succeeds, the edit is denied, the library is unchanged and both calls are audited locally without tokens or images

#### Scenario: Approved comparison

- **WHEN** the caller has recognition permission and an active approval for a specific local snapshot and camera
- **THEN** the service returns match or unknown, observation time, camera and evidence quality without enrolling anyone, recording a sighting or delivering an event

### Requirement: Owner-mapped visit evidence

History SHALL use explicitly mapped cameras and opted-in subjects in bounded intervals. It SHALL group nearby observations into possible visits and use last seen for static images.

#### Scenario: Primary and corroborating evidence

- **WHEN** the owner-mapped camera at the destination and supporting cameras observe the synthetic subject within the grouping interval
- **THEN** a single possible visit is returned, while supporting observations alone indicate only a sighting

#### Scenario: Missing evidence

- **WHEN** the interval has no observations or lacks directional evidence
- **THEN** the service reports the available observations without asserting departure or absence

### Requirement: Explicit edits and deletion

Edits SHALL require the management grant, local enablement, a valid edit ID and an opted-in subject. Reference enrollment SHALL also require an owner-selected face. Retention and withdrawal SHALL support preview and actual local deletion before enabling continuous capture.

#### Scenario: Replayed edit

- **WHEN** an edit is repeated with the same request ID and payload within its validity window
- **THEN** it has one library effect and a conflicting reuse is rejected

#### Scenario: Withdrawal

- **WHEN** the owner applies withdrawal for an identity
- **THEN** the service deletes linked reference samples, shared snapshots, their face observations and related delivery and job records

### Requirement: Live acceptance before management

The pilot SHALL verify direct ChatGPT-to-Entra discovery, client configuration, authorization code with S256, resource-bound tokens and refresh using the actual connector and intended URL. The pilot SHALL begin with one synthetic read and one denied edit before enabling management.

#### Scenario: Compatibility failure

- **WHEN** a live stage fails
- **THEN** the operator records the exact stage and error before proposing a separately bound compatibility adapter
