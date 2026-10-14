"""Read-only connector and ACP readiness snapshots shared by user interfaces."""


def computer_diagnostics(core, dispatcher):
    computers = dispatcher.status()
    indexed = {row["computer_id"]: row for row in computers}
    routes = []
    for address, client in core.agents.items():
        if client.profile.backend != "connector":
            continue
        computer = indexed.get(client.profile.computer_id)
        connected = bool(computer and computer["connected"])
        advertised = bool(connected and client.profile.alias in computer["advertised_agents"])
        ready = connected and advertised and client.connected
        registration = dispatcher.active.get(client.profile.computer_id)
        stream_status = (
            registration.relay.route_status(client.profile.alias)
            if connected and advertised
            else {
                "epoch": None,
                "active_streams": 0,
                "capacity_available": False,
                "last_stream_error": None,
            }
        )
        failure = stream_status["last_stream_error"]
        if ready:
            status, problem = "ready", None
        elif computer is None:
            status, problem = "offline", "not_enrolled"
        elif not computer["enabled"]:
            status, problem = "offline", "access_disabled"
        elif not connected:
            status, problem = "offline", "computer_offline"
        elif not advertised:
            status, problem = "unadvertised", "agent_unadvertised"
        else:
            status = "not_initialized"
            problem = failure["code"] if failure else "agent_not_initialized"
        routes.append(
            {
                "address": address,
                "computer_id": client.profile.computer_id,
                "alias": client.profile.alias,
                "computer_connected": connected,
                "advertised": advertised,
                "agent_ready": ready,
                "problem": problem,
                **stream_status,
                "status": status,
            }
        )
    return {"computers": computers, "routes": routes}
