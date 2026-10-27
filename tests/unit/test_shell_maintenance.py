"""Shell activity participates in the existing connector reservation policy."""

from types import SimpleNamespace

import pytest

from acp_gateway.connectors.maintenance import ConnectorMaintenance


def test_extra_shell_work_and_uncertain_cleanup_prevent_idle_reservation():
    status = {"running_sessions": 1, "uncertain_sessions": 0}
    controller = ConnectorMaintenance([], {})
    controller.services.append(SimpleNamespace(maintenance_status=lambda: status))
    assert controller.status()["running_sessions"] == 1
    assert not controller.status()["idle_observed"]
    with pytest.raises(ValueError, match="busy"):
        controller.apply("prepare", restart_uncertain=True)
    status.update(running_sessions=0, uncertain_sessions=1)
    with pytest.raises(ValueError, match="busy"):
        controller.apply("prepare")
    # Existing explicit owner acknowledgement remains available for restart;
    # ordinary automatic updater admission cannot ignore uncertainty.
    token = controller.apply("prepare", restart_uncertain=True)["token"]
    controller.apply("resume", token)


@pytest.mark.parametrize(
    "status", [{"running_sessions": -1}, {"running_sessions": True}, {"unknown_field": 1}]
)
def test_malformed_activity_cannot_be_reported_as_idle(status):
    controller = ConnectorMaintenance([], {})
    controller.services.append(SimpleNamespace(maintenance_status=lambda: status))
    with pytest.raises(ValueError, match="Invalid local activity"):
        controller.status()
