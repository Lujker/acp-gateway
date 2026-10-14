"""Authenticated connector profiles, persisted without changing the database schema."""

from acp_gateway.agents import AgentClient
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.protocol import AgentManifest

NAMESPACE = "connector_agent_catalog_v2"


class ConnectorCatalog:
    def __init__(self, core, dispatcher, *, pin_dir):
        self.core = core
        self.dispatcher = dispatcher
        self.pin_dir = pin_dir
        self.managed = set()

    def _profile(self, computer_id, manifest):
        return AgentProfile(
            alias=manifest.alias,
            title=manifest.display_name,
            kind=manifest.kind,
            backend="connector",
            computer_id=computer_id,
            default_cwd=manifest.default_cwd,
            session_mode=manifest.session_mode,
        )

    def _publish(self, computer_id, manifests):
        profiles = [self._profile(computer_id, a) for a in manifests]
        for profile in profiles:
            address = profile.address
            if address in self.core.agents:
                # Existing sessions keep their recorded cwd; new sessions use local defaults.
                self.core.agent(address).profile = profile
            else:
                self.core.add_agent(
                    AgentClient(
                        profile,
                        None,
                        pin_dir=self.pin_dir,
                        transport_factory=lambda p=profile: self.dispatcher.open_agent(
                            p.computer_id, p.alias
                        ),
                    )
                )
            self.managed.add(address)
        return {p.address for p in profiles}

    def restore(self):
        for computer in self.core.store.computers.list():
            if not computer["enabled"]:
                continue
            computer_id = computer["computer_id"]
            value = self.core.store.channel_state(NAMESPACE, computer_id, [])
            manifests = [AgentManifest.model_validate(a) for a in value]
            self._publish(computer_id, manifests)

    async def registered(self, registration):
        # Version 1 has aliases only: retain explicitly configured legacy routes.
        if not all(a.kind is not None and a.default_cwd is not None for a in registration.agents):
            return
        computer_id = registration.computer_id
        current = self._publish(computer_id, registration.agents)
        self.core.store.set_channel_state(
            NAMESPACE,
            computer_id,
            [a.model_dump(mode="json") for a in registration.agents],
        )
        retired = {
            address
            for address in self.managed
            if address.startswith(computer_id + "/") and address not in current
        }
        self.managed.difference_update(retired)
        await self.core.remove_agents(retired)
