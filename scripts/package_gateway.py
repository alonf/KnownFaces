"""Build the same source-only, content-addressed ZIP on Linux and Windows."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZipInfo

SOURCE = Path(__file__).resolve().parents[1] / "gateway/gateway.cjs"


def package():
    code = SOURCE.read_bytes().replace(b"\r\n", b"\n")
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        entry = ZipInfo("gateway.cjs", (1980, 1, 1, 0, 0, 0))
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, code)
    payload = stream.getvalue()
    return payload, sha256(payload).hexdigest(), sha256(code).hexdigest()


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser()
    cli.add_argument("directory", type=Path)
    directory = cli.parse_args().directory
    directory.mkdir(parents=True, exist_ok=True)
    payload, digest, _ = package()
    output = directory / (digest + ".zip")
    output.write_bytes(payload)
    print(output)
