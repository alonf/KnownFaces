"""Local operator commands run inside the service container."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from .auth import Access
from .retention import maintain


def main():
    cli = argparse.ArgumentParser()
    commands = cli.add_subparsers(dest="command", required=True)
    credential = commands.add_parser("token")
    credential.add_argument("role", choices=["admin", "ingest", "read"])
    backup = commands.add_parser("backup")
    backup.add_argument("path", type=Path)
    prune = commands.add_parser(
        "prune", help="Preview expired captures and remote records"
    )
    prune.add_argument("--apply", action="store_true")
    forget = commands.add_parser(
        "forget", help="Preview deletion of an identity and its shared frames"
    )
    forget.add_argument("person_id", type=UUID)
    forget.add_argument("--apply", action="store_true")
    args = cli.parse_args()
    folder = Path(os.getenv("KF_DATA", "data"))
    if args.command == "token":
        print(Access(folder).keys[args.role])
        return
    source = folder / "knownfaces.db"
    if args.command in {"prune", "forget"}:
        print(
            json.dumps(
                maintain(
                    source, forget=getattr(args, "person_id", None), apply=args.apply
                )
            )
        )
        return
    if not source.is_file() or args.path.exists():
        cli.error("Source must exist and backup destination must be a new file")
    with sqlite3.connect(source) as db, sqlite3.connect(args.path) as target:
        db.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup failed integrity check")
    print(json.dumps({"path": str(args.path), "bytes": args.path.stat().st_size}))


if __name__ == "__main__":
    main()
