"""Run from the project root with python -m scripts.benchmark."""

import json
import os
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
from fastapi.testclient import TestClient

from knownfaces.app import create_app
from knownfaces.vision import MODEL_VERSION, Face

from .fixtures import corpus, photos


def summary(values):
    return {
        k: round(float(np.percentile(values, p)), 2)
        for k, p in (("p50_ms", 50), ("p95_ms", 95), ("max_ms", 100))
    } | {"under_one_second": sum(x < 1000 for x in values), "samples": len(values)}


def main():
    images = photos()
    with TemporaryDirectory() as folder:
        clock = perf_counter()
        app = create_app(folder)
        cold = (perf_counter() - clock) * 1000
        store = app.state.store
        frames = corpus(store.vision, images)
        people = [
            store.enroll(data, 0, full_name=f"Subject {i}")["person_id"]
            for i, data in enumerate(images)
        ]
        rng = np.random.default_rng(298)
        with store.connection(write=True) as db:
            for i in range(996):
                pid = f"distractor-{i}"
                vector = rng.normal(size=128).astype(np.float32)
                vector /= np.linalg.norm(vector)
                db.execute("INSERT INTO people VALUES(?,NULL)", (pid,))
                store.add_sample(db, pid, None, Face([], 1, vector, b""), 2)
        times, total = [], []
        with TestClient(app) as client:
            headers = {
                "Authorization": "Bearer "
                + json.loads((Path(folder) / "credentials.json").read_text())["ingest"]
            }
            for i in range(110):
                k = i % 3
                begin = perf_counter()
                response = client.post(
                    "/v1/snapshots",
                    data={"camera_id": "bench"},
                    files={"image": ("frame.jpg", frames[k], "image/jpeg")},
                    headers=headers,
                )
                duration = (perf_counter() - begin) * 1000
                response.raise_for_status()
                result = response.json()
                expected = people[: (0, 1, 4)[k]]
                assert sorted(f["person_id"] for f in result["faces"]) == sorted(
                    expected
                ), result
                if i >= 10:
                    times.append(result["recognition_ms"])
                    total.append(duration)
        print(
            json.dumps(
                dict(
                    model=MODEL_VERSION,
                    system=platform.platform(),
                    processor=platform.processor(),
                    backend="OpenCV CPU",
                    gallery=1000,
                    real_identities=4,
                    synthetic_distractors=996,
                    resolution="1920x1080",
                    warmup=10,
                    opencv_threads=int(os.getenv("KF_THREADS", "4")),
                    detector_threshold=store.vision.detector.getScoreThreshold(),
                    match_threshold=store.threshold,
                    margin=store.margin,
                    cold_model_and_app_ms=round(cold, 2),
                    recognition=summary(times),
                    http_in_process=summary(total),
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
