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
        local_report=report, release_lookup=lambda: calls.append(1) or Version("0.3.0")
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
