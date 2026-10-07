"""GatewayCore against the recorded-traffic goose mock (P1.3 acceptance)."""

import asyncio

import pytest
from pydantic import SecretStr

from acp_gateway.agents import (
    AgentClient,
    AgentUnavailable,
    PermissionRequest,
    SessionBusy,
    reject_all,
)
from acp_gateway.agents.events import MessageChunk, ToolCallStarted, TurnFinished
from acp_gateway.channels import Channel
from acp_gateway.config import AgentProfile
from acp_gateway.core import (
    GatewayCore,
    JobFinished,
    JobNotFound,
    JobProgress,
    JobStarted,
    SessionCreated,
    UnknownAgent,
    UnknownSession,
    for_conversation,
)
from acp_gateway.storage import Conversation, JobStatus, Store
from fakes.fake_goose import FakeGooseServer, free_port

SECRET = "mock-goose-" + "secret-7781"
CLI = Conversation("cli", "default", "work")
TG = Conversation("telegram", "12345", "work")


@pytest.fixture
def server():
    with FakeGooseServer(SECRET) as srv:
        yield srv


async def allow(request: PermissionRequest) -> str | None:
    return request.option("allow_once").option_id


def make_core(url: str, tmp_path, *, handler=reject_all, **core_options) -> GatewayCore:
    profile = AgentProfile(
        alias="work",
        title="Work Goose",
        kind="goose",
        url=url,
        secret_env="AGENT_WORK_SECRET",
        default_cwd="/home/user/work",
    )
    client = AgentClient(
        profile,
        SecretStr(SECRET),
        pin_dir=tmp_path / "pins",
        permission_handler=handler,
        reconnect_delays=(0.05, 0.1),
        open_timeout=5,
    )
    return GatewayCore(Store.open(tmp_path / "gateway.db"), {"work": client}, **core_options)


async def wait_for_tool_call(core: GatewayCore, conversation: Conversation, job_id: str) -> None:
    """Wait until the job's command is running on the agent."""
    with core.bus.subscribe(for_conversation(conversation)) as events:
        async with asyncio.timeout(10):
            async for event in events:
                if (
                    isinstance(event, JobProgress)
                    and event.job_id == job_id
                    and isinstance(event.event, ToolCallStarted)
                ):
                    return


# ----------------------------------------------------------------- sessions


