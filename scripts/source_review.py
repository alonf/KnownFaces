"""Compare all staged paths with the explicit source manifest before commit and in CI."""

import subprocess
from pathlib import Path


def main():
    paths = set(Path("source-files.txt").read_text().splitlines())
    staged = (
        subprocess.check_output(["git", "ls-files", "--stage", "-z"])
        .decode()
        .split("\0")
    )
    actual = set()
    for record in filter(None, staged):
        meta, name = record.split("\t", 1)
        mode, oid, stage = meta.split()
        if mode not in {"100644", "100755"} or stage != "0" or name not in paths:
            raise SystemExit("Unexpected index entry; review git diff --cached --stat")
        data = subprocess.check_output(["git", "cat-file", "blob", oid])
        if len(data) > 256000 or b"\0" in data:
            raise SystemExit(f"Unexpected binary or large source: {name}")
        data.decode("utf-8")
        actual.add(name)
    if actual != paths:
        raise SystemExit("Index differs from source-files.txt")
    print(f"Source allowlist verified ({len(actual)} text files)")


if __name__ == "__main__":
    main()
