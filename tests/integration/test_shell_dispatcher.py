"""Dispatcher shell durability, full-result privacy and explicit single transfer."""

import asyncio
import secrets
import time

import pytest
from test_telegram import OTHER, OWNER
from test_telegram import telegram as telegram

from acp_gateway.connectors.protocol import ShellResult
from acp_gateway.core import GatewayError
from acp_gateway.core.errors import MaintenanceBlocked, SelectionChanged
from acp_gateway.log import MASK, register_secret
from acp_gateway.shell_dispatcher import PREFIX, ShellDispatcher
from acp_gateway.storage import Conversation, Store


class Remote:
    def __init__(self):
        self.frames = []
        self.output = "done"
        self.fail = False
        self.hook = None
        self.capable = True

    def status(self):
        return [
            {
                "computer_id": "home",
                "enabled": True,
                "connected": True,
                "shell_operations": self.capable,
            }
        ]

    async def shell(self, computer, frame):
        assert computer == "home"
        self.frames.append(frame)
        if self.hook:
            await self.hook(frame)
        if self.fail:
            raise ConnectionError("lost")
        return ShellResult(
            operation=frame.operation,
            state="completed",
            exit_code=0,
            cleanup_verified=True,
            interpreter="sh",
            output=self.output[frame.offset : frame.offset + 4000],
            offset=frame.offset,
            total=len(self.output),
        )


@pytest.fixture
async def shell(telegram):
    core, channel, _, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    await core.new_session(conv)
    client = core.agent("work")
    client.profile = client.profile.model_copy(
        update={"alias": "code", "kind": "codex", "backend": "connector", "computer_id": "home"}
    )
    remote = Remote()
    coordinator = ShellDispatcher(core, remote, channel.namespace)
    channel.connector_dispatcher = remote
    channel._shell_coordinator = coordinator
    return coordinator, conv, remote


async def test_intent_is_committed_before_rpc_and_raw_command_never_persisted(shell, tmp_path):
    coordinator, conv, remote = shell
    credential = secrets.token_hex(32)
    register_secret(credential)

    async def inspect(frame):
        reopened = Store.open(tmp_path / "gateway.db")
        try:
            row = reopened.channel_state(coordinator.namespace, PREFIX + frame.operation.hex)
            assert row["state"] == "dispatching"
            assert credential not in row["command"] and MASK in row["command"]
            assert frame.command == "echo " + credential
        finally:
            reopened.close()

    remote.hook = inspect
    row = await coordinator.start(conv, "echo " + credential)
    assert row["state"] == "completed" and row["output"] == "done"
    assert coordinator.core.running_jobs() == []


async def test_lost_reply_and_restart_only_inspect_status_and_isolate_owners(shell):
    coordinator, conv, remote = shell
    remote.fail = True
    row = await coordinator.start(conv, "echo once")
    assert row["state"] == "unknown" and not row["remote_terminal"]
    with pytest.raises(GatewayError):
        await coordinator.refresh(OTHER, row["id"])
    remote.fail = False
    restarted = ShellDispatcher(coordinator.core, remote, coordinator.namespace)
    restarted.recover()
    result = await restarted.refresh(OWNER, row["id"])
    assert result["output"] == "done"
    assert [f.action for f in remote.frames] == ["start", "status"]
    assert not remote.frames[-1].command


async def test_complete_redaction_before_persisting_windows(shell):
    coordinator, conv, remote = shell
    credential = secrets.token_hex(32)
    register_secret(credential)
    remote.output = "x" * 3990 + credential + "tail"
    seen = []

    async def inspect(frame):
        if frame.action == "status":
            row = coordinator.owned(frame.operation, OWNER)
            seen.append(row["output"])

    remote.hook = inspect
    row = await coordinator.start(conv, "echo")
    assert seen == [""]
    assert row["output"] == "x" * 3990 + MASK + "tail"
    assert credential[:10] not in row["output"]


