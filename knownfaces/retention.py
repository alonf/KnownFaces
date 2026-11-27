"""Offline retention and withdrawal. Preview is the default operator action."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta


def maintain(path, *, forget=None, apply=False, now=None):
    if not path.is_file():
        raise ValueError("Gallery does not exist")
    now = now or datetime.now(UTC)

    def cutoff(days):
        return (now - timedelta(days=days)).isoformat(timespec="microseconds")

    with closing(sqlite3.connect(path, timeout=3)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            db.execute("CREATE TEMP TABLE expired(id TEXT PRIMARY KEY)")
            if forget:
                if (
                    db.execute(
                        "SELECT 1 FROM people WHERE id=?", (str(forget),)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Unknown identity")
                if db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('working','running') LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "Drain or cancel pending image jobs before withdrawal"
                    )
                db.execute(
                    """INSERT INTO expired SELECT snapshot_id FROM samples WHERE person_id=? AND snapshot_id IS NOT NULL
                    UNION SELECT s.id FROM snapshots s,json_each(s.result,'$.faces') f WHERE json_extract(f.value,'$.person_id')=?""",
                    (str(forget), str(forget)),
                )
            else:
                db.execute(
                    "INSERT INTO expired SELECT id FROM snapshots WHERE received_at<?",
                    (cutoff(7),),
                )

            def count(query, args=()):
                return db.execute("SELECT count(*) FROM " + query, args).fetchone()[0]

            obsolete_jobs = (
                "json_extract(result,'$.snapshot_id') IN (SELECT id FROM expired)"
            )
            job_args = ()
            if not forget:
                obsolete_jobs += " OR created<?"
                job_args = (cutoff(1),)
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            counts = {
                "snapshots": count("expired"),
                "jobs": count("jobs WHERE " + obsolete_jobs, job_args),
                "samples": count(
                    "samples WHERE snapshot_id IN (SELECT id FROM expired) OR person_id=?",
                    (str(forget),),
                ),
                "applied": apply,
            }
            if not forget and "remote_audit" in tables:
                counts["audit"] = count("remote_audit WHERE at<?", (cutoff(30),))
                counts["edit_receipts"] = count("remote_edits WHERE at<?", (cutoff(2),))
            if apply:
                db.execute(
                    "CREATE TEMP TABLE affected AS SELECT DISTINCT person_id FROM samples WHERE snapshot_id IN (SELECT id FROM expired)"
                )
                db.execute("DELETE FROM jobs WHERE " + obsolete_jobs, job_args)
                db.execute(
                    "DELETE FROM deliveries WHERE id IN (SELECT id FROM expired)"
                )
                db.execute(
                    "DELETE FROM samples WHERE snapshot_id IN (SELECT id FROM expired) OR person_id=?",
                    (str(forget),),
                )
                db.execute("DELETE FROM snapshots WHERE id IN (SELECT id FROM expired)")
                if forget:
                    db.execute("DELETE FROM people WHERE id=?", (str(forget),))
                db.execute(
                    "DELETE FROM people WHERE id IN (SELECT person_id FROM affected) AND NOT EXISTS(SELECT 1 FROM samples WHERE person_id=people.id)"
                )
                # Keep surviving local clusters searchable when their oldest exemplar expires.
                db.execute("""UPDATE samples SET exemplar=1 WHERE rowid IN (
                    SELECT min(rowid) FROM samples WHERE person_id IN (SELECT person_id FROM affected)
                    GROUP BY person_id HAVING max(exemplar)=0)""")
                if counts["samples"]:
                    db.execute(
                        "UPDATE metadata SET gallery_revision=gallery_revision+1"
                    )
                if "audit" in counts:
                    db.execute("DELETE FROM remote_audit WHERE at<?", (cutoff(30),))
                    db.execute("DELETE FROM remote_edits WHERE at<?", (cutoff(2),))
                db.commit()
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError(
                        "Deletion committed. Stop active DB users and checkpoint the WAL."
                    )
            else:
                db.rollback()
            return counts
        except Exception:
            db.rollback()
            raise
