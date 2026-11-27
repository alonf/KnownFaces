# Deploy KnownFaces

GitHub Actions deploys the Linux B1 HTTPS gateway and Azure Relay. An Ubuntu x86-64 guest at home runs Docker. The guest holds the gallery, local UI, dedicated OAuth MCP service and Relay listener. The host launcher supports the trusted SSH configuration of an existing Ring MCP guest and a separate Hyper-V guest. The dedicated path uses the Ring project's MIT-licensed qcow2 converter, a Generation 2 Hyper-V VM, Secure Boot and a NoCloud seed. On first shutdown the host reads the guest's public SSH key directly from the seed disk, detaches it and pins that key for SSH. Ring services, credentials and Docker volumes are outside the KnownFaces project.

## Deployment inputs

Start from the reviewed source release on protected `main`. Use Python 3.13, Git, Azure CLI, GitHub CLI, the pinned Bicep from `scripts/install-bicep.sh`, and PowerShell 7.2+ for the home launcher. Copy `infra/operator.example.json` to a private directory outside the checkout. Supply the subscription, two dedicated group names, region, globally unique site name, GitHub repository, independent reviewer, home operator's Entra user object ID and the one permitted ChatGPT user's object ID. For the initial gallery use only synthetic owner-mapped camera IDs and alias-to-person UUIDs. Keep recognition and management disabled. The release builds the recognition image on the guest from the reviewed commit. Model files and gallery contents do not travel through GitHub.

## Bootstrap access

The initial trust relationship requires an existing Azure operator with the relevant group, identity, role-definition and role-assignment permissions, and a GitHub repository administrator. The release identities cannot grant themselves privileges. Use an authenticated operator session for the bootstrap Bicep. For a new subscription register Microsoft.Web, Microsoft.Storage, Microsoft.Relay and Microsoft.ManagedIdentity using the platform bootstrap described below.

For each stage run the preview, review the Bicep and its sanitized record, then apply the independently approved digest. Run from the clean release checkout. This example uses PowerShell.

```powershell
$cfg = 'C:\KnownFaces-Private\operator.json'
$private = Split-Path $cfg
$sha = (git rev-parse HEAD).Trim()
$stage = 'providers'
python -m scripts.bootstrap_infra $stage --config $cfg --record "$private/$stage.json"
# After review, substitute the digest printed by the preview.
python -m scripts.bootstrap_infra $stage --config $cfg --approved "$private/$stage.json" --digest <approved-digest>
```

Repeat for `groups`, then `trust`. Configure GitHub before previewing or applying `permissions`. The groups stage creates or reconciles the two resource groups. Trust creates three environment-bound managed identities in the trust group. The publisher can receive blob write access only to the gateway container in the later access stage.

```powershell
python -m scripts.configure_github --config $cfg --apply
```

This creates a main-branch ruleset and main-only environments with an independent reviewer, configures OIDC IDs and target settings, and enables the manual workflow. Publish the reviewed source before bootstrapping the GitHub settings. For every apply or publication, the named independent reviewer approves the GitHub environment with the exact comment `approve <review-digest>`. Before Azure login the job checks GitHub's approval history for that reviewer, comment, environment and run. An administrator bypass without that approval fails this check. GitHub's environment REST API does not expose an administrator-bypass toggle. The script fails closed if the account plan cannot enforce the required environment protections. After configuring GitHub, preview and separately apply the `permissions` stage. It reads the effective branch and environment protections again before granting workload roles. Initial source publication, identity creation and each Azure apply retain their approval boundaries.

## Deploy Azure through Actions

Use the **KnownFaces Azure** workflow on protected main. The commands below show the first deployment.

```powershell
gh workflow run azure.yml --ref main -f operation=preview -f preview_target=foundation
gh run list --workflow azure.yml --event workflow_dispatch --limit 5
gh run view <preview-run-id> --log
gh workflow run azure.yml --ref main -f operation=provision-foundation -f reviewed_run=<preview-run-id> -f approved_digest=<review-digest>
```

