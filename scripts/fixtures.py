"""Fetch public fixture photographs, verify bytes and compose labeled 1080p frames."""

from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

import cv2
import numpy as np

SOURCES = {
    "astronaut.png": (
        "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/astronaut.png",
        "88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5",
    ),
    "hopper.jpg": (
        "https://raw.githubusercontent.com/matplotlib/matplotlib/v3.10.3/lib/matplotlib/mpl-data/sample_data/grace_hopper.jpg",
        "a8ca6d734765703b09728ab47fe59f473d93ae3967fc24c7c0288c3c7adb7130",
    ),
    "obama.jpg": (
        "https://raw.githubusercontent.com/ageitgey/face_recognition/master/examples/obama.jpg",
        "0930e3aa8cae5920329c0c8cbc6a2ab70f47b0e67b432875beaa95cbf7e741f6",
    ),
    "biden.jpg": (
        "https://raw.githubusercontent.com/ageitgey/face_recognition/master/examples/biden.jpg",
        "3c17508bb91554c637a2eabddfae790e5bb1caba93130814fc2ac50be9760c4c",
    ),
}


def photos(directory=Path(".fixtures")):
    directory.mkdir(exist_ok=True)
    result = []
    for name, (url, digest) in SOURCES.items():
        file = directory / name
        if not file.exists():
            with urlopen(url, timeout=60) as response:
                file.write_bytes(response.read())
        data = file.read_bytes()
        if sha256(data).hexdigest() != digest:
            raise RuntimeError(f"Fixture checksum mismatch: {name}")
        result.append(data)
    return result


def corpus(vision, images):
    background = np.full((1080, 1920, 3), 60, np.uint8)
    frames = [cv2.imencode(".jpg", background)[1].tobytes()]
    for i, data in enumerate(images):
        _, faces = vision.inspect(data)
        assert len(faces) == 1
        x, y, w, h = faces[0].box
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        pad = int(max(w, h) * 0.35)
        tile = image[max(0, y - pad) : y + h + pad, max(0, x - pad) : x + w + pad]
        col = 30 + 470 * i
        background[315:765, col : col + 425] = cv2.resize(tile, (425, 450))
        if i in (0, 3):
            frames.append(cv2.imencode(".jpg", background)[1].tobytes())
    return frames
