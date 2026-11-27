"""Preview by default. Azure output stays in memory. Print only the reviewed manifest."""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from scripts.package_gateway import package


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run(args):
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode or proc.stderr.strip():
        raise SystemExit(
            f"{Path(args[0]).stem} reported an error or warning; review privately and generate a fresh plan"
        )
    return proc.stdout


def bicep():
    binary = shutil.which("bicep") or Path.home() / ".azure/bin" / (
        "bicep.exe" if os.name == "nt" else "bicep"
    )
    if "0.41.2 " not in run([binary, "--version"]).decode():
        raise SystemExit("Use Bicep 0.41.2 for reproducible review")
    return binary


TYPES = {
    "microsoft.web/serverfarms",
    "microsoft.web/sites",
    "microsoft.web/sites/basicpublishingcredentialspolicies",
    "microsoft.web/sites/hybridconnectionnamespaces/relays",
    "microsoft.relay/namespaces",
    "microsoft.relay/namespaces/hybridconnections",
    "microsoft.relay/namespaces/hybridconnections/authorizationrules",
    "microsoft.storage/storageaccounts",
    "microsoft.storage/storageaccounts/blobservices/containers",
}
SAFE_FIELDS = {
    "publicNetworkAccess",
    "ipSecurityRestrictionsDefaultAction",
    "scmIpSecurityRestrictionsDefaultAction",
    "httpsOnly",
    "ftpsState",
    "requiresClientAuthorization",
    "allow",
    "appSettings",
    "rights",
    "port",
    "hostname",
    "sendKeyValue",
    "appCommandLine",
    "identity",
    "linuxFxVersion",
}


def activation_only(change):
    """Compare full provider snapshots. A pilot may open only the three activation switches."""
    before, after = (copy.deepcopy(change.get(key)) for key in ("before", "after"))
    for snapshot, network, access, state in (
        (before, "Disabled", "Deny", "blocked"),
        (after, "Enabled", "Allow", "synthetic"),
    ):
        props = (snapshot or {}).get("properties", {})
        config = props.get("siteConfig") or {}
        settings = config.get("appSettings") or []
        flags = [item for item in settings if item.get("name") == "KF_EDGE_STATE"]
        if (
            props.pop("publicNetworkAccess", None) != network
            or config.pop("ipSecurityRestrictionsDefaultAction", None) != access
            or config.get("scmIpSecurityRestrictionsDefaultAction") != "Deny"
            or len(flags) != 1
            or flags[0].get("value") != state
        ):
            raise ValueError("Cannot verify predicted activation switches")
        flags[0]["value"] = None
        settings.sort(key=lambda item: item["name"])
    if before != after:
        raise ValueError("Activation has unrelated changes")


def inspect_plan(raw, inputs):
    contents = raw.get("properties", raw)
    changes = contents.get("changes")
    if (
        raw.get("status") != "Succeeded"
        or raw.get("error")
        or contents.get("error")
        or raw.get("diagnostics")
        or contents.get("diagnostics")
        or not isinstance(changes, list)
        or not changes
    ):
        raise ValueError("Incomplete what-if or provider diagnostics")
    prefix = inputs["group_id"].lower() + "/providers/"
    reviewed = []
    for change in changes:
        rid = change["resourceId"].lower()
        parts = rid.removeprefix(prefix).split("/")
        kind = "/".join([parts[0], *parts[1::2]])
        effect = change["changeType"]
        if (
            not rid.startswith(prefix)
            or len(parts) % 2 != 1
            or kind not in TYPES
            or effect not in {"Create", "Modify", "NoChange"}
            or change.get("unsupportedReason")
            or change.get("diagnostics")
        ):
            raise ValueError("Unexpected resource, scope or change type")
        if (
            inputs["state"] == "synthetic"
            and effect != "NoChange"
            and (effect != "Modify" or kind != "microsoft.web/sites")
        ):
            raise ValueError(
                "Pilot activation must only modify the already provisioned site"
            )
        if effect in {"Create", "Modify"} and not isinstance(change.get("after"), dict):
            raise ValueError("Missing full predicted resource")
        if inputs["state"] == "synthetic" and effect == "Modify":
            activation_only(change)
        if re.search(
            r"\[\s*[a-z][\w.]*\s*\(|\*{4,}|<redacted>", json.dumps(change), re.I
        ):
            raise ValueError(
                "Unresolved ARM expression; a reliable preview is required"
            )
        fields = []
        for field in change.get("delta") or []:
            if field.get("propertyChangeType") not in {
                "Create",
                "Modify",
                "Delete",
                "Array",
            }:
                raise ValueError("Unsupported property change")
            path = field.get("path", "")
            prop = next(
                (p for p in reversed(path.split(".")) if p in SAFE_FIELDS), None
            )
            fields.append(prop or ("field-" + digest(path)[:12]))
        if effect == "Modify" and not fields:
            raise ValueError("Missing property delta")
        reviewed.append(
            {
                "type": kind,
                "resource_id_sha": digest(rid),
                "effect": effect,
                "fields": sorted(fields),
            }
        )
    if (
        inputs["state"] == "synthetic"
        and sum(item["effect"] == "Modify" for item in reviewed) != 1
    ):
        raise ValueError("Expected exactly one reviewed site activation")
    ordered = sorted(changes, key=lambda item: item["resourceId"].lower())
    return {
        "commit": inputs["commit"],
        "state": inputs["state"],
        "template_sha": inputs["template_sha"],
        "target_sha": digest([inputs["group_id"], inputs["parameters"]]),
        "package_sha": inputs["parameters"]["packageSha"],
        "gateway_sha": inputs["parameters"]["gatewaySha"],
        "changes": sorted(reviewed, key=lambda item: item["resource_id_sha"]),
        "plan_sha": digest(ordered),
    }


