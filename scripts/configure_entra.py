"""Reconcile the single-tenant resource and private connector registrations for the read pilot."""

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from scripts.bootstrap_infra import azure, config


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", type=Path, required=True)
    cli.add_argument("--private-dir", type=Path, required=True)
    cli.add_argument("--redirect", required=True)
    cli.add_argument("--apply", action="store_true")
    args = cli.parse_args()
    c = config(args.config)
    redirect = urlsplit(args.redirect)
    if (
        redirect.scheme != "https"
        or redirect.netloc != "chatgpt.com"
        or redirect.query
        or redirect.fragment
        or not redirect.path.startswith("/")
    ):
        cli.error("Supply the exact HTTPS callback displayed by ChatGPT")
    dest = args.private_dir.resolve()
    if dest.is_relative_to(Path.cwd().resolve()):
        cli.error("Choose a private directory outside the release checkout")
    if not args.apply:
        print(
            "Would configure a single-tenant API, a dedicated ChatGPT client, and delegated read consent for one user."
        )
        return
    tenant = azure("account", "show", "--subscription", c["subscription"])["tenantId"]
    host = azure(
        "webapp",
        "show",
        "--subscription",
        c["subscription"],
        "-g",
        c["resourceGroup"],
        "-n",
        c["siteName"],
    )["defaultHostName"]
    if not host.endswith(".azurewebsites.net"):
        raise SystemExit("Unexpected App Service hostname")
    dest.mkdir(mode=0o700, exist_ok=True)

    def graph(method, path, body=None):
        flags = [
            "rest",
            "--subscription",
            c["subscription"],
            "--method",
            method,
            "--url",
            "https://graph.microsoft.com/v1.0/" + path,
        ]
        if body is None:
            return azure(*flags)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "request.json"
            file.write_text(json.dumps(body))
            return azure(
                *flags,
                "--headers",
                "Content-Type=application/json",
                "--body",
                "@" + str(file),
            )

    def find(kind, condition):
        page = graph("GET", kind + "?" + urlencode({"$filter": condition}))
        if page.get("@odata.nextLink") or len(page["value"]) > 1:
            raise SystemExit("Ambiguous identity or consent")
        return next(iter(page["value"]), None)

    marker = "KnownFaces-" + str(
        uuid5(
            NAMESPACE_URL,
            c["subscription"] + "/" + c["resourceGroup"] + "/" + c["siteName"],
        )
    )

    def application(suffix):
        name = c["siteName"] + "-" + suffix
        obj = find("applications", f"displayName eq '{name}'")
        if obj:
            if (
                marker not in obj.get("tags", [])
                or obj["signInAudience"] != "AzureADMyOrg"
            ):
                raise SystemExit(
                    "Application name is already in use outside this deployment"
                )
            return obj
        return graph(
            "POST",
            "applications",
            dict(displayName=name, tags=[marker], signInAudience="AzureADMyOrg"),
        )

    def principal(app):
        return find("servicePrincipals", "appId eq '" + app["appId"] + "'") or graph(
            "POST", "servicePrincipals", {"appId": app["appId"]}
        )

    api, client = application("api"), application("chatgpt")
    scopes = [
        dict(
            id=str(uuid5(UUID(api["appId"]), scope)),
            value="knownfaces." + scope,
            type="Admin",
            isEnabled=True,
            adminConsentDisplayName="KnownFaces " + scope,
            adminConsentDescription="Allow this delegated KnownFaces operation: "
            + scope,
        )
        for scope in ("read", "recognize", "manage")
    ]
    graph(
        "PATCH",
        "applications/" + api["id"],
        dict(
            identifierUris=["api://" + api["appId"]],
            api=dict(requestedAccessTokenVersion=2, oauth2PermissionScopes=scopes),
        ),
    )
    graph(
        "PATCH",
        "applications/" + client["id"],
        dict(
            web={"redirectUris": [args.redirect]},
            requiredResourceAccess=[
                {
                    "resourceAppId": api["appId"],
                    "resourceAccess": [{"type": "Scope", "id": scopes[0]["id"]}],
                }
            ],
        ),
    )
    rsp, csp = principal(api), principal(client)
    binding = f"clientId eq '{csp['id']}' and resourceId eq '{rsp['id']}'"
    grants = graph("GET", "oauth2PermissionGrants?" + urlencode({"$filter": binding}))
    for grant in grants["value"]:
        if (
            grant.get("principalId") != c["authorizedUser"]
            or grant.get("scope") != "knownfaces.read"
            or grant.get("consentType") != "Principal"
        ):
            raise SystemExit(
                "Unexpected existing consent; review access before proceeding"
            )
    if grants.get("@odata.nextLink"):
        raise SystemExit("Unexpected number of delegated grants")
    if not grants["value"]:
        graph(
            "POST",
            "oauth2PermissionGrants",
            dict(
                clientId=csp["id"],
                resourceId=rsp["id"],
                consentType="Principal",
                principalId=c["authorizedUser"],
                scope="knownfaces.read",
            ),
        )
    policy = dict(
        resource="https://" + host + "/mcp",
        tenant_id=tenant,
        api_id=api["appId"],
        connector_id=client["appId"],
        authorized_user_oid=c["authorizedUser"],
        recognize_enabled=False,
        manage_enabled=False,
        cameras=c.get("cameras", {}),
        subjects=c.get("subjects", {}),
        snapshots={},
    )

    def exclusive(name, value):
        with os.fdopen(
            os.open(dest / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as f:
            json.dump(value, f, indent=2)

    if (dest / "policy.json").exists():
        if json.loads((dest / "policy.json").read_bytes()) != policy:
            raise SystemExit(
                "Private policy differs; preserve and review the owner's mappings"
            )
    else:
        exclusive("policy.json", policy)
    if not (dest / "connector.json").exists():
        if client.get("passwordCredentials"):
            raise SystemExit(
                "A connector secret already exists. Supply its private record or approve rotation."
            )
        credential = graph(
            "POST",
            "applications/" + client["id"] + "/addPassword",
            {
                "passwordCredential": {
                    "displayName": "KnownFaces private ChatGPT connector"
                }
            },
        )
        exclusive(
            "connector.json",
            dict(
                client_id=client["appId"],
                client_secret=credential["secretText"],
                expires=credential["endDateTime"],
            ),
        )
    elif (
        json.loads((dest / "connector.json").read_bytes())["client_id"]
        != client["appId"]
    ):
        raise SystemExit(
            "Existing connector credential belongs to another registration"
        )
    print(
        "Private policy and connector credential are ready in the selected directory."
    )


if __name__ == "__main__":
    main()
