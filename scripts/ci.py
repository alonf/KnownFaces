"""Run credential-free checks from the repository root with the CI Python environment."""

import os
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from scripts.azure_release import bicep, run
from scripts.package_gateway import package


def check(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    check(sys.executable, "-m", "scripts.source_review")
    check(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests",
        env={**os.environ, "KF_TEST_MARKERS": "1"},
    )
    check(
        shutil.which("openspec.cmd") or shutil.which("openspec"),
        "validate",
        "--all",
        "--strict",
        "--no-interactive",
    )
    with tempfile.TemporaryDirectory() as directory:
        for source in sorted(Path("infra").rglob("*.bicep")):
            run([bicep(), "lint", str(source)])
            run(
                [
                    bicep(),
                    "build",
                    str(source),
                    "--outfile",
                    str(Path(directory) / "arm.json"),
                ]
            )
        payload, _, code_sha = package()
        site_root = Path(directory) / "wwwroot"
        with ZipFile(BytesIO(payload)) as archive:
            archive.extractall(site_root)
        check(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--env",
            "KF_GATEWAY_SHA=" + code_sha,
            "--mount",
            f"type=bind,source={directory},target=/fixture,readonly",
            "--mount",
            f"type=bind,source={site_root},target=/home/site/wwwroot,readonly",
            "--mount",
            f"type=bind,source={Path('gateway/check-package.cjs').resolve()},target=/check.cjs,readonly",
            "node:22-alpine",
            "node",
            "/check.cjs",
        )
    check("node", "--test", "gateway/gateway.test.cjs")
    check(
        "docker",
        "compose",
        "-f",
        "infra/home/compose.yaml",
        "--profile",
        "listener",
        "config",
        "--quiet",
        env={
            **os.environ,
            "KF_IMAGE": "knownfaces:ci",
            "KF_HCM_IMAGE": "knownfaces-hcm:ci",
        },
    )
    check(
        "docker",
        "build",
        "-t",
        "knownfaces-hcm:ci",
        "-f",
        "infra/home/Dockerfile.hcm",
        "infra/home",
    )
    check(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "hcm",
        "knownfaces-hcm:ci",
        "add-from-file",
        "--help",
    )
    check(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{Path('infra/home').resolve()}:/home-src:ro",
        "--entrypoint",
        "bash",
        "knownfaces-hcm:ci",
        "-n",
        "/home-src/install.sh",
    )
    check(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--mount",
        f"type=bind,source={Path('deploy/Caddyfile.example').resolve()},target=/etc/caddy/Caddyfile,readonly",
        "--mount",
        f"type=bind,source={Path('tests/check-remote-edge.sh').resolve()},target=/check,readonly",
        "caddy:2-alpine",
        "sh",
        "/check",
    )
    tag = "knownfaces-ci:local"
    check("docker", "build", "-t", tag, ".")
    check(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        tag,
        "python",
        "-c",
        "import cv2,numpy as np; from knownfaces.vision import Vision; from knownfaces.remote import create_remote; "
        "assert not create_remote().routes; assert not Vision().inspect(cv2.imencode('.png',np.zeros((64,64,3),np.uint8))[1].tobytes())[1]",
    )
    print("Local CI passed")


if __name__ == "__main__":
    main()
