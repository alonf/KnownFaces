# Face directory

## Purpose

Give administrators a way to identify returning people and curate reference images without distorting camera history.

## ADDED Requirements

### Requirement: Review and manage names

An authorized administrator SHALL be able to browse named and unnamed identities with representative crops and perform create, read, update and delete operations on full names through the UI and API. Name edits SHALL preserve identity IDs and linked observations.

#### Scenario: Tag a returning unknown

- **WHEN** an administrator opens an unnamed face, assigns a full name and searches that name
- **THEN** the original ID and earlier camera sightings appear under the assigned name.

#### Scenario: Remove an incorrect name

- **WHEN** an administrator renames a person and then removes the name
- **THEN** the directory shows the same ID as unnamed, the old name stops matching and its sightings remain queryable.

### Requirement: Enroll from a reference photo

The service SHALL accept reference photos for tagging in the UI and API. An administrator SHALL be able to select a detected face and attach it to an existing identity or create a named identity. Enrollment SHALL be distinct from a camera sighting.

#### Scenario: Group photo selected

- **WHEN** an administrator uploads a photo of three people
- **THEN** the UI shows each detected face and saves only the selected reference to the chosen identity, without changing anyone's last-seen time.

#### Scenario: No face detected

- **WHEN** an uploaded reference photo contains no accepted detection
- **THEN** the administrator sees an actionable result and no identity is created.

### Requirement: Limit access to face data

The service SHALL require authorization for stored images, names, recognition and history, and SHALL restrict tagging and reference management to administrators.

#### Scenario: Query credential attempts a name change

- **WHEN** a read-only agent submits a name change
- **THEN** the request is denied and the stored name is unchanged.
