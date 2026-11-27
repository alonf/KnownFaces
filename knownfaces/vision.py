import os
from dataclasses import dataclass
from io import BytesIO
from threading import Lock

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from .models import DIRECTORY, MODELS, verified

MAX_BYTES = 12 * 1024 * 1024
MODEL_VERSION = "yunet-2023mar+sface-2021dec/bgr-v1"


@dataclass
class Face:
    box: list[int]
    score: float
    vector: np.ndarray
    crop: bytes


class Vision:
    def __init__(self):
        paths = []
        for name, (_, digest) in MODELS.items():
            path = DIRECTORY / name
            verified(path, digest)
            paths.append(str(path))
        cv2.setNumThreads(int(os.getenv("KF_THREADS", "4")))
        self.detector = cv2.FaceDetectorYN.create(
            paths[0], "", (640, 640), float(os.getenv("KF_DETECT", "0.82"))
        )
        self.encoder = cv2.FaceRecognizerSF.create(paths[1], "")
        self.lock = Lock()

    def inspect(self, data):
        if not 0 < len(data) <= MAX_BYTES:
            raise ValueError("Supply an image of at most 12 MiB")
        try:
            with Image.open(BytesIO(data)) as info:
                if (
                    info.format not in {"JPEG", "PNG"}
                    or info.width * info.height > 16_000_000
                ):
                    raise ValueError("Expected a JPEG or PNG of at most 16 megapixels")
                media = Image.MIME[info.format]
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid image") from exc
        image = cv2.imdecode(
            np.frombuffer(data, np.uint8),
            cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION,
        )
        if image is None:
            raise ValueError("Image decoding failed")
        h, w = image.shape[:2]
        ratio = min(1.0, 736 / max(h, w))
        small = cv2.resize(image, (max(1, round(w * ratio)), max(1, round(h * ratio))))
        sy, sx = small.shape[:2]
        found = []
        with self.lock:
            self.detector.setInputSize((sx, sy))
            _, rows = self.detector.detect(small)
            for row in [] if rows is None else rows:
                row[:14:2] *= w / sx
                row[1:14:2] *= h / sy
                x, y, width, height = row[:4]
                left, top = max(0, int(x)), max(0, int(y))
                right, bottom = min(w, int(x + width + 1)), min(h, int(y + height + 1))
                if left >= right or top >= bottom:
                    continue
                aligned = self.encoder.alignCrop(image, row)
                vec = self.encoder.feature(aligned).flatten()
                vec /= np.linalg.norm(vec)
                crop = cv2.resize(image[top:bottom, left:right], (112, 112))
                found.append(
                    Face(
                        [left, top, right - left, bottom - top],
                        float(row[-1]),
                        vec,
                        cv2.imencode(".jpg", crop)[1].tobytes(),
                    )
                )
        return media, found
