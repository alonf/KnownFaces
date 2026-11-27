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


def azure_cli():
    binary = shutil.which("az")
    if not binary:
        raise SystemExit("Install Azure CLI")
    if os.name == "nt":
        python = Path(binary).resolve().parent.parent / "python.exe"
        if not python.is_file():
            raise SystemExit(
                "Use the Microsoft Azure CLI installer or run the release on Linux"
            )
        return [str(python), "-IBm", "azure.cli"]
    return [binary]


TYPES = {
    "microsoft.web/serverfarms",
    "microsoft.web/sites",
    "microsoft.web/sites/config",
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
        child = change["resourceId"].lower().endswith("/config/web")
        inline = "siteConfig" in props
        if not child and props.pop("publicNetworkAccess", None) != network:
            raise ValueError("Cannot verify predicted activation switches")
        if not child and not inline:
            continue
        config = props if child else props["siteConfig"]
        settings = config.get("appSettings") or []
        flags = [item for item in settings if item.get("name") == "KF_EDGE_STATE"]
        if (
            config.pop("ipSecurityRestrictionsDefaultAction", None) != access
            or config.get("scmIpSecurityRestrictionsDefaultAction") != "Deny"
            or len(flags) != 1
            or flags[0].get("value") != state
        ):
            raise ValueError("Cannot verify predicted activation switches")
        flags[0]["value"] = None
        settings.sort(key=lambda item: item["name"])
    if before != after:
        raise ValueError("Activation has unrelated changes")


def review_generated_fields(change, kind, inputs):
    """Prove the three provider-masked inputs from the exact compiled release template.

    On existing resources a delta touching a concealed value or its parent is
    never reviewable. Leave that mask in place for the ordinary rejection below.
    """
    checked = copy.deepcopy(change)
    template = inputs.get("template", {})
    if not template or digest(template) != inputs["template_sha"]:
        return checked, []
    specs = {
        "microsoft.web/sites": (
            "identity",
            {"type": "SystemAssigned"},
            "system-assigned identity",
        ),
        "microsoft.relay/namespaces/hybridconnections": (
            "properties.userMetadata",
            '[[{"key":"endpoint","value":"knownfaces-mcp.invalid:8094"}]',
            "fixed TCP endpoint knownfaces-mcp.invalid:8094",
        ),
        "microsoft.web/sites/hybridconnectionnamespaces/relays": (
            "properties.sendKeyValue",
            "[listKeys(resourceId('Microsoft.Relay/namespaces/hybridConnections/authorizationRules', "
            "format('kf-{0}', variables('suffix')), 'mcp', 'edge-send'), '2024-01-01').primaryKey]",
            "ARM resolves the primary key of this release's mcp/edge-send rule",
        ),
    }
    if kind not in specs:
        return checked, []
    path, expected, description = specs[kind]
    candidates = [r for r in template["resources"] if r["type"].lower() == kind]
    if len(candidates) != 1:
        return checked, []

    def parent(obj):
        return obj.get("properties", {}) if "." in path else obj

    leaf = path.split(".")[-1]
    if parent(candidates[0]).get(leaf) != expected:
        return checked, []
    rid = change["resourceId"].lower()
    site_id = (
        inputs["group_id"]
        + "/providers/Microsoft.Web/sites/"
        + inputs["parameters"]["siteName"]
    ).lower()
    if leaf == "identity" and rid != site_id:
        return checked, []
    if leaf == "userMetadata" and not re.fullmatch(
        re.escape(inputs["group_id"].lower())
        + r"/providers/microsoft\.relay/namespaces/kf-[a-z0-9]+/hybridconnections/mcp",
        rid,
    ):
        return checked, []
    if leaf == "sendKeyValue":
        props = change.get("after", {}).get("properties", {})
        if (
            props.get("sendKeyName") != "edge-send"
            or props.get("hostname") != "knownfaces-mcp.invalid"
            or props.get("port") != 8094
        ):
            return checked, []
        relay = props.get("serviceBusNamespace", "")
        expected_id = f"{inputs['group_id']}/providers/Microsoft.Relay/namespaces/{relay}/hybridConnections/mcp"
        if (
            not re.fullmatch(r"kf-[a-z0-9]+", relay)
            or props.get("relayArmUri", "").lower() != expected_id.lower()
            or rid
            != site_id + "/hybridconnectionnamespaces/" + relay.lower() + "/relays/mcp"
        ):
            return checked, []
    if change["changeType"] == "NoChange" and change.get("delta"):
        return checked, []
    if change["changeType"] == "Modify":
        for delta in change.get("delta", []):
            field = delta.get("path", "")
            if (
                not field
                or field == path
                or path.startswith(field + ".")
                or field.startswith(path + ".")
            ):
                return checked, []
        if parent(change.get("before") or {}).get(leaf) != parent(
            change.get("after") or {}
        ).get(leaf):
            return checked, []
    proofs = []
    for side in ("before", "after"):
        props = parent(checked.get(side) or {})
        value = props.get(leaf)
        if isinstance(value, str) and re.fullmatch(r"\*{4,}", value):
            props[leaf] = description
            proofs = [description]
    return checked, proofs


def require_closed_site(kind, change, inputs):
    if (
        not inputs.get("template")
        or inputs["state"] == "synthetic"
        or change["changeType"] == "NoChange"
    ):
        return
    props = change["after"].get("properties", {})
    if kind == "microsoft.web/sites":
        if (
            props.get("publicNetworkAccess") != "Disabled"
            or props.get("httpsOnly") is not True
        ):
            raise ValueError("Unverified public access on blocked site")
    elif kind == "microsoft.web/sites/config":
        settings = {s["name"]: s["value"] for s in props.get("appSettings", [])}
        if (
            props.get("ipSecurityRestrictionsDefaultAction") != "Deny"
            or props.get("scmIpSecurityRestrictionsDefaultAction") != "Deny"
            or props.get("ftpsState") != "Disabled"
            or props.get("httpLoggingEnabled") is not False
            or settings.get("KF_EDGE_STATE") != "blocked"
            or settings.get("KF_GATEWAY_SHA") != inputs["parameters"]["gatewaySha"]
        ):
            raise ValueError("Unverified blocked gateway configuration")


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
            rid.startswith(prefix)
            and effect == "Ignore"
            and not change.get("delta")
            and not change.get("after")
            and not change.get("diagnostics")
            and not change.get("unsupportedReason")
            and re.fullmatch(
                r"microsoft\.authorization/(roleassignments|roledefinitions)/[a-f0-9-]{36}",
                rid.rsplit("/providers/", 1)[-1],
            )
        ):
            # Separately reviewed bootstrap RBAC is outside this incremental template.
            continue
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
            and (
                effect != "Modify"
                or kind not in {"microsoft.web/sites", "microsoft.web/sites/config"}
            )
        ):
            raise ValueError(
                "Pilot activation must only modify the already provisioned site"
            )
        if effect in {"Create", "Modify"} and not isinstance(change.get("after"), dict):
            raise ValueError("Missing full predicted resource")
        require_closed_site(kind, change, inputs)
        if inputs["state"] == "synthetic" and effect == "Modify":
            activation_only(change)
        inspected, proofs = review_generated_fields(change, kind, inputs)
        if re.search(
            r"\[\s*[a-z][\w.]*\s*\(|\*{4,}|<redacted>", json.dumps(inspected), re.I
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
                **({"verified_template_inputs": proofs} if proofs else {}),
            }
        )
    if inputs["state"] == "synthetic" and {
        item["type"] for item in reviewed if item["effect"] == "Modify"
    } != (
        {"microsoft.web/sites", "microsoft.web/sites/config"}
        if inputs.get("template")
        else {"microsoft.web/sites"}
    ):
        raise ValueError("Expected exactly one reviewed site activation")
    if not reviewed:
        raise ValueError("No KnownFaces resource predictions")
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


