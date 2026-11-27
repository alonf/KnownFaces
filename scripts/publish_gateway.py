"""Operator-only, create-only publication during the approved blocked maintenance window."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from scripts.azure_release import blocked_package_account, digest, run
from scripts.package_gateway import package


def publish(manifest, approved, azure, group, site, directory):
    payload, sha, code_sha = package()
    if (
        digest(manifest) != approved
        or manifest["state"] != "blocked"
        or manifest["package_sha"] != sha
        or manifest["gateway_sha"] != code_sha
    ):
        raise SystemExit("Package or reviewed blocked manifest mismatch")
    account = blocked_package_account(azure, group, site)
    blob = [
        "--auth-mode",
        "login",
        "--account-name",
        account,
        "-c",
        "gateway",
        "-n",
        sha + ".zip",
    ]
    file = directory / "reviewed.zip"
    file.write_bytes(payload)
    try:
        exists = json.loads(
            azure("storage", "blob", "exists", *blob, "--query", "exists", "-o", "json")
        )
        if exists is False:
            azure(
                "storage",
                "blob",
                "upload",
                *blob,
                "-f",
                str(file),
                "--if-none-match",
                "*",
                "--overwrite",
                "false",
                "--no-progress",
                "-o",
                "none",
            )
        elif exists is not True:
            raise SystemExit("Could not verify package existence")
        check = directory / "download.zip"
        azure(
            "storage",
            "blob",
            "download",
            *blob,
            "-f",
            str(check),
            "--no-progress",
            "-o",
            "none",
        )
        if hashlib.sha256(check.read_bytes()).hexdigest() != sha:
            raise SystemExit("Published blob differs from reviewed bytes")
    finally:
        blocked_package_account(azure, group, site)
    print("Verified gateway ZIP and blocked site settings")


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--approved", type=Path, required=True)
    cli.add_argument("--digest", required=True)
    args = cli.parse_args()
    manifest = json.loads(args.approved.read_bytes())
    if (
        digest(manifest) != args.digest
        or run(["git", "rev-parse", "HEAD"]).decode().strip() != manifest["commit"]
    ):
        cli.error("Expected approved manifest for this exact checkout")
    run(["git", "diff", "--quiet", "HEAD", "--", "."])
    sub = str(UUID(os.environ["AZURE_SUBSCRIPTION_ID"]))
    group = os.environ["KF_GROUP"]
    site = os.environ["KF_SITE"]
    _, package_sha, gateway_sha = package()
    parameters = {
        "siteName": site,
        "location": os.environ["KF_LOCATION"],
        "edgeState": "blocked",
        "packageSha": package_sha,
        "gatewaySha": gateway_sha,
    }
    if (
        digest([f"/subscriptions/{sub}/resourceGroups/{group}", parameters])
        != manifest["target_sha"]
    ):
        cli.error("Operator environment differs from the approved deployment target")

    def azure(*command):
        return run(
            [shutil.which("az"), *command, "--subscription", sub, "--only-show-errors"]
        )

    with tempfile.TemporaryDirectory() as directory:
        publish(
            manifest,
            args.digest,
            azure,
            group,
            site,
            Path(directory),
        )


if __name__ == "__main__":
    main()
