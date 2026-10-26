"""Negotiated administration over real WS, without model or agent launches."""

import asyncio
import time
from uuid import uuid4

import pytest
from test_connector_update_inventory import connected

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.connectors.protocol import (
    AgentManifest,
    ProfilesRequest,
    ProfilesResult,
)


def request(action="list", **kwargs):
    return ProfilesRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        actor="telegram:123",
        expires=time.time() + 90,
        action=action,
        **kwargs,
    )


def result():
    return ProfilesResult(
        status="ok",
        revision="a" * 64,
        agents=[AgentManifest(alias="new", display_name="New", kind="codex", default_cwd="/work")],
    )


async def test_negotiated_profile_catalog_updates_both_peers_without_agent_launch(tmp_path):
    seen = []

    async def handler(frame):
        seen.append(frame)
        return result()

    async with connected(tmp_path, profiles=True, profiles_handler=handler) as (
        dispatcher,
        peer,
        welcome,
    ):
        assert welcome.agent_profiles == 1
        published = []

        async def catalog(registration):
            published.append([a.alias for a in registration.agents])

        dispatcher.on_registration = catalog
        assert await dispatcher.profiles("home", request()) == result()
        assert peer.aliases == {"new"}
        assert dispatcher.active["home"].relay.aliases == {"new"}
        assert published == [["new"]]
        assert len(seen) == 1
        assert not peer.streams


async def test_old_connector_refuses_profile_management(tmp_path):
    async with connected(tmp_path) as (dispatcher, _, welcome):
        assert welcome.agent_profiles is None
        with pytest.raises(AgentUnavailable, match="unavailable"):
            await dispatcher.profiles("home", request())


async def test_wrong_epoch_is_rejected(tmp_path):
    async with connected(tmp_path, profiles=True) as (_, peer, _):
        await peer.send(request())
        async with asyncio.timeout(5):
            await peer.closed.wait()
        assert peer.connection.close_code == 1008


async def test_response_after_revocation_cannot_publish_catalog(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(frame):
        started.set()
        await release.wait()
        return result()

    async with connected(tmp_path, profiles=True, profiles_handler=handler) as (dispatcher, _, _):
        task = asyncio.create_task(dispatcher.profiles("home", request()))
        await started.wait()
        dispatcher.registry.revoke("home")
        release.set()
        with pytest.raises((AgentUnavailable, ConnectionError)):
            await task