def require_review(manifest, original, seal):
    if manifest != original or digest(manifest) != seal:
        raise SystemExit(
            "Commit, target, parameters or plan changed; obtain a new review"
        )


def blocked_package_account(azure, group, site):
    access = json.loads(
        azure(
            "webapp",
            "show",
            "-g",
            group,
            "-n",
            site,
            "--query",
            "publicNetworkAccess",
            "-o",
            "json",
        )
    )
    cfg = json.loads(
        azure("webapp", "config", "show", "-g", group, "-n", site, "-o", "json")
    )
    if access != "Disabled" or any(
        cfg.get(k) != "Deny"
        for k in (
            "ipSecurityRestrictionsDefaultAction",
            "scmIpSecurityRestrictionsDefaultAction",
        )
    ):
        raise SystemExit("Operation requires a verified blocked site")
    accounts = json.loads(
        azure(
            "storage",
            "account",
            "list",
            "-g",
            group,
            "--query",
            "[].name",
            "-o",
            "json",
        )
    )
    if len(accounts) != 1:
        raise SystemExit("Expected one release storage account in the dedicated group")
    return accounts[0]


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument(
        "operation", choices=["preview", "provision-blocked", "activate-synthetic"]
    )
    cli.add_argument("--target", choices=["blocked", "synthetic"], default="blocked")
    cli.add_argument("--record", type=Path)
    cli.add_argument("--approved", type=Path)
    cli.add_argument("--digest")
    args = cli.parse_args()
    applying = args.operation != "preview"
    if applying and not (
        args.approved and args.digest and re.fullmatch("[0-9a-f]{64}", args.digest)
    ):
        cli.error(
            "A protected apply requires the independent review record and approved digest"
        )
    sub = str(UUID(os.environ["AZURE_SUBSCRIPTION_ID"]))
    group, site, location = (
        os.environ[k] for k in ("KF_GROUP", "KF_SITE", "KF_LOCATION")
    )
    if not (
        re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", group)
        and re.fullmatch(r"[a-z][a-z0-9-]{1,48}[a-z0-9]", site)
        and re.fullmatch(r"[a-z0-9]+", location)
    ):
        cli.error("Invalid target parameters")
    state = (
        args.target
        if not applying
        else {"activate-synthetic": "synthetic", "provision-blocked": "blocked"}[
            args.operation
        ]
    )
    commit = run(["git", "rev-parse", "HEAD"]).decode().strip()
    run(["git", "diff", "--quiet", "HEAD", "--", "."])
    az = shutil.which("az")
    if not az:
        cli.error("Azure CLI is required")

    def azure(*command):
        return run([az, *command, "--subscription", sub, "--only-show-errors"])

    _, package_sha, code_sha = package()
    with tempfile.TemporaryDirectory() as temp:
        template = Path(temp) / "template.json"
        run([bicep(), "build", "infra/main.bicep", "--outfile", str(template)])
        params = {
            "siteName": site,
            "location": location,
            "edgeState": state,
            "packageSha": package_sha,
            "gatewaySha": code_sha,
        }
        inputs = {
            "commit": commit,
            "group_id": f"/subscriptions/{sub}/resourceGroups/{group}",
            "state": state,
            "parameters": params,
            "template_sha": digest(json.loads(template.read_bytes())),
        }
        deployment = [
            "-g",
            group,
            "--template-file",
            str(template),
            "--mode",
            "Incremental",
            "--parameters",
            *[f"{k}={v}" for k, v in params.items()],
        ]
        if applying and state == "synthetic":
            account = blocked_package_account(azure, group, site)
            blob = Path(temp) / "verified.zip"
            azure(
                "storage",
                "blob",
                "download",
                "--auth-mode",
                "login",
                "--account-name",
                account,
                "-c",
                "gateway",
                "-n",
                package_sha + ".zip",
                "-f",
                str(blob),
                "--no-progress",
                "-o",
                "none",
            )
            if hashlib.sha256(blob.read_bytes()).hexdigest() != package_sha:
                raise SystemExit("Published package differs from reviewed bytes")
        raw = json.loads(
            azure(
                "deployment",
                "group",
                "what-if",
                *deployment,
                "--validation-level",
                "ProviderNoRbac",
                "--result-format",
                "FullResourcePayloads",
                "--no-pretty-print",
                "-o",
                "json",
            )
        )
        manifest = inspect_plan(raw, inputs)
        seal = digest(manifest)
        if applying:
            original = json.loads(args.approved.read_bytes())
            require_review(manifest, original, args.digest)
            azure(
                "deployment",
                "group",
                "create",
                *deployment,
                "--name",
                "kf-" + commit[:12] + "-" + state,
                "-o",
                "none",
            )
            print("Reviewed deployment completed")
        else:
            public = json.dumps(manifest, indent=2)
            if args.record:
                args.record.write_text(public, encoding="utf-8")
            print(public)
            print("Approval digest", seal)


if __name__ == "__main__":
    main()
