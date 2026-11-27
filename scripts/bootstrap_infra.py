"""Review and reconcile KnownFaces' initial trust and resource-scoped grants."""

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from scripts.azure_release import azure_cli, bicep, digest, require_review, run


def command(args, body=None):
    process = subprocess.run(args, input=body, capture_output=True)
    if process.returncode:
        raise SystemExit(
            f"{Path(args[0]).stem} {args[1]} failed (exit {process.returncode}); no credential output retained"
        )
    return process.stdout


def azure(*args):
    return json.loads(
        command([*azure_cli(), *args, "--only-show-errors", "-o", "json"]) or b"null"
    )


def config(path):
    c = json.loads(path.read_bytes())
    for key, pattern in {
        "resourceGroup": r"[\w-]{1,80}",
        "trustGroup": r"[\w-]{1,80}",
        "location": "[a-z0-9]+",
        "siteName": "[a-z][a-z0-9-]{1,35}[a-z0-9]",
        "repository": r"[\w.-]+/[\w.-]+",
        "reviewer": r"[A-Za-z0-9-]+",
    }.items():
        if not re.fullmatch(pattern, c[key]):
            raise SystemExit("Invalid private configuration field " + key)
    for key in ("subscription", "homeOperator", "authorizedUser"):
        c[key] = str(UUID(c[key]))
    if c["trustGroup"].lower() == c["resourceGroup"].lower():
        raise SystemExit("Use a separate group for deployment identities")
    return c