async def test_first_message_creates_a_session_and_answers(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    try:
        job = await core.ask(CLI, "Reply with exactly one word: pong")
        session = core.active_session(CLI)
    finally:
        await core.close()

    assert job.status is JobStatus.COMPLETED
    assert job.answer == "pong"
    assert job.stop_reason == "end_turn"
    assert job.usage["totalTokens"] == 25140
    assert session.acp_session_id == job.acp_session_id
    assert server.sessions[job.acp_session_id].mode == "smart_approve"
    assert session.title == "Pong"  # from goose's session_info_update


async def test_multi_turn_dialog_keeps_context(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    try:
        await core.ask(CLI, "Remember the code word lynx1234. Reply: OK")
        job = await core.ask(CLI, "What was the code word?")
    finally:
        await core.close()
    assert job.answer == "lynx1234"
    assert len(server.sessions) == 1


async def test_mapping_survives_gateway_restart(server, tmp_path):
    first = make_core(server.url("ws"), tmp_path)
    await first.start()
    try:
        before = await first.ask(TG, "Remember the code word fox42. Reply: OK")
    finally:
        await first.close()

    second = make_core(server.url("ws"), tmp_path)  # same database, new agent connection
    await second.start()
    try:
        after = await second.ask(TG, "What was the code word?")
        old_job = second.job(before.id)
    finally:
        await second.close()

    assert after.acp_session_id == before.acp_session_id
    assert after.answer == "fox42"
    assert "load_session" in server.log
    assert old_job.answer == "OK"  # job results are kept across restarts


async def test_several_sessions_per_conversation(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    created = []
    try:
        with core.bus.subscribe() as events:
            await core.ask(CLI, "Remember the code word alpha. Reply: OK")
            first = core.active_session(CLI)
            second = await core.new_session(CLI)
            assert core.active_session(CLI) == second
            assert (await core.ask(CLI, "What was the code word?")).answer == "unknown"

            core.switch_session(CLI, first.acp_session_id)
            assert (await core.ask(CLI, "What was the code word?")).answer == "alpha"
            listed = {s.acp_session_id for s in core.sessions(CLI)}
            events.close()
            created = [e.session async for e in events if isinstance(e, SessionCreated)]
    finally:
        await core.close()
    assert listed == {first.acp_session_id, second.acp_session_id}
    assert [s.acp_session_id for s in created] == [first.acp_session_id, second.acp_session_id]


async def test_conversations_do_not_share_sessions(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    try:
        cli_job = await core.ask(CLI, "say pong")
        tg_job = await core.ask(TG, "say pong")
        with pytest.raises(UnknownSession):
            core.switch_session(TG, cli_job.acp_session_id)
    finally:
        await core.close()
    assert cli_job.acp_session_id != tg_job.acp_session_id


async def test_unknown_agent(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    try:
        with pytest.raises(UnknownAgent):
            await core.ask(Conversation("cli", "default", "home"), "hi")
        with pytest.raises(UnknownAgent):
            core.sessions(Conversation("cli", "default", "home"))
    finally:
        await core.close()


async def test_agent_unavailable_when_creating_a_session(tmp_path):
    core = make_core(f"ws://127.0.0.1:{free_port()}/acp", tmp_path)
    try:
        with pytest.raises(AgentUnavailable, match="Work Goose"):
            await core.submit(CLI, "hi")
        assert core.active_session(CLI) is None
    finally:
        await core.close()


# --------------------------------------------------------------------- jobs


async def test_long_job_returns_a_job_id_and_finishes_later(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow)
    try:
        job = await core.ask(CLI, "run exactly: sleep 1 .", wait=0.2)
        assert job.status is JobStatus.RUNNING
        assert core.running_jobs(CLI) == [job]
        done = await core.wait(job.id, timeout=10)
        stored = core.store.job(job.id)
    finally:
        await core.close()
    assert done.status is JobStatus.COMPLETED
    assert done.answer == "finished"
    assert stored == done


async def test_busy_session_refuses_a_second_prompt(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow)
    try:
        job = await core.submit(CLI, "run exactly: sleep 30 .")
        with pytest.raises(SessionBusy, match="Work Goose is still working"):
            await core.submit(CLI, "say pong")
        # Another conversation is not blocked by it.
        assert (await core.ask(TG, "say pong", wait=10)).answer == "pong"
        (cancelled,) = await core.cancel(CLI)
        assert cancelled.id == job.id
        assert cancelled.status is JobStatus.CANCELLED
        assert (await core.ask(CLI, "say pong", wait=10)).answer == "pong"
    finally:
        await core.close()


async def test_new_session_while_busy_takes_new_messages(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow)
    try:
        busy = await core.submit(CLI, "run exactly: sleep 30 .")
        await core.new_session(CLI)
        assert (await core.ask(CLI, "say pong", wait=10)).answer == "pong"
        assert [j.id for j in core.running_jobs(CLI)] == [busy.id]
    finally:
        await core.close()


async def test_cancel_stops_the_running_command(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow)
    try:
        job = await core.submit(CLI, "run exactly: sleep 30 .")
        await wait_for_tool_call(core, CLI, job.id)
        (cancelled,) = await core.cancel(CLI)
    finally:
        await core.close()
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.stop_reason == "cancelled"
    assert "cancel" in server.log


async def test_cancel_when_the_agent_does_not_confirm(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow, cancel_grace=0.3)
    client = core.agent("work")
    real_cancel = client.cancel
    calls = []

    async def first_cancel_lost(session_id):
        calls.append(session_id)
        if len(calls) > 1:  # the stream close that follows still cancels on the agent
            await real_cancel(session_id)

    client.cancel = first_cancel_lost
    try:
        job = await core.submit(CLI, "run exactly: sleep 30 .")
        await wait_for_tool_call(core, CLI, job.id)
        cancelled = await core.cancel_job(job.id)
        assert (await core.ask(CLI, "say pong", wait=10)).answer == "pong"
    finally:
        await core.close()
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.error == "the agent did not confirm the cancellation"


async def test_cancel_of_a_finished_job_returns_it(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    try:
        job = await core.ask(CLI, "say pong")
        assert (await core.cancel_job(job.id)) == job
        assert await core.cancel(CLI) == []
        with pytest.raises(JobNotFound):
            core.job("nope")
    finally:
        await core.close()


async def test_rejected_action_completes_with_the_agent_answer(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)  # reject_all
    try:
        job = await core.ask(CLI, "run exactly: echo hi .")
    finally:
        await core.close()
    assert job.status is JobStatus.COMPLETED
    assert job.answer == "DENIED"


async def test_agent_drop_fails_the_job_and_frees_the_session(tmp_path):
    srv = FakeGooseServer(SECRET).start()
    core = make_core(srv.url("ws"), tmp_path, handler=allow)
    try:
        job = await core.submit(CLI, "run exactly: sleep 30 .")
        await wait_for_tool_call(core, CLI, job.id)
        await asyncio.to_thread(srv.stop)
        failed = await core.wait(job.id, timeout=10)
        assert failed.status is JobStatus.FAILED
        assert "lost connection to Work Goose" in failed.error

        restarted = FakeGooseServer(SECRET, port=srv.port, sessions=srv.sessions).start()
        try:
            again = await core.ask(CLI, "say pong", wait=10)
        finally:
            restarted.stop()
    finally:
        await core.close()
        srv.stop()
    assert again.acp_session_id == job.acp_session_id
    assert again.answer == "pong"


async def test_shutdown_interrupts_running_jobs(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path, handler=allow)
    job = await core.submit(CLI, "run exactly: sleep 30 .")
    await wait_for_tool_call(core, CLI, job.id)
    await core.close()
    assert "cancel" in server.log  # the turn is cancelled on the agent, not left running

    store = Store.open(tmp_path / "gateway.db")
    try:
        assert store.job(job.id).status is JobStatus.INTERRUPTED
    finally:
        store.close()


async def test_start_settles_jobs_of_a_crashed_process(server, tmp_path):
    store = Store.open(tmp_path / "gateway.db")
    session = store.add_session(CLI, "20261006_99", "/w")
    store.add_job("crashed", session.id)
    store.close()

    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    try:
        job = core.job("crashed")
    finally:
        await core.close()
    assert job.status is JobStatus.INTERRUPTED


# ------------------------------------------------------------------- events


async def test_events_of_a_job(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    try:
        with core.bus.subscribe(for_conversation(CLI)) as events:
            other = core.bus.subscribe(for_conversation(TG))
            job = await core.ask(CLI, "say pong")
            events.close()
            received = [e async for e in events]
            other.close()
            assert [e async for e in other] == []
    finally:
        await core.close()

    assert isinstance(received[0], SessionCreated)
    assert isinstance(received[1], JobStarted)
    assert received[1].job.id == job.id
    assert isinstance(received[-1], JobFinished)
    assert received[-1].job == job
    progress = [e.event for e in received if isinstance(e, JobProgress)]
    assert {e.session_id for e in progress} == {job.acp_session_id}
    assert [e.text for e in progress if isinstance(e, MessageChunk)] == ["pong"]
    assert isinstance(progress[-1], TurnFinished)


# ----------------------------------------------------------------- channels


class RecordingChannel(Channel):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.core = None

    async def start(self, core: GatewayCore) -> None:
        self.core = core
        self.log.append(f"start {self.name}")

    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")
        if self.name == "broken":
            raise RuntimeError("stop failed")


async def test_channels_are_started_and_stopped_by_the_core(server, tmp_path):
    log: list[str] = []
    core = make_core(server.url("ws"), tmp_path)
    first, broken = RecordingChannel("cli", log), RecordingChannel("broken", log)
    core.add_channel(first)
    core.add_channel(broken)
    with pytest.raises(ValueError, match="already registered"):
        core.add_channel(RecordingChannel("cli", log))
    await core.start()
    assert first.core is core
    assert first.connected
    await core.close()  # a failing channel does not stop the shutdown
    assert log == ["start cli", "start broken", "stop broken", "stop cli"]


async def test_shutdown_right_after_submit_settles_the_job(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    job = await core.submit(CLI, "say pong")  # its task has not run a single step yet
    waiter = asyncio.create_task(core.wait(job.id))
    await core.close()
    finished = await asyncio.wait_for(waiter, timeout=5)
    assert finished.status is JobStatus.INTERRUPTED
