import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4

import numpy as np

from .vision import MODEL_VERSION


def uid():
    return str(uuid4())


def timestamp(value=None):
    instant = datetime.fromisoformat(value) if value is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def label(value, limit=200):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise ValueError(f"Expected 1 to {limit} characters")
    return value.strip()


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


def one(db, query, args=()):
    row = db.execute(query, args).fetchone()
    if row is None:
        raise Missing("Record not found")
    return row


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (model TEXT NOT NULL, gallery_revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, full_name TEXT);
CREATE INDEX IF NOT EXISTS people_name ON people(full_name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS snapshots (
 id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, event_id TEXT, fingerprint TEXT NOT NULL,
 captured_at TEXT NOT NULL, received_at TEXT NOT NULL, media TEXT NOT NULL, image BLOB NOT NULL, result TEXT,
 UNIQUE(camera_id,event_id));
CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(captured_at,id);
CREATE INDEX IF NOT EXISTS camera_time ON snapshots(camera_id,captured_at,id);
CREATE TABLE IF NOT EXISTS samples (
 id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), snapshot_id TEXT REFERENCES snapshots(id),
 box TEXT NOT NULL, score REAL NOT NULL, embedding BLOB NOT NULL, crop BLOB NOT NULL, exemplar INTEGER NOT NULL,
 UNIQUE(snapshot_id,person_id));