async def test_recover_does_not_replay_ambiguous_card_or_transfer(shell):
    coordinator, conv, remote = shell
    row = await coordinator.start(conv, "echo")
    row.update(state="dispatching", transfer_state="sending", card_state="sending")
    coordinator.save(row)
    coordinator.recover()
    restored = coordinator.owned(row["id"], OWNER)
    assert restored["state"] == restored["transfer_state"] == restored["card_state"] == "unknown"
    assert len(remote.frames) == 1
    with pytest.raises(GatewayError, match="Проверьте"):
        await coordinator.transfer(OWNER, row["id"], conv, admission=lambda: None)


async def test_transfer_is_explicit_single_and_checks_selection_under_submit_lock(shell):
    coordinator, conv, _ = shell
    row = await coordinator.start(conv, "echo")
    assert not coordinator.core.running_jobs()
    first = await coordinator.transfer(OWNER, row["id"], conv, admission=lambda: None)
    second = await coordinator.transfer(OWNER, row["id"], conv, admission=lambda: None)
    assert first.id == second.id
    await coordinator.core.wait(first.id, 5)
    other = await coordinator.start(conv, "another")
    lock = coordinator.core._locks[conv]
    await lock.acquire()
    pending = asyncio.create_task(
        coordinator.transfer(OWNER, other["id"], conv, admission=lambda: None)
    )
    await asyncio.sleep(0)
    coordinator.core.store.set_active(conv, row["dialogue"])
    lock.release()
    with pytest.raises(SelectionChanged):
        await pending
    assert coordinator.owned(other["id"], OWNER)["transfer_state"] == "available"


async def test_no_start_on_old_peer_or_maintenance_and_intent_failure(shell, monkeypatch):
    coordinator, conv, remote = shell
    remote.capable = False
    with pytest.raises(GatewayError, match="поддерживает"):
        await coordinator.start(conv, "echo")
    remote.capable = True
    token = coordinator.core.prepare_maintenance()
    with pytest.raises(MaintenanceBlocked):
        await coordinator.start(conv, "echo")
    coordinator.core.resume_maintenance(token)

    def failed(*args):
        raise OSError("disk failed")

    monkeypatch.setattr(coordinator.core.store, "set_channel_states", failed)
    with pytest.raises(OSError):
        await coordinator.start(conv, "echo")
    assert not remote.frames and not coordinator.rows()


async def test_index_and_intent_are_rolled_back_together(shell, monkeypatch):
    coordinator, conv, remote = shell
    store = coordinator.core.store
    original = store.set_channel_state

    def fail_second(namespace, key, value):
        if key.startswith(PREFIX):
            raise OSError("disk failure during intent")
        original(namespace, key, value)

    monkeypatch.setattr(store, "set_channel_state", fail_second)
    with pytest.raises(OSError):
        await coordinator.start(conv, "echo")
    assert not coordinator.rows() and not remote.frames
    assert store.channel_state(coordinator.namespace, "shell_operations") is None


async def test_retention_preserves_unknown_and_locked_records(shell):
    coordinator, conv, _ = shell
    terminal = await coordinator.start(conv, "echo")
    uncertain = await coordinator.start(conv, "echo")
    terminal["created"] = uncertain["created"] = time.time() - 8 * 86400
    uncertain.update(state="unknown", remote_terminal=False)
    coordinator.save(terminal)
    coordinator.save(uncertain)
    async with coordinator.locks[terminal["id"]]:
        assert len(coordinator.prune()) == 2
    assert coordinator.prune() == [uncertain["id"]]
    assert terminal["id"] not in coordinator.locks


async def test_cancel_stays_on_original_operation_after_active_dialogue_changes(shell):
    coordinator, conv, remote = shell
    remote.fail = True
    row = await coordinator.start(conv, "echo")
    coordinator.core.store.add_session(conv, "other", "/other")
    remote.fail = False
    result = await coordinator.refresh(OWNER, row["id"], cancel=True)
    assert remote.frames[-1].action == "cancel"
    assert remote.frames[-1].operation.hex == row["id"] and not remote.frames[-1].command
    assert result["dialogue"] == row["dialogue"]
