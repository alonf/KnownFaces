import copy
import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from scripts.azure_release import digest, inspect_plan, require_review
from scripts.package_gateway import package
from scripts.publish_gateway import publish

GROUP = "/subscriptions/00000000-0000-4000-8000-000000000001/resourceGroups/fixture"
INPUT = {
    "group_id": GROUP,
    "state": "blocked",
    "commit": "a" * 40,
    "parameters": {
        "siteName": "fixture",
        "packageSha": "0" * 64,
        "gatewaySha": "1" * 64,
    },
    "template_sha": "2" * 64,
}


def prediction():
    return {
        "status": "Succeeded",
        "changes": [
            {
                "resourceId": GROUP + "/providers/Microsoft.Web/sites/fixture",
                "changeType": "Modify",
                "after": {"properties": {"privateField": "sentinel-do-not-log"}},
                "delta": [
                    {
                        "path": "properties.privateField",
                        "propertyChangeType": "Modify",
                        "after": "sentinel-do-not-log",
                    }
                ],
            }
        ],
    }


def test_package_is_deterministic_source_only():
    payload, package_id, code_id = package()
    assert (payload, package_id, code_id) == package()
    with ZipFile(BytesIO(payload)) as archive:
        assert archive.namelist() == ["gateway.cjs"]
        assert b"8093" not in archive.read("gateway.cjs")


def test_review_binds_target_and_private_changes_without_rendering_values():
    raw = prediction()
    old = inspect_plan(raw, INPUT)
    assert not {"group_id", "parameters"} & old.keys()
    assert GROUP not in json.dumps(old) and "fixture" not in json.dumps(old)
    assert "sentinel" not in json.dumps(old)
    changed = copy.deepcopy(INPUT)
    changed["parameters"]["siteName"] = "different"
    with pytest.raises(SystemExit):
        require_review(inspect_plan(raw, changed), old, digest(old))
    require_review(old, old, digest(old))
    for field, changed in [
        ("state", "synthetic"),
        ("commit", "b" * 40),
        ("template_sha", "0" * 64),
        ("target_sha", "0" * 64),
    ]:
        with pytest.raises(SystemExit):
            require_review(old | {field: changed}, old, digest(old))
    drift = copy.deepcopy(raw)
    drift["changes"][0]["after"]["properties"]["privateField"] = "different"
    with pytest.raises(SystemExit):
        require_review(inspect_plan(drift, INPUT), old, digest(old))


@pytest.mark.parametrize(
    "changes",
    [
        {"changeType": "Ignore"},
        {"changeType": "Unsupported"},
        {"changeType": "Delete"},
        {"diagnostics": [{}]},
        {"resourceId": "/outside/the/group"},
        {"after": {"key": "[listKeys('x','y')]"}},
        {"after": None},
        {"delta": []},
    ],
)
def test_unreliable_what_if_stops(changes):
    raw = prediction()
    raw["changes"][0].update(changes)
    with pytest.raises(ValueError):
        inspect_plan(raw, INPUT)


def test_activation_requires_existing_resources():
    raw = prediction()
    raw["changes"][0]["changeType"] = "Create"
    with pytest.raises(ValueError):
        inspect_plan(raw, INPUT | {"state": "synthetic"})
    raw["diagnostics"] = [{"message": "sentinel"}]
    with pytest.raises(ValueError, match="diagnostics"):
        inspect_plan(raw, INPUT)


def test_activation_accepts_only_three_switches():
    raw = prediction()
    change = raw["changes"][0]

    def site(network, access, state):
        return {
            "properties": {
                "publicNetworkAccess": network,
                "siteConfig": {
                    "ipSecurityRestrictionsDefaultAction": access,
                    "scmIpSecurityRestrictionsDefaultAction": "Deny",
                    "appSettings": [{"name": "KF_EDGE_STATE", "value": state}],
                },
            }
        }

    change["before"] = site("Disabled", "Deny", "blocked")
    change["after"] = site("Enabled", "Allow", "synthetic")
    assert inspect_plan(raw, INPUT | {"state": "synthetic"})["state"] == "synthetic"
    change["after"]["properties"]["siteConfig"]["httpLoggingEnabled"] = True
    with pytest.raises(ValueError, match="unrelated"):
        inspect_plan(raw, INPUT | {"state": "synthetic"})


@pytest.mark.parametrize("access", ["Disabled", "Enabled"])
def test_publication_requires_blocked_site(access, tmp_path):
    payload, sha, source_sha = package()
    manifest = {"state": "blocked", "package_sha": sha, "gateway_sha": source_sha}
    calls = []

    def azure(*args):
        calls.append(args[:3])
        if args[:2] == ("webapp", "show"):
            return json.dumps(access)
        if args[:3] == ("webapp", "config", "show"):
            return json.dumps(
                {
                    "ipSecurityRestrictionsDefaultAction": "Deny",
                    "scmIpSecurityRestrictionsDefaultAction": "Deny",
                }
            )
        if args[:3] == ("storage", "account", "list"):
            return '["fixture"]'
        if args[:3] == ("storage", "blob", "exists"):
            return "false"
        if args[:3] == ("storage", "blob", "upload"):
            assert args[args.index("--if-none-match") + 1] == "*"
            return b""
        if args[:3] == ("storage", "blob", "download"):
            (tmp_path / "download.zip").write_bytes(payload)
            return b""
        pytest.fail("Unexpected Azure command")

    if access == "Enabled":
        with pytest.raises(SystemExit, match="blocked site"):
            publish(manifest, digest(manifest), azure, "group", "site", tmp_path)
        assert ("storage", "blob", "upload") not in calls
    else:
        publish(manifest, digest(manifest), azure, "group", "site", tmp_path)
        assert calls.count(("webapp", "config", "show")) == 2
        assert calls.count(("storage", "blob", "upload")) == 1
