import os
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

REV = "47534e27c9851bb1128ccc0102f1145e27f23f98"
DIRECTORY = Path(os.environ.get("KF_MODELS", "models"))
MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "face_detection_yunet",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        "face_recognition_sface",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def verified(path, expected):
    if sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"Model checksum mismatch: {path.name}")


def fetch():
    DIRECTORY.mkdir(exist_ok=True, parents=True)
    for name, (folder, digest) in MODELS.items():
        path = DIRECTORY / name
        if not path.exists():
            url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{REV}/models/{folder}/{name}"
            with urlopen(url, timeout=90) as response:
                payload = response.read()
            if sha256(payload).hexdigest() != digest:
                raise RuntimeError(f"Download checksum mismatch: {name}")
            path.write_bytes(payload)
        verified(path, digest)
        with urlopen(
            f"https://raw.githubusercontent.com/opencv/opencv_zoo/{REV}/models/{folder}/LICENSE",
            timeout=30,
        ) as response:
            (DIRECTORY / f"{folder}.LICENSE").write_bytes(response.read())


if __name__ == "__main__":
    fetch()