def verify_relay(azure, group, site, subscription):
    """Verify deployed routing and authorization metadata without retrieving either key."""
    identity = json.loads(
        azure("webapp", "identity", "show", "-g", group, "-n", site, "-o", "json")
    )
    if identity.get("type") != "SystemAssigned" or not identity.get("principalId"):
        raise SystemExit("Unexpected gateway identity")
    names = json.loads(
        azure(
            "relay",
            "namespace",
            "list",
            "-g",
            group,
            "--query",
            "[].name",
            "-o",
            "json",
        )
    )
    if len(names) != 1:
        raise SystemExit("Expected one dedicated Relay namespace")
    ns = names[0]
    entity = json.loads(
        azure(
            "relay",
            "hyco",
            "show",
            "-g",
            group,
            "--namespace-name",
            ns,
            "-n",
            "mcp",
            "-o",
            "json",
        )
    )
    if entity.get("requiresClientAuthorization") is not True or json.loads(
        entity["userMetadata"]
    ) != [{"key": "endpoint", "value": "knownfaces-mcp.invalid:8094"}]:
        raise SystemExit("Unexpected Hybrid Connection target or authorization")
    for name, rights in (("edge-send", ["Send"]), ("home-listen", ["Listen"])):
        rule = json.loads(
            azure(
                "relay",
                "hyco",
                "authorization-rule",
                "show",
                "-g",
                group,
                "--namespace-name",
                ns,
                "--hybrid-connection-name",
                "mcp",
                "-n",
                name,
                "--query",
                "rights",
                "-o",
                "json",
            )
        )
        if rule != rights:
            raise SystemExit("Unexpected entity policy rights")
    uri = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Web/sites/{site}/hybridConnectionNamespaces/{ns}/relays/mcp?api-version=2024-11-01"
    )
    binding = json.loads(
        azure(
            "rest",
            "--method",
            "get",
            "--url",
            uri,
            "--query",
            "properties.[hostname,port,sendKeyName,relayArmUri]",
            "-o",
            "json",
        )
    )
    if (
        binding[:3] != ["knownfaces-mcp.invalid", 8094, "edge-send"]
        or binding[3].lower() != entity["id"].lower()
    ):
        raise SystemExit("Unexpected gateway binding")


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument(
        "operation",
        choices=[
            "preview",
            "provision-foundation",
            "provision-blocked",
            "activate-synthetic",
        ],
    )
    cli.add_argument(
        "--target", choices=["foundation", "blocked", "synthetic"], default="blocked"
    )
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
        else {
            "activate-synthetic": "synthetic",
            "provision-blocked": "blocked",
            "provision-foundation": "foundation",
        }[args.operation]
    )
    commit = run(["git", "rev-parse", "HEAD"]).decode().strip()
    run(["git", "diff", "--quiet", "HEAD", "--", "."])
    az = azure_cli()

    def azure(*command):
        return run([*az, *command, "--subscription", sub, "--only-show-errors"])

    _, package_sha, code_sha = package()
    with tempfile.TemporaryDirectory() as temp:
        template = Path(temp) / "template.json"
        run([bicep(), "build", "infra/main.bicep", "--outfile", str(template)])
        params = {
            "siteName": site,
            "location": location,
            "edgeState": "synthetic" if state == "synthetic" else "blocked",
            "attachRelay": state != "foundation",
            "packageSha": package_sha,
            "gatewaySha": code_sha,
        }
        inputs = {
            "commit": commit,
            "group_id": f"/subscriptions/{sub}/resourceGroups/{group}",
            "state": state,
            "parameters": params,
            "template_sha": digest(json.loads(template.read_bytes())),
            "template": json.loads(template.read_bytes()),
        }
        deployment = [
            "-g",
            group,
            "--template-file",
            str(template),
            "--mode",
            "Incremental",
            "--parameters",
            *[
                f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                for k, v in params.items()
            ],
        ]
        if applying and state == "synthetic":
            account = blocked_package_account(azure, group, site)
            verify_relay(azure, group, site, sub)
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