1. **Foundation** creates the blocked site, HTTPS and SCM restrictions, storage, private container, Relay namespace, Hybrid Connection requiring client authorization. The App Service plan starts billing at this step. The Bicep separates the Web configuration so ARM can show the effective ingress switches. Review evidence for a concealed platform field is limited to an exact compiled identity, fixed endpoint or entity Send-key expression. Other redactions, ambiguous changes, diagnostics and drift stop deployment. The complete provider plan and compiled template are digest-bound.
2. **Resource access** uses the same bootstrap preview and apply commands with `$stage = 'resource-access'` after the site and Hybrid Connection exist. The access administrator creates the Send-only and Listen-only rules here. This gives the home operator access to the Listen key, the deploy identity access to the Send key and package bytes, the site identity package read access, and the publisher access to write the package. Bindings are scoped to the specific rule or container. Allow time for RBAC propagation.
   Organizations with delegated access-administration OIDC identities can also run this stage entirely in GitHub Actions. Set `accessWorkflowClients` in the private operator config to an object containing separate `preview` and `apply` client IDs before running `configure_github`. Their federated subjects are the `knownfaces-access-preview` and `knownfaces-access-apply` environments. Both require read access to the three release identities and the site plus deployment what-if. The apply identity additionally needs deployment write, authorization-rule write on the dedicated Hybrid Connection and delegated role-assignment rights at the specific container and rule scopes. The `azure-access.yml` workflow takes `operation=preview`, then `operation=apply` with the separate reviewed run and digest. If your organization has no such delegated identities, use the same resource-access Bicep from the privileged bootstrap session. Application deploy and publisher identities receive no RBAC administration rights.
3. **Blocked binding** uses `operation=preview, preview_target=blocked`, then `operation=provision-blocked` with that preview's run ID and digest. ARM resolves the Send key for the gateway. Keep ingress blocked throughout publication.
4. **Publish the gateway** using the same reviewed blocked plan, `operation=publish-gateway`, its `reviewed_run` and `approved_digest`. The separate publication environment authorizes only the content-addressed ZIP. The job verifies its bytes and rechecks the site's restrictions.
5. **Activate** only after home installation and listener setup. Use `operation=preview, preview_target=synthetic`, then `operation=activate-synthetic` with that new review. Activation verifies the deployed identity, Relay routing and rights, and package bytes. Only the public-network switch, main-site default rule and gateway mode should change. SCM stays closed.

## Configure the private Entra client

The API and client can be created before a connector exists. Omit the optional redirect on the first run, then rerun with the exact callback displayed by ChatGPT. This preserves the client secret. Run the script as the tenant operator authorized to configure the two dedicated registrations and grant consent to the single approved user.

```powershell
python -m scripts.configure_entra --config $cfg --private-dir "$private\connector" --apply
```

The script configures a v2 API, registers the three delegated scopes, grants read to one user, and writes `policy.json` and `connector.json` locally. Use a private user-only directory. The client secret is used only in ChatGPT. For a new gallery, start with empty subjects, enroll the synthetic visitor locally, then add its alias through the reviewed policy installation below. Keep the private policy and connector credential on the home side.

## Run one host script

Run from the same release checkout on the Windows Hyper-V host. Choose one install command. Omit `-Apply` for a preview. The dedicated mode requires enabled Hyper-V and hardware virtualization. It verifies Canonical's Ubuntu cloud image checksum and creates a 4-vCPU, 6 GiB, 40 GiB Ubuntu 24.04 guest. Use an elevated host terminal for that mode. Existing mode expects an x86-64 Ubuntu guest, trusted SSH host key, key authentication and passwordless sudo for the operator. A previously configured Ring SSH alias works. Docker and Compose v2 are installed from Ubuntu if missing. Choose a guest with free space for model downloads and gallery growth.

