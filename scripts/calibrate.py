"""Evaluate held-out single-person camera crops against labeled references."""

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from knownfaces.vision import Vision


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "manifest",
        type=Path,
        help="CSV columns: path,person,split,light. Split: reference/test. Light: day/night.",
    )
    args = cli.parse_args()
    with args.manifest.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    vision = Vision()
    gallery = {}
    tests = []
    for row in rows:
        _, faces = vision.inspect((args.manifest.parent / row["path"]).read_bytes())
        if row["split"] == "reference":
            if len(faces) != 1:
                raise ValueError(f"Expected one reference face in {row['path']}")
            gallery.setdefault(row["person"], []).append(faces[0].vector)
        elif row["split"] == "test" and row["light"] in {"day", "night"}:
            tests.append((row, faces))
        else:
            raise ValueError("Invalid split or lighting condition")
    if not gallery or not tests:
        raise ValueError("Supply reference and held-out test images")
    results = []
    for threshold in (0.40, 0.44, 0.48, 0.52, 0.56, 0.60):
        for margin in (0.03, 0.06, 0.10):
            for light in ("day", "night"):
                counts = dict(
                    samples=0,
                    detection_errors=0,
                    wrong_identity=0,
                    missed_identity=0,
                    correct=0,
                )
                for row, faces in tests:
                    if row["light"] != light:
                        continue
                    counts["samples"] += 1
                    if len(faces) != 1:
                        counts["detection_errors"] += 1
                        continue
                    scores = sorted(
                        (
                            (max(float(np.dot(v, faces[0].vector)) for v in vs), p)
                            for p, vs in gallery.items()
                        ),
                        reverse=True,
                    )
                    best, person = scores[0]
                    runner = scores[1][0] if len(scores) > 1 else -1
                    prediction = (
                        person
                        if best >= threshold and best - runner >= margin
                        else None
                    )
                    truth = row["person"] if row["person"] in gallery else None
                    field = (
                        "correct"
                        if prediction == truth
                        else "missed_identity"
                        if prediction is None
                        else "wrong_identity"
                    )
                    counts[field] += 1
                results.append(
                    dict(threshold=threshold, margin=margin, light=light, **counts)
                )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
