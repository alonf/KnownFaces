"""Owner-controlled configuration for the separate remote resource server."""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotApproval(StrictModel):
    snapshot_id: UUID
    camera_id: str = Field(min_length=1, max_length=200)
    expires_at: AwareDatetime
    # Both fields must be set locally to authorize adding this specific face.
    enroll_for: str | None = None
    face_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def enrollment_pair(self):
        if (self.enroll_for is None) != (self.face_index is None):
            raise ValueError("Enrollment needs both an identity and a face index")
        return self


class RemotePolicy(StrictModel):
    resource: str
    tenant_id: UUID
    api_id: UUID
    connector_id: UUID
    authorized_user_oid: UUID
    recognize_enabled: bool = False
    manage_enabled: bool = False
    # Keys are actual owner-supplied camera IDs, never display-name heuristics.
    cameras: dict[str, Literal["primary", "yard", "entrance", "service"]] = Field(
        default_factory=dict, max_length=4
    )
    # Public lookup aliases mapped to locally opted-in identity UUIDs.
    subjects: dict[str, UUID] = Field(default_factory=dict)
    snapshots: dict[UUID, SnapshotApproval] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bindings(self):
        url = urlsplit(self.resource)
        if (
            url.scheme != "https"
            or not url.hostname
            or not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", url.netloc)
            or url.path != "/mcp"
            or self.resource != f"https://{url.netloc}/mcp"
        ):
            raise ValueError("resource must be the canonical HTTPS /mcp URL")
        if self.api_id == self.connector_id:
            raise ValueError(
                "The resource and OAuth client need distinct registrations"
            )
        if len(set(self.cameras.values())) != len(self.cameras):
            raise ValueError("Each camera role maps to one camera")
        if len(set(self.subjects.values())) != len(self.subjects):
            raise ValueError("Identity aliases must be unique")
        if any(not alias.strip() or len(alias) > 100 for alias in self.subjects):
            raise ValueError("Provide short, nonempty aliases")
        for approval in self.snapshots.values():
            if approval.camera_id not in self.cameras or (
                approval.enroll_for is not None
                and approval.enroll_for not in self.subjects
            ):
                raise ValueError(
                    "Approval must refer to a mapped camera and opted-in subject"
                )
        return self

    @property
    def issuer(self):
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def discovery_url(self):
        return (
            self.resource.removesuffix("/mcp")
            + "/.well-known/oauth-protected-resource/mcp"
        )

    def scope_uri(self, scope):
        return f"api://{self.api_id}/{scope}"

    @property
    def enabled_scopes(self):
        scopes = {"knownfaces.read"}
        if self.recognize_enabled:
            scopes.add("knownfaces.recognize")
        if self.manage_enabled:
            scopes.add("knownfaces.manage")
        return scopes

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(Path(path).read_bytes())
