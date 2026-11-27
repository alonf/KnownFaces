"""Remote operations share the local gallery but never call the capture workflow."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from .store import Store, label, one, timestamp

REMOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_audit(
 at TEXT NOT NULL, actor TEXT NOT NULL, tool TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS remote_audit_at ON remote_audit(at);
CREATE TABLE IF NOT EXISTS remote_edits(
 actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 at TEXT NOT NULL, PRIMARY KEY(actor,request_id));
"""


class RemoteStore(Store):
    def __init__(self, path, vision, policy):
        super().__init__(path, vision)
        self.policy = policy
        self.aliases = {str(pid): alias for alias, pid in policy.subjects.items()}
        with self.connection() as db:
            db.executescript(REMOTE_SCHEMA)

    def gallery_rows(self, db):
        # Only administrator-selected reference photos of explicitly opted-in people.
        return db.execute(
            """SELECT person_id,embedding FROM (
            SELECT person_id,embedding,row_number() OVER(PARTITION BY person_id ORDER BY rowid DESC) AS n
            FROM samples WHERE snapshot_id IS NULL AND exemplar=2
            AND person_id IN (SELECT value FROM json_each(?))) WHERE n<=8 ORDER BY person_id""",
            (json.dumps(list(self.aliases)),),
        )

    def audit(self, actor, tool, outcome):
        with self.connection(write=True) as db:
            self.audit_in(db, actor, tool, outcome)

    @staticmethod
    def audit_in(db, actor, tool, outcome):
        db.execute(
            "INSERT INTO remote_audit VALUES(?,?,?,?)",
            (timestamp(), actor, tool, outcome),
        )

    def approved(self, ref):
        approval = self.policy.snapshots.get(ref)
        if approval is None or approval.expires_at <= datetime.now(UTC):
            raise ValueError("Snapshot approval unavailable")
        return approval

    def source(self, db, approval):
        return one(
            db,
            "SELECT camera_id,captured_at,image FROM snapshots WHERE id=? AND camera_id=?",
            (str(approval.snapshot_id), approval.camera_id),
        )

    def inspect_approved(self, ref):
        approval = self.approved(ref)
        with self.connection() as db:
            snapshot = self.source(db, approval)
        _, faces = self.vision.inspect(snapshot["image"])
        with self.lock, self.connection() as db:
            matches = self.matches(db, faces)
        result = []
        for i, face in enumerate(faces):
            hit = matches.get(i)
            result.append(
                {
                    "match": self.aliases[hit[0]] if hit else "unknown",
                    "evidence_quality": {
                        "detection_score": round(face.score, 3),
                        "similarity": round(hit[1], 3) if hit else None,
                    },
                }
            )
        return {
            "observed_at": snapshot["captured_at"],
            "camera": approval.camera_id,
            "faces": result,
        }

    def history(self, subject, start, end, limit):
        if subject not in self.policy.subjects:
            raise ValueError("Subject unavailable")
        lo, hi = timestamp(start), timestamp(end)
        span = datetime.fromisoformat(hi) - datetime.fromisoformat(lo)
        if not timedelta(0) < span <= timedelta(days=7) or not 1 <= limit <= 20:
            raise ValueError("Use a window of up to seven days and a limit of 1 to 20")
        cameras = self.policy.cameras
        if "primary" not in cameras.values():
            raise ValueError("Owner must map the primary camera first")
        with self.connection() as db:
            rows = list(
                db.execute(
                    """SELECT s.camera_id,s.captured_at FROM samples f JOIN snapshots s ON s.id=f.snapshot_id
                WHERE f.person_id=? AND s.captured_at>=? AND s.captured_at<?
                AND s.camera_id IN (SELECT key FROM json_each(?)) ORDER BY s.captured_at,s.id LIMIT 1001""",
                    (str(self.policy.subjects[subject]), lo, hi, json.dumps(cameras)),
                )
            )
        if len(rows) > 1000:
            raise ValueError("Narrow the time window")
        groups = []
        for camera, when in rows:
            instant = datetime.fromisoformat(when)
            if not groups or instant - datetime.fromisoformat(
                groups[-1]["first_seen"]
            ) > timedelta(minutes=15):
                groups.append(
                    {
                        "first_seen": when,
                        "last_seen": when,
                        "cameras": [],
                        "evidence_quality": "corroboration_only",
                    }
                )
            group = groups[-1]
            group["last_seen"] = when
            if camera not in group["cameras"]:
                group["cameras"].append(camera)
            if cameras[camera] == "primary":
                group["evidence_quality"] = "possible_visit"
        return {
            "observations": list(reversed(groups))[:limit],
            "interpretation": "last_seen" if rows else "no_observations_in_window",
        }

    def edit(self, actor, operation, request_id, issued_at, subject, value):
        if subject not in self.policy.subjects:
            raise ValueError("Subject unavailable")
        instant = timestamp(issued_at)
        delta = datetime.now(UTC) - datetime.fromisoformat(instant)
        if not -timedelta(seconds=30) <= delta < timedelta(days=1):
            raise ValueError("Edit request expired or is in the future")
        person = str(self.policy.subjects[subject])
        digest = hashlib.sha256(
            json.dumps(
                [operation, instant, person, str(value)], ensure_ascii=False
            ).encode()
        ).hexdigest()

        def replay(db):
            row = db.execute(
                "SELECT fingerprint FROM remote_edits WHERE actor=? AND request_id=?",
                (actor, str(request_id)),
            ).fetchone()
            if row and row[0] != digest:
                raise ValueError("Edit request ID conflicts with an earlier edit")
            return bool(row)

        with self.connection() as db:
            if replay(db):
                self.audit(actor, operation, "replayed")
                return {"applied": True}
        if operation == "add_approved_reference":
            approval = self.approved(value)
            if approval.enroll_for != subject:
                raise ValueError(
                    "Reference enrollment requires specific owner approval"
                )
            with self.connection() as db:
                frame = self.source(db, approval)
            _, faces = self.vision.inspect(frame["image"])
            if approval.face_index is None or approval.face_index >= len(faces):
                raise ValueError("Approved face unavailable")
            face = faces[approval.face_index]
        with self.connection(write=True) as db:
            if replay(db):
                self.audit_in(db, actor, operation, "replayed")
                return {"applied": True}
            one(db, "SELECT id FROM people WHERE id=?", (person,))
            if operation == "rename_subject":
                db.execute(
                    "UPDATE people SET full_name=? WHERE id=?", (label(value), person)
                )
            elif operation == "add_approved_reference":
                self.source(db, self.approved(value))
                self.add_sample(db, person, None, face, 2)
            else:
                raise ValueError("Unknown edit")
            db.execute(
                "INSERT INTO remote_edits VALUES(?,?,?,?)",
                (actor, str(request_id), digest, timestamp()),
            )
            self.audit_in(db, actor, operation, "applied")
        return {"applied": True}
