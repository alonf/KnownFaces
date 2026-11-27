# Sighting history

## Purpose

Let agents answer when a person was present, where they appeared and who appeared in the same image.

## ADDED Requirements

### Requirement: Search captured observations

HTTP and MCP SHALL expose equivalent lookups by identity ID or name, camera ID and UTC capture-time interval, with composable filters, stable ordering and bounded pagination. Rows SHALL include the image ID, camera ID, capture time and identities observed in the image.

#### Scenario: Last two sightings at the door

- **WHEN** an agent queries a person at the front door with a limit of two
- **THEN** it receives the two most recent distinct matching images in descending capture order, even if images arrived out of order. If only one exists, it receives one.

#### Scenario: Time window across cameras

- **WHEN** a caller supplies only start and end timestamps
- **THEN** the result includes sightings across cameras from the inclusive start to the exclusive end, ordered consistently across pages.

### Requirement: Resolve ambiguous names with MCP multi-round-trip requests

A lookup that requires one identity SHALL expose ambiguity. On MCP revision 2026-07-28, a client advertising form elicitation SHALL be able to resolve it using MRTR. Clients without the capability and HTTP callers SHALL receive candidate IDs to refine the query.

#### Scenario: Two people have the same name

- **WHEN** a capable client requests the latest sightings for a name shared by two identities
- **THEN** the server returns an input-required result with a choice of candidates and continuation state. A retry with a valid selection returns only that identity's sightings.

#### Scenario: User declines, cancels or submits an invalid choice

- **WHEN** the client declines, cancels, or submits a selection outside the candidate set
- **THEN** the server reports the outcome without selecting a person or disclosing a candidate's sightings.

### Requirement: Query co-occurrences

The service SHALL expose the other distinct identities in each snapshot and aggregate shared-image counts by identity pair. An agent SHALL be able to filter by camera or time window and retrieve the supporting images.

#### Scenario: Two visitors appear together twice

- **WHEN** A and B appear together in two distinct images and A appears alone in a third
- **THEN** A's co-occurrence query returns B with count two and the two supporting image IDs. Duplicate delivery of either image leaves that count at two.