CREATE INDEX IF NOT EXISTS person_samples ON samples(person_id,snapshot_id);
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY REFERENCES snapshots(id), url TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 args TEXT, result TEXT, error TEXT);
"""


class Store:
    def __init__(self, path, vision, webhook=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vision, self.webhook = vision, webhook
        self.lock = RLock()
        self.revision = -1
        self.threshold = float(os.getenv("KF_MATCH", "0.48"))
        self.margin = float(os.getenv("KF_MARGIN", "0.06"))
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise RuntimeError("Unsupported database schema")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")
            db.execute(
                "INSERT INTO metadata SELECT ?,0 WHERE NOT EXISTS (SELECT 1 FROM metadata)",
                (MODEL_VERSION,),
            )
            if one(db, "SELECT model FROM metadata")[0] != MODEL_VERSION:
                raise RuntimeError("Gallery model differs from installed model")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def gallery_rows(self, db):
        return db.execute("""SELECT person_id, embedding FROM (
            SELECT person_id,embedding,row_number() OVER(PARTITION BY person_id ORDER BY exemplar DESC,rowid DESC) AS n
            FROM samples WHERE exemplar>0) WHERE n<=8 ORDER BY person_id""")

    def matches(self, db, faces):
        revision = one(db, "SELECT gallery_revision FROM metadata")[0]
        if revision != self.revision:
            rows = list(self.gallery_rows(db))
            self.ids = np.array([r[0] for r in rows])
            self.matrix = (
                np.stack([np.frombuffer(r[1], np.float32) for r in rows])
                if rows
                else None
            )
            self.unique_ids, self.starts = np.unique(self.ids, return_index=True)
            self.revision = revision
        candidates = []
        if self.matrix is not None:
            for index, face in enumerate(faces):
                scores = np.maximum.reduceat(self.matrix @ face.vector, self.starts)
                best = int(np.argmax(scores))
                runner = np.partition(scores, -2)[-2] if len(scores) > 1 else -1
                if (
                    scores[best] >= self.threshold
                    and scores[best] - runner >= self.margin
                ):
                    candidates.append(
                        (float(scores[best]), index, str(self.unique_ids[best]))
                    )
        winners, taken = {}, set()
        for score, index, person in sorted(candidates, reverse=True):
            if person not in taken:
                winners[index] = (person, score)
                taken.add(person)
        return winners

    def recognize(self, data, camera_id, captured_at=None, event_id=None):
        camera_id = label(camera_id)
        event_id = label(event_id) if event_id is not None else None
        capture = timestamp(captured_at) if captured_at is not None else None
        received = timestamp()
        fingerprint = sha256(data + (capture or "").encode()).hexdigest()

        def replay(db):
            if event_id is None:
                return
            row = db.execute(
                "SELECT fingerprint,result FROM snapshots WHERE camera_id=? AND event_id=?",
                (camera_id, event_id),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise Conflict("Event ID already used with different content")
                return json.loads(row[1])

        with self.connection() as db:
            prior = replay(db)
            if prior:
                return prior
        start = perf_counter()
        media, faces = self.vision.inspect(data)
        when = capture or received
        with self.lock, self.connection(write=True) as db:
            prior = replay(db)
            if prior:
                return prior
            matches = self.matches(db, faces)
            elapsed = (perf_counter() - start) * 1000
            sid = uid()
            db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,NULL)",
                (sid, camera_id, event_id, fingerprint, when, received, media, data),
            )
            observations = []
            for index, face in enumerate(faces):
                person, similarity = matches.get(index, (None, None))
                new = person is None
                if new:
                    person = uid()
                    db.execute("INSERT INTO people VALUES(?,NULL)", (person,))
                previous = one(
                    db,
                    """SELECT max(s.captured_at) FROM snapshots s JOIN samples f ON s.id=f.snapshot_id
                                    WHERE f.person_id=? AND s.captured_at<?""",
                    (person, when),
                )[0]
                sample = self.add_sample(db, person, sid, face, int(new))
                obs = dict(
                    observation_id=sample,
                    person_id=person,
                    box=face.box,
                    detection_confidence=face.score,
                    match_similarity=similarity,
                    previous_seen_at=previous,
                )
                name = one(db, "SELECT full_name FROM people WHERE id=?", (person,))[0]
                if name:
                    obs["full_name"] = name
                observations.append(obs)
            result = dict(
                snapshot_id=sid,
                camera_id=camera_id,
                captured_at=when,
                face_count=len(faces),
                has_faces=bool(faces),
                detection_confidence=max((f.score for f in faces), default=0),
                faces=observations,
                recognition_ms=round(elapsed, 3),
            )
            db.execute(
                "UPDATE snapshots SET result=? WHERE id=?", (json.dumps(result), sid)
            )
            if self.webhook:
                db.execute(
                    "INSERT INTO deliveries(id,url) VALUES(?,?)", (sid, self.webhook)
                )
            return result

    @staticmethod
    def add_sample(db, person, sid, face, exemplar):
        sample = uid()
        db.execute(
            "INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)",
            (
                sample,
                person,
                sid,
                json.dumps(face.box),
                face.score,
                face.vector.astype(np.float32).tobytes(),
                face.crop,
                exemplar,
            ),
        )
        if exemplar:
            db.execute("UPDATE metadata SET gallery_revision=gallery_revision+1")
        return sample

    def enroll(self, data, index, person_id=None, full_name=None):
        _, faces = self.vision.inspect(data)
        if index < 0 or index >= len(faces):
            raise ValueError("Select a detected face")
        with self.connection(write=True) as db:
            if person_id:
                one(db, "SELECT id FROM people WHERE id=?", (person_id,))
                if full_name is not None:
                    raise ValueError("Supply person_id or full_name")
            else:
                person_id = uid()
                db.execute(
                    "INSERT INTO people VALUES(?,?)", (person_id, label(full_name))
                )
            sample = self.add_sample(db, person_id, None, faces[index], 2)
            return {"person_id": person_id, "sample_id": sample}

    def rename(self, person_id, full_name):
        with self.connection(write=True) as db:
            one(db, "SELECT id FROM people WHERE id=?", (person_id,))
            name = label(full_name) if full_name is not None else None
            db.execute("UPDATE people SET full_name=? WHERE id=?", (name, person_id))
            return {"person_id": person_id, "full_name": name}

    def reassign(self, observation_id, person_id):
        with self.connection(write=True) as db:
            row = one(
                db,
                "SELECT snapshot_id,person_id FROM samples WHERE id=? AND snapshot_id IS NOT NULL",
                (observation_id,),
            )
            one(db, "SELECT id FROM people WHERE id=?", (person_id,))
            try:
                db.execute(
                    "UPDATE samples SET person_id=?,exemplar=2 WHERE id=?",
                    (person_id, observation_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    "That identity already appears in this snapshot"
                ) from exc
            # Retain a representative for the source cluster when its founding observation moves.
            db.execute(
                """UPDATE samples SET exemplar=1 WHERE id=(SELECT id FROM samples WHERE person_id=? LIMIT 1)
                       AND NOT EXISTS(SELECT 1 FROM samples WHERE person_id=? AND exemplar>0)""",
                (row[1], row[1]),
            )
            db.execute("UPDATE metadata SET gallery_revision=gallery_revision+1")
        return {"observation_id": observation_id, "person_id": person_id}

    def people(self, name=None, person_id=None, after="", limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("Limit must be 1 to 200")
        with self.connection() as db:
            rows = db.execute(
                """SELECT p.id AS person_id,p.full_name,
               (SELECT id FROM samples WHERE person_id=p.id ORDER BY exemplar DESC,rowid DESC LIMIT 1) AS sample_id,
               (SELECT max(s.captured_at) FROM snapshots s JOIN samples f ON s.id=f.snapshot_id WHERE f.person_id=p.id) AS last_seen_at
               FROM people p WHERE (? IS NULL OR p.full_name=? COLLATE NOCASE) AND (? IS NULL OR p.id=?)
               AND p.id>? ORDER BY p.id LIMIT ?""",
                (name, name, person_id, person_id, after, limit + 1),
            ).fetchall()
        return {
            "people": [dict(r) for r in rows[:limit]],
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    @staticmethod
    def filters(camera_id=None, start=None, end=None):
        parts, args = [], []
        for key, value in (
            ("s.camera_id=?", camera_id),
            ("s.captured_at>=?", timestamp(start) if start else None),
            ("s.captured_at<?", timestamp(end) if end else None),
        ):
            if value is not None:
                parts.append(key)
                args.append(value)
        return parts, args

    def sightings(
        self,
        person_id=None,
        name=None,
        camera_id=None,
        start=None,
        end=None,
        limit=50,
        cursor=None,
    ):
        if not 1 <= limit <= 200:
            raise ValueError("Limit must be 1 to 200")
        if name and person_id:
            raise ValueError("Use a name or a person ID")
        if name:
            candidates = self.people(name=name, limit=200)["people"]
            if len(candidates) != 1:
                return (
                    {"candidates": candidates}
                    if candidates
                    else {"sightings": [], "next_cursor": None}
                )
            person_id = candidates[0]["person_id"]
        parts, args = self.filters(camera_id, start, end)
        if person_id:
            parts.append(
                "EXISTS(SELECT 1 FROM samples f WHERE f.snapshot_id=s.id AND f.person_id=?)"
            )
            args.append(person_id)
        if cursor:
            try:
                when, sid = json.loads(base64.urlsafe_b64decode(cursor))
                args.extend((timestamp(when), label(sid)))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ValueError("Invalid history cursor") from exc
            parts.append("(s.captured_at,s.id)<(?,?)")
        where = " AND ".join(parts) or "1"
        with self.connection() as db:
            rows = db.execute(
                f"SELECT s.id,s.camera_id,s.captured_at FROM snapshots s WHERE {where} ORDER BY s.captured_at DESC,s.id DESC LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
            result = []
            for row in rows[:limit]:
                faces = db.execute(
                    """SELECT f.id observation_id,p.id person_id,p.full_name FROM samples f
                    JOIN people p ON p.id=f.person_id WHERE snapshot_id=? ORDER BY f.rowid""",
                    (row[0],),
                )
                result.append(
                    dict(
                        snapshot_id=row[0],
                        camera_id=row[1],
                        captured_at=row[2],
                        faces=[dict(f) for f in faces],
                    )
                )
            token = (
                base64.urlsafe_b64encode(
                    json.dumps([rows[limit - 1][2], rows[limit - 1][0]]).encode()
                ).decode()
                if len(rows) > limit
                else None
            )
            return {"sightings": result, "next_cursor": token}

    def companions(self, person_id, camera_id=None, start=None, end=None):
        parts, args = self.filters(camera_id, start, end)
        where = " AND ".join(["a.person_id=?", "b.person_id<>a.person_id", *parts])
        with self.connection() as db:
            one(db, "SELECT id FROM people WHERE id=?", (person_id,))
            rows = db.execute(
                f"""SELECT p.id,p.full_name,count(*) n,json_group_array(s.id) images
                FROM samples a JOIN snapshots s ON s.id=a.snapshot_id JOIN samples b ON b.snapshot_id=s.id
                JOIN people p ON p.id=b.person_id WHERE {where} GROUP BY p.id ORDER BY n DESC,p.id""",
                [person_id, *args],
            )
            return {
                "cooccurrences": [
                    dict(
                        person_id=r[0],
                        full_name=r[1],
                        count=r[2],
                        snapshot_ids=json.loads(r[3]),
                    )
                    for r in rows
                ]
            }

    def snapshot(self, sid):
        with self.connection() as db:
            return json.loads(
                one(db, "SELECT result FROM snapshots WHERE id=?", (sid,))[0]
            )