def identities(c):
    result = []
    for env in ("preview", "apply", "publish"):
        obj = azure(
            "identity",
            "show",
            "--subscription",
            c["subscription"],
            "-g",
            c["trustGroup"],
            "-n",
            c["siteName"] + "-" + env,
        )
        result.append(
            dict(
                environment=env,
                principal=str(UUID(obj["principalId"])),
                client=str(UUID(obj["clientId"])),
            )
        )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "stage",
        choices=("providers", "groups", "trust", "permissions", "resource-access"),
    )
    p.add_argument("--config", required=True, type=Path)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--record", type=Path)
    choice.add_argument("--approved", type=Path)
    p.add_argument("--digest")
    args = p.parse_args()
    c = config(args.config)
    commit = run(["git", "rev-parse", "HEAD"]).decode().strip()
    run(["git", "diff", "--quiet", "HEAD", "--", "."])
    sub, group = c["subscription"], c["resourceGroup"]
    stage = args.stage
    if stage == "providers":
        names = [
            "Microsoft.Web",
            "Microsoft.Storage",
            "Microsoft.Relay",
            "Microsoft.ManagedIdentity",
        ]
        missing = [
            n
            for n in names
            if azure(
                "provider",
                "show",
                "--subscription",
                sub,
                "--namespace",
                n,
                "--query",
                "registrationState",
            )
            != "Registered"
        ]
        review = dict(commit=commit, stage=stage, target=digest(sub), register=missing)
        if args.record:
            args.record.write_text(json.dumps(review, indent=2))
            print("Review digest", digest(review))
        else:
            require_review(review, json.loads(args.approved.read_bytes()), args.digest)
            for name in missing:
                azure(
                    "provider",
                    "register",
                    "--subscription",
                    sub,
                    "--namespace",
                    name,
                    "--wait",
                )
        return
    target = ["group", "--resource-group", group]
    if stage == "groups":
        target = ["sub", "--location", c["location"]]
        params = {k: c[k] for k in ("resourceGroup", "trustGroup", "location")}
    elif stage == "trust":
        target = ["group", "--resource-group", c["trustGroup"]]
        params = dict(repository=c["repository"], prefix=c["siteName"])
    elif stage == "permissions":
        params = dict(principals=identities(c))
    else:
        ids = {i["environment"]: i["principal"] for i in identities(c)}
        site = azure(
            "webapp",
            "identity",
            "show",
            "--subscription",
            sub,
            "-g",
            group,
            "-n",
            c["siteName"],
        )
        params = dict(
            siteName=c["siteName"],
            applyPrincipal=ids["apply"],
            publisherPrincipal=ids["publish"],
            homeOperator=c["homeOperator"],
            sitePrincipal=str(UUID(site["principalId"])),
        )
    with tempfile.TemporaryDirectory() as folder:
        template = Path(folder) / "template.json"
        inputs = Path(folder) / "inputs.json"
        run(
            [
                bicep(),
                "build",
                f"infra/bootstrap/{stage}.bicep",
                "--outfile",
                str(template),
            ]
        )
        inputs.write_text(json.dumps({k: {"value": v} for k, v in params.items()}))
        flags = [
            *target[1:],
            "--subscription",
            sub,
            "--template-file",
            str(template),
            "--parameters",
            "@" + str(inputs),
        ]
        raw = azure(
            "deployment",
            target[0],
            "what-if",
            *flags,
            "--no-pretty-print",
            "--validation-level",
            "ProviderNoRbac",
        )
        data = raw.get("properties", raw)
        changes = data.get("changes", [])
        if (
            raw.get("status") != "Succeeded"
            or raw.get("error")
            or data.get("error")
            or data.get("diagnostics")
            or raw.get("diagnostics")
            or not changes
        ):
            raise SystemExit("Bootstrap what-if incomplete")
        # In incremental mode Azure can enumerate unrelated existing resources.
        # Never discard a change to one of our declared resource types.
        types = {
            r["type"].lower() for r in json.loads(template.read_bytes())["resources"]
        }
        reviewed = []
        for item in changes:
            rid = item["resourceId"].lower()
            if not rid.startswith(f"/subscriptions/{sub.lower()}/"):
                raise SystemExit("Unexpected subscription")
            if (
                item["changeType"] == "Ignore"
                and not item.get("diagnostics")
                and not item.get("unsupportedReason")
            ):
                if stage == "groups":
                    managed = rid in {
                        f"/subscriptions/{sub}/resourcegroups/{c[k]}".lower()
                        for k in ("trustGroup", "resourceGroup")
                    }
                else:
                    parts = rid.split("/providers/")[-1].split("/")
                    managed = "/".join([parts[0], *parts[1::2]]) in types
                    if parts[:2] == ["microsoft.authorization", "roleassignments"]:
                        extended = rid.count("/providers/") > 1
                        if (
                            stage == "resource-access"
                            and not extended
                            or stage == "permissions"
                            and extended
                        ):
                            managed = False
                if not managed:
                    continue
            if (
                item["changeType"] not in {"Create", "NoChange", "Modify"}
                or item.get("diagnostics")
                or item.get("unsupportedReason")
                or re.search(
                    r"\*{4,}|<redacted>|\[\s*[a-z][\w.]*\s*\(", json.dumps(item), re.I
                )
            ):
                raise SystemExit("Unreviewable bootstrap change")
            reviewed.append(item)
        if not reviewed:
            raise SystemExit("No reviewable bootstrap resources")
        review = dict(
            commit=commit,
            stage=stage,
            target=digest([sub, target, params]),
            template=digest(json.loads(template.read_bytes())),
            effects=[
                (x["changeType"], digest(x["resourceId"].lower()))
                for x in sorted(reviewed, key=lambda x: x["resourceId"].lower())
            ],
            plan=digest(sorted(reviewed, key=lambda x: x["resourceId"].lower())),
        )
        review = json.loads(json.dumps(review))
        if args.record:
            args.record.write_text(json.dumps(review, indent=2))
            print("Review digest", digest(review))
        else:
            require_review(review, json.loads(args.approved.read_bytes()), args.digest)
            azure(
                "deployment",
                target[0],
                "create",
                *flags,
                "--name",
                "knownfaces-" + stage + "-" + commit[:10],
                "--query",
                "properties.provisioningState",
            )
            print("Applied reviewed", stage)


if __name__ == "__main__":
    main()
