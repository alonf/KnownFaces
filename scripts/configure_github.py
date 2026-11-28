"""Configure deployment trust on the specified GitHub repository after the trust Bicep stage."""

import argparse
import copy
import json
import shutil
from pathlib import Path
from uuid import UUID

from scripts.azure_release import digest
from scripts.bootstrap_infra import azure, command, config, identities


def verify_release_ruleset(live, desired):
    """Accept the configured protections or stronger PR and status-check requirements."""
    if (
        live.get("target") != desired["target"]
        or live.get("enforcement") != "active"
        or live.get("bypass_actors") != []
        or live.get("conditions") != desired["conditions"]
    ):
        raise SystemExit(
            "Release ruleset target, enforcement or bypass settings differ"
        )
    rules = {r["type"]: r.get("parameters", {}) for r in live["rules"]}
    for rule in desired["rules"]:
        kind = rule["type"]
        if kind not in rules:
            raise SystemExit(f"Missing release rule: {kind}")
        for field, expected in rule.get("parameters", {}).items():
            actual = rules[kind].get(field)
            if field == "required_approving_review_count":
                meets = type(actual) is int and actual >= expected
            elif field == "required_status_checks":
                required = {(c["context"], c["integration_id"]) for c in expected}
                meets = required <= {
                    (c["context"], c.get("integration_id")) for c in actual or []
                }
            elif isinstance(expected, bool):
                meets = isinstance(actual, bool) and (actual or not expected)
            else:
                meets = actual == expected
            if not meets:
                raise SystemExit(f"Inadequate release protection: {kind}.{field}")


def github_api(route, method="GET", body=None):
    data = command(
        [shutil.which("gh"), "api", "--method", method, route]
        + (["--input", "-"] if body is not None else []),
        json.dumps(body).encode() if body is not None else None,
    )
    return json.loads(data) if data else None


def verify_environment(env, required, policies, reviewer, name):
    if (
        env.get("deployment_branch_policy")
        != {"protected_branches": False, "custom_branch_policies": True}
        or len(required) != 1
        or required[0].get("prevent_self_review") is not True
        or [
            (r.get("type"), r.get("reviewer", {}).get("id"))
            for r in required[0].get("reviewers", [])
        ]
        != [("User", reviewer)]
        or [(v.get("type"), v["name"]) for v in policies] != [("branch", "main")]
    ):
        raise SystemExit(
            "GitHub could not verify reviewer and main-only protections for " + name
        )


def release_ruleset():
    return dict(
        name="KnownFaces protected release",
        target="branch",
        enforcement="active",
        bypass_actors=[],
        conditions={"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        rules=[
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "verify", "integration_id": 15368}
                    ],
                },
            },
        ],
    )


def migrate_ruleset(repository, ruleset_id, approved_digest=None):
    desired = release_ruleset()
    route = f"repos/{repository}/rulesets/{ruleset_id}"
    live = github_api(route)
    if (
        live.get("source_type") != "Repository"
        or live.get("source", "").casefold() != repository.casefold()
        or live.get("id") != ruleset_id
        or live.get("name") != desired["name"]
    ):
        raise SystemExit("Expected the repository-owned KnownFaces release ruleset")
    proposed = copy.deepcopy(live)
    status = [r for r in proposed["rules"] if r["type"] == "required_status_checks"]
    if len(status) != 1:
        raise SystemExit("Expected one required status checks rule")
    checks = [
        c
        for c in status[0]["parameters"]["required_status_checks"]
        if c["context"] == "verify"
    ]
    if len(checks) != 1:
        raise SystemExit("Expected one verify status check")
    previous = checks[0].get("integration_id")
    if previous == 15368:
        verify_release_ruleset(live, desired)
        print("Verified existing GitHub Actions binding. Migration already complete.")
        return
    if previous not in (None, -1):
        raise SystemExit("The verify check already names a different integration")
    checks[0]["integration_id"] = 15368
    verify_release_ruleset(proposed, desired)
    seal = digest([route, live, proposed["rules"]])
    print(
        f"{repository} ruleset {ruleset_id}: bind verify to GitHub Actions (15368). Review digest {seal}"
    )
    if approved_digest is None:
        return
    if approved_digest != seal:
        raise SystemExit("Ruleset differs from the approved migration preview")
    github_api(route, "PUT", {"rules": proposed["rules"]})
    saved = github_api(route)
    verify_release_ruleset(saved, desired)
    if saved["rules"] != proposed["rules"]:
        raise SystemExit("Saved rules differ from the reviewed migration")
    print("Verified migrated ruleset")


