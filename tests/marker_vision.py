"""Deterministic PNG markers for service contract tests. No camera data or model weights."""

from io import BytesIO

import numpy as np
from PIL import Image

from knownfaces.vision import Face, MAX_BYTES


def frame(*markers):
    image = Image.new("L", (4, 4))
    for column, marker in enumerate(markers):
        image.putpixel((column, 0), marker)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class MarkerVision:
    def inspect(self, data):
        if len(data) > MAX_BYTES:
            raise ValueError("Image too large")
        try:
            with Image.open(BytesIO(data)) as image:
                markers = image.crop((0, 0, 4, 1)).tobytes()
        except OSError as exc:
            raise ValueError("Invalid image") from exc
        faces = [
            Face(
                [column, 0, 1, 1],
                0.99,
                np.eye(128, dtype=np.float32)[marker],
                frame(marker),
            )
            for column, marker in enumerate(markers)
            if marker
        ]
        return "image/png", faces
