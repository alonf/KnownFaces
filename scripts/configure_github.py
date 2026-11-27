"""Configure deployment trust on the specified GitHub repository after the trust Bicep stage."""

import argparse
import json
import shutil
from pathlib import Path
from uuid import UUID

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
                required = {c["context"] for c in expected}
                meets = required <= {c["context"] for c in actual or []}
            elif isinstance(expected, bool):
                meets = isinstance(actual, bool) and (actual or not expected)
            else:
                meets = actual == expected
            if not meets:
                raise SystemExit(f"Inadequate release protection: {kind}.{field}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    c = config(args.config)
    if not args.apply:
        print(
            "Would add a protected main ruleset, main-only reviewed environments, OIDC settings and release switches for",
            c["repository"],
        )
        return
    gh = shutil.which("gh")
    repo = c["repository"]
    prefix = "repos/" + repo

    def api(route, method="GET", body=None):
        data = command(
            [gh, "api", "--method", method, route]
            + (["--input", "-"] if body is not None else []),
            json.dumps(body).encode() if body is not None else None,
        )
        return json.loads(data) if data else None

    reviewer = api("users/" + c["reviewer"])["id"]
    tenants = azure("account", "show", "--subscription", c["subscription"])["tenantId"]
    principals = identities(c)
    access_clients = c.get("accessWorkflowClients", {})
    if access_clients:
        if (
            set(access_clients) != {"preview", "apply"}
            or len(set(access_clients.values())) != 2
        ):
            raise SystemExit(
                "Provide distinct, preauthorized access preview and apply client IDs"
            )
        if {i["client"] for i in principals} & set(access_clients.values()):
            raise SystemExit(
                "Access administration requires identities separate from the release jobs"
            )
        principals.extend(
            {"environment": "access-" + k, "client": str(UUID(v))}
            for k, v in access_clients.items()
        )
    rule = dict(
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
                    "required_status_checks": [{"context": "verify"}],
                },
            },
        ],
    )
    matches = [r for r in api(prefix + "/rulesets") if r["name"] == rule["name"]]
    if not matches:
        ruleset_id = api(prefix + "/rulesets", "POST", rule)["id"]
    elif len(matches) != 1:
        raise SystemExit("Ambiguous KnownFaces main ruleset")
    else:
        ruleset_id = matches[0]["id"]
    verify_release_ruleset(api(prefix + "/rulesets/" + str(ruleset_id)), rule)
    existing = {
        e["name"] for e in api(prefix + "/environments?per_page=100")["environments"]
    }
    for item in principals:
        name = "knownfaces-" + item["environment"]
        endpoint = prefix + "/environments/" + name
        if name not in existing:
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
        if (
            len(required) != 1
            or not required[0]["prevent_self_review"]
            or not any(
                r["reviewer"]["id"] == reviewer for r in required[0]["reviewers"]
            )
            or [(v.get("type"), v["name"]) for v in policies] != [("branch", "main")]
        ):
            raise SystemExit(
                "GitHub could not verify reviewer and main-only protections for " + name
            )
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
        "KF_ACCESS_ENABLED": "true" if access_clients else "false",
    }.items():
        command([gh, "variable", "set", key, "--repo", repo, "--body", value])
    print("Verified GitHub environments and configured their federated identities.")


if __name__ == "__main__":
    main()