def check_protections(c, environments, *, create=False):
    api = github_api
    prefix = "repos/" + c["repository"]
    reviewer = api("users/" + c["reviewer"])["id"]
    rule = release_ruleset()
    matches = [r for r in api(prefix + "/rulesets") if r["name"] == rule["name"]]
    if not matches and create:
        ruleset_id = api(prefix + "/rulesets", "POST", rule)["id"]
    elif len(matches) != 1:
        raise SystemExit("Ambiguous KnownFaces main ruleset")
    else:
        ruleset_id = matches[0]["id"]
    verify_release_ruleset(api(prefix + "/rulesets/" + str(ruleset_id)), rule)
    existing = {
        e["name"] for e in api(prefix + "/environments?per_page=100")["environments"]
    }
    for environment in environments:
        name = "knownfaces-" + environment
        endpoint = prefix + "/environments/" + name
        if name not in existing and create:
            api(
                endpoint,
                "PUT",
                dict(
                    prevent_self_review=True,
                    reviewers=[{"type": "User", "id": reviewer}],
                    deployment_branch_policy={
                        "protected_branches": False,
                        "custom_branch_policies": True,
                    },
                ),
            )
            api(
                endpoint + "/deployment-branch-policies",
                "POST",
                {"name": "main", "type": "branch"},
            )
        env = api(endpoint)
        required = [
            r for r in env["protection_rules"] if r["type"] == "required_reviewers"
        ]
        policies = api(endpoint + "/deployment-branch-policies")["branch_policies"]
        verify_environment(env, required, policies, reviewer, name)
    return reviewer


def workflow_principals(c, principals):
    access_clients = {
        k: str(UUID(v)) for k, v in c.get("accessWorkflowClients", {}).items()
    }
    if access_clients:
        if (
            set(access_clients) != {"preview", "apply"}
            or len(set(access_clients.values())) != 2
        ):
            raise SystemExit(
                "Provide distinct, preauthorized access preview and apply client IDs"
            )
        if {str(UUID(i["client"])) for i in principals} & set(access_clients.values()):
            raise SystemExit(
                "Access administration requires identities separate from the release jobs"
            )
        principals.extend(
            {"environment": "access-" + k, "client": v}
            for k, v in access_clients.items()
        )
    return principals


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--apply", action="store_true")
    p.add_argument(
        "--migrate-ruleset",
        type=int,
        metavar="ID",
        help="Preview the existing repository ruleset upgrade in isolation",
    )
    p.add_argument("--digest", help="Approved ruleset migration preview digest")
    args = p.parse_args()
    if args.migrate_ruleset is None and args.digest is not None:
        p.error("--digest requires --migrate-ruleset")
    if args.migrate_ruleset is not None and args.apply != (args.digest is not None):
        p.error("Applying a migration requires both --apply and --digest")
    c = config(args.config)
    if args.migrate_ruleset is not None:
        migrate_ruleset(c["repository"], args.migrate_ruleset, args.digest)
        return
    if not args.apply:
        print(
            "Would add a protected main ruleset, main-only reviewed environments, OIDC settings and release switches for",
            c["repository"],
        )
        return
    gh = shutil.which("gh")
    repo = c["repository"]
    tenants = azure("account", "show", "--subscription", c["subscription"])["tenantId"]
    principals = workflow_principals(c, identities(c))
    reviewer = check_protections(c, [i["environment"] for i in principals], create=True)
    for item in principals:
        name = "knownfaces-" + item["environment"]
        values = dict(
            AZURE_CLIENT_ID=item["client"],
            AZURE_TENANT_ID=tenants,
            AZURE_SUBSCRIPTION_ID=c["subscription"],
            KF_GROUP=c["resourceGroup"],
            KF_LOCATION=c["location"],
            KF_SITE=c["siteName"],
        )
        if item["environment"].startswith("access-"):
            values["KF_ACCESS_CONFIG"] = json.dumps(
                {
                    k: c[k]
                    for k in (
                        "repository",
                        "reviewer",
                        "subscription",
                        "resourceGroup",
                        "trustGroup",
                        "location",
                        "siteName",
                        "authorizedUser",
                        "homeOperator",
                    )
                }
            )
        for key, value in values.items():
            command(
                [gh, "secret", "set", key, "--repo", repo, "--env", name],
                value.encode(),
            )
        command(
            [
                gh,
                "variable",
                "set",
                "KF_REVIEWER_ID",
                "--repo",
                repo,
                "--env",
                name,
                "--body",
                str(reviewer),
            ]
        )
    for key, value in {
        "KF_PROTECTED_APPLY": "verified",
        "KF_AZURE_ENABLED": "true",
        "KF_ACCESS_ENABLED": "true" if c.get("accessWorkflowClients") else "false",
    }.items():
        command([gh, "variable", "set", key, "--repo", repo, "--body", value])
    print("Verified GitHub environments and configured their federated identities.")


if __name__ == "__main__":
    main()
