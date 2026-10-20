"""Read-only update inventory, paging and explicit release lookup."""

import asyncio
from types import SimpleNamespace

import pytest
from packaging.version import Version
from pydantic import ValidationError

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.connectors.protocol import InstallationReport, ProtocolError, decode_frame
from acp_gateway.updates import UpdateInventory


def report(version="0.2.0", **kwargs):
    return InstallationReport(
        installed_version=version, manager="uv", source="registry", platform="win32", **kwargs
    )


async def test_inventory_is_read_only_paged_and_does_not_check_release_implicitly():
    called = []

    async def remote(target):
        called.append(target)
        if target == "pc01":
            raise AgentUnavailable("private diagnostic")
        return report()

    dispatcher = SimpleNamespace(
        status=lambda: [{"computer_id": f"pc{i:02}", "connected": i != 0} for i in range(12)],
        update_info=remote,
    )
    service = UpdateInventory(
        dispatcher,
        package_lookup=lambda version: True,
        local_report=report,
        release_lookup=lambda: pytest.fail("implicit network access"),
    )
    first = await service.targets()
    assert len(first["targets"]) == 11 and first["more"]
    assert first["targets"][1]["state"] == "offline"
    assert first["targets"][2]["state"] == "unavailable"
    assert "pc00" not in called
    assert "private diagnostic" not in str(first)
    assert all(not t["report"].can_update for t in first["targets"] if t["report"])
    second = await service.targets(page=1)
    assert len(second["targets"]) == 3 and not second["more"]
    assert second["targets"][1]["target"] == "pc10"


async def test_explicit_release_lookup_is_cached_and_shared_by_concurrent_requests():
    calls = []
    service = UpdateInventory(
        package_lookup=lambda version: True,
        local_report=report,
        release_lookup=lambda: calls.append(1) or Version("0.3.0"),
    )
    assert (await service.targets())["latest"] is None
    await asyncio.gather(*(service.check_release() for _ in range(8)))
    result = await service.targets()
    assert calls == [1]
    assert result["latest"] == "0.3.0"
    assert result["targets"][0]["newer_stable"]


@pytest.mark.parametrize(
    "change",
    [{"can_update": "yes"}, {"secret": "private"}, {"installed_version": "private\nvalue"}],
)
def test_inventory_rejects_invalid_capability_or_unbounded_fields(change):
    value = report().model_dump()
    value.update(change)
    with pytest.raises(ValidationError):
        InstallationReport.model_validate(value)


def test_update_mutation_is_not_part_of_inventory_protocol():
    with pytest.raises(ProtocolError):
        decode_frame('{"version":1,"type":"update_install","target":"private"}')


async def test_docker_cannot_offer_pypi_only_release():
    docker = report().model_copy(
        update={"manager": "docker", "platform": "linux", "can_update": True}
    )
    service = UpdateInventory(
        package_lookup=lambda version: True,
        local_report=lambda: docker,
        release_lookup=lambda: Version("0.3.0"),
        image_lookup=lambda version: None,
    )
    await service.check_release()
    target = (await service.targets())["targets"][0]
    assert target["available_version"] is None
    assert target["newer_stable"] is False
    assert target["artifact_state"] == "unpublished"


def test_cli_release_resolution_uses_the_same_backend_artifact_gate():
    from acp_gateway.updates import available_stable_release

    docker = report().model_copy(update={"manager": "docker", "platform": "linux"})
    assert available_stable_release(
        docker, release_lookup=lambda: Version("0.3.0"), image_lookup=lambda version: None
    ) == (None, "unpublished")
    assert available_stable_release(
        report(), release_lookup=lambda: Version("0.3.0"), package_lookup=lambda version: True
    ) == (
        "0.3.0",
        "published",
    )


@pytest.mark.parametrize("files", [[], [{"yanked": True, "packagetype": "bdist_wheel"}]])
def test_exact_uv_selection_cannot_claim_unpublished_or_yanked_release(files):
    import httpx

    from acp_gateway.updates import available_exact_release

    response = {
        "info": {
            "version": "0.3.0",
            "project_urls": {"Source": "https://github.com/Lujker/acp-gateway"},
        },
        "urls": files,
    }
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert available_exact_release(report(), "0.3.0", client=client) == (None, "unpublished")
    assert seen == ["https://pypi.org/pypi/acp-gateway/0.3.0/json"]


@pytest.mark.parametrize(
    "file",
    [
        {"packagetype": "bdist_wheel", "filename": "acp_gateway-0.3.0-cp39-cp39-manylinux.whl"},
        {
            "packagetype": "bdist_wheel",
            "filename": "acp_gateway-0.3.0-py3-none-any.whl",
            "requires_python": ">=3.13",
        },
    ],
)
def test_uv_does_not_offer_incompatible_artifacts(file):
    import httpx

    from acp_gateway.updates import available_exact_release

    response = {
        "info": {
            "version": "0.3.0",
            "project_urls": {"Source": "https://github.com/Lujker/acp-gateway"},
        },
        "urls": [file],
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        assert available_exact_release(report(), "0.3.0", client=client) == (None, "unpublished")


async def test_latest_uv_version_requires_usable_published_artifact():
    service = UpdateInventory(
        local_report=report,
        release_lookup=lambda: Version("0.3.0"),
        package_lookup=lambda version: False,
    )
    await service.check_release()
    target = (await service.targets())["targets"][0]
    assert target["available_version"] is None
    assert target["artifact_state"] == "unpublished"
    assert target["newer_stable"] is False


async def test_package_lookup_network_failure_is_not_an_available_update():
    import httpx

    def lookup(version):
        raise httpx.ConnectError("private diagnostic")

    service = UpdateInventory(
        local_report=report, release_lookup=lambda: Version("0.3.0"), package_lookup=lookup
    )
    await service.check_release()
    target = (await service.targets())["targets"][0]
    assert target["artifact_state"] == "unavailable"
    assert target["available_version"] is None
    assert "private diagnostic" not in str(target)
