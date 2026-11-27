import asyncio
import copy
import runpy

import pytest

from scripts.azure_release import digest, inspect_plan


def test_home_proxy_rejects_private_services_and_non_tls_ports():
    handler = runpy.run_path("infra/home/relay-proxy.py")["connect"]

    async def exercise():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            for target in (
                "knownfaces-mcp.invalid:8094",
                "127.0.0.1:8093",
                "fixture.servicebus.windows.net:80",
                "fixture.servicebus.windows.net.example.invalid:443",
            ):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", server.sockets[0].getsockname()[1]
                )
                writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
                await writer.drain()
                assert b"403 Forbidden" in await asyncio.wait_for(reader.read(), 2)
                writer.close()
                await writer.wait_closed()

    asyncio.run(exercise())


GROUP = "/subscriptions/00000000-0000-4000-8000-000000000001/resourceGroups/example"


def creation():
    template = {
        "resources": [
            {"type": "Microsoft.Web/sites", "identity": {"type": "SystemAssigned"}}
        ]
    }
    inputs = dict(
        group_id=GROUP,
        state="foundation",
        commit="a" * 40,
        template=template,
        template_sha=digest(template),
        parameters=dict(siteName="fixture", packageSha="1" * 64, gatewaySha="2" * 64),
    )
    change = dict(
        resourceId=GROUP + "/providers/Microsoft.Web/sites/fixture",
        changeType="Create",
        after={
            "identity": "********",
            "properties": {"httpsOnly": True, "publicNetworkAccess": "Disabled"},
        },
    )
    return inputs, {"status": "Succeeded", "changes": [change]}


def test_review_identifies_compiled_identity_and_rejects_opaque_config():
    inputs, raw = creation()
    manifest = inspect_plan(raw, inputs)
    assert manifest["changes"][0]["verified_template_inputs"] == [
        "system-assigned identity"
    ]
    raw["changes"][0]["after"]["properties"]["siteConfig"] = "********"
    with pytest.raises(ValueError, match="Unresolved"):
        inspect_plan(raw, inputs)


def test_mask_cannot_hide_target_access_or_identity_drift():
    inputs, raw = creation()
    other = copy.deepcopy(inputs)
    other["template"]["resources"][0]["identity"]["type"] = "UserAssigned"
    other["template_sha"] = digest(other["template"])
    with pytest.raises(ValueError, match="Unresolved"):
        inspect_plan(raw, other)
    raw["changes"][0]["after"]["properties"]["publicNetworkAccess"] = "Enabled"
    with pytest.raises(ValueError, match="public access"):
        inspect_plan(raw, inputs)


def test_compiled_endpoint_proof_rejects_different_port_and_changed_masked_field():
    inputs, raw = creation()
    resource = {
        "type": "Microsoft.Relay/namespaces/hybridConnections",
        "properties": {
            "userMetadata": '[[{"key":"endpoint","value":"knownfaces-mcp.invalid:8094"}]'
        },
    }
    inputs["template"]["resources"] = [resource]
    inputs["template_sha"] = digest(inputs["template"])
    change = raw["changes"][0]
    change.update(
        resourceId=GROUP
        + "/providers/Microsoft.Relay/namespaces/kf-fixture/hybridConnections/mcp",
        after={"properties": {"userMetadata": "********"}},
    )
    assert inspect_plan(raw, inputs)["changes"][0]["verified_template_inputs"]
    change.update(
        changeType="Modify",
        before=copy.deepcopy(change["after"]),
        delta=[dict(path="properties.userMetadata", propertyChangeType="Modify")],
    )
    with pytest.raises(ValueError, match="Unresolved"):
        inspect_plan(raw, inputs)
    change.update(changeType="Create", delta=[])
    resource["properties"]["userMetadata"] = resource["properties"][
        "userMetadata"
    ].replace("8094", "8093")
    inputs["template_sha"] = digest(inputs["template"])
    with pytest.raises(ValueError, match="Unresolved"):
        inspect_plan(raw, inputs)
