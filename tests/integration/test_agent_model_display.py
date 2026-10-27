"""Panels name each agent's model from what that agent itself reported."""

from acp.schema import ConfigOptionUpdate, SessionConfigOptionSelect
from test_telegram import OWNER, feed
from test_telegram import telegram as telegram

from acp_gateway.storage import Conversation


def _select(option_id, category, value):
    return SessionConfigOptionSelect(
        id=option_id,
        name=option_id,
        type="select",
        category=category,
        current_value=value,
        options=[],
    )


async def test_goose_destination_shows_reported_model_and_follows_config_updates(telegram):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    active = core.active_session(conv)
    text = channel._destination_text(OWNER)
    assert "Goose · chatgpt_codex · gpt-5.6-sol · medium (последнее наблюдение)" in text
    assert "модель неизвестна" not in text

    client = core.agent("work")
    client._on_update(
        active.acp_session_id,
        ConfigOptionUpdate(
            session_update="config_option_update",
            config_options=[
                _select("provider", None, "anthropic"),
                _select("model", "model", "claude-sonnet-5-5"),
                _select("thinking_effort", "thought_level", "high"),
            ],
        ),
    )
    text = channel._destination_text(OWNER)
    assert "Goose · anthropic · claude-sonnet-5-5 · high" in text
    # The new value is persisted for panels rendered after a dispatcher restart.
    client._observations.clear()
    assert core.observed_model(core.active_session(conv))["observed_model"] == "claude-sonnet-5-5"


async def test_job_pins_goose_model_reported_before_the_prompt(telegram):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    job = await core.submit_selected(conv, "say pong")
    await core.wait(job.id, timeout=5)
    source = core.store.job_context(job.id)
    assert (source["provider"], source["model"], source["effort"]) == (
        "chatgpt_codex",
        "gpt-5.6-sol",
        "medium",
    )
    assert source["model_observation_source"] == "acp.config_options"
    assert "Goose · chatgpt_codex · gpt-5.6-sol · medium" in channel._result_footer(OWNER, job)


async def test_unknown_model_hints_are_specific_per_agent(telegram):
    core, channel, _, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "recorded-only", "/work")
    profile = core.agent("work").profile
    assert "Goose · модель неизвестна" in channel._destination_text(OWNER)
    for kind, hint in [
        ("hermes", "Hermes · модель ещё не запрашивалась · /model"),
        ("claude_code", "Claude Code · модель станет известна после первого ответа"),
    ]:
        object.__setattr__(profile, "kind", kind)
        try:
            assert hint in channel._destination_text(OWNER)
        finally:
            object.__setattr__(profile, "kind", "goose")