```powershell
$hostScript = '.\infra\home\Deploy-KnownFaces.ps1'
$guest = @{ Mode='Existing'; SshHost='<trusted Ring SSH alias>'; SshUser='bridge' }
# For a dedicated Hyper-V guest use this instead:
# $guest = @{ Mode='New'; VMName='knownfaces'; SwitchName='External'; SshPublicKeyPath="$HOME\.ssh\id_ed25519.pub" }
& $hostScript @guest -Action Install -Release $sha -PolicyPath "$private\connector\policy.json" -Apply
```

Install builds both images before replacing containers, mounts the named `knownfaces-home_gallery` volume, checks service health and leaves the listener stopped on first install. The UI listens on guest loopback 8093. For an existing guest, `ssh -N -L 18093:127.0.0.1:8093 <trusted-alias>` makes it available on the workstation. The synthetic gallery already on a different machine is a separate volume. Use an approved backup/restore if moving any existing gallery. To update synthetic mappings, supply the desired private policy and run:

```powershell
& $hostScript @guest -Action Policy -Release $sha -PolicyPath "$private\connector\policy.json" -Apply
```

The policy action accepts only camera and subject mapping changes and reloads the dedicated MCP container.

```powershell
$settings = Get-Content $cfg -Raw | ConvertFrom-Json
$names = @(az relay namespace list --subscription $settings.subscription -g $settings.resourceGroup --query '[].name' -o tsv)
if ($LASTEXITCODE -ne 0 -or $names.Count -ne 1) { throw 'Expected one dedicated Relay namespace' }
& $hostScript @guest -Action Connect -Release $sha -SubscriptionId $settings.subscription -ResourceGroup $settings.resourceGroup -RelayNamespace $names[0] -Apply
```

Connect obtains only `mcp/home-listen` on the home host and transfers it on the authenticated guest channel. Microsoft HCM 1.3.0 imports a private JSON file. It shares an internal Docker network with the MCP service on 8094 and a TLS CONNECT proxy restricted to public Microsoft Relay hostnames. Its containers have no published ports or gallery mounts. The SHA-256 of the Microsoft archive is pinned in the Dockerfile. A vendor update to the archive needs a reviewed checksum update.

## Update and roll back

When already signed into the Linux guest, the equivalent local entry points are `bash infra/home/knownfaces-home.sh Install "$(git rev-parse HEAD)" /private/policy.json` and `bash infra/home/knownfaces-home.sh Update "$(git rev-parse HEAD)"`. To connect from the guest, use `Connect SHA SUBSCRIPTION GROUP NAMESPACE` with a home-side Azure CLI login authorized for the Listen rule. Both launchers use the same guest installer.

```powershell
& $hostScript @guest -Action Update -Release (git rev-parse HEAD) -Apply
& $hostScript @guest -Action Disconnect -Release (git rev-parse HEAD) -Apply
```

Update rebuilds from the supplied commit, preserves the policy and volume, waits for health, and returns to the previous installed release if startup fails. Use `Update` with the previous full SHA for an explicit image rollback. A release with a database migration still needs a compatible backup and migration plan. Disconnect immediately removes the dedicated listener and proxy. Reconcile the Azure site to `blocked` through a fresh preview and protected apply. The local UI and recognition remain available during a Relay outage.

## ChatGPT acceptance

In ChatGPT on the web add a private custom MCP server, choose Server URL, and use the deployed canonical `https://<actual-site-host>/mcp`. Select OAuth with the client ID and secret from the private connector file. Run the Entra configuration command again with `--redirect <exact-ChatGPT-callback>` before completing sign-in. Record discovery, redirect, S256, consent, one bounded synthetic read, one denied edit, unchanged gallery, and refresh. The tenant previously omitted S256 from issuer discovery. Direct Entra compatibility and Azure streaming require a live test. [ChatGPT connection guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) · [Microsoft Hybrid Connections](https://learn.microsoft.com/en-us/azure/app-service/app-service-hybrid-connections)
