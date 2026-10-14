import pytest

from acp_gateway.channels.telegram_ui import Panels
from acp_gateway.core import GatewayError


@pytest.mark.parametrize(
    "command", ["/new", "/switch 1", "/agent home2/hermes", "cancel:job", "/mode set auto 1 2"]
)
def test_mutating_controls_are_bound_to_message_and_one_shot(command):
    panels = Panels()
    markup, tokens = panels.create(10, [[("Go", command)]], ("home/hermes", 1, 2))
    panels.bind(tokens, 100)
    data = markup.inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64 and "hermes" not in data
    for chat, message_id in [(20, 100), (10, 101)]:
        with pytest.raises(GatewayError):
            panels.take(data, chat, message_id, ("home/hermes", 1, 2))
    assert panels.take(data, 10, 100, ("home/hermes", 1, 2)) == command
    with pytest.raises(GatewayError):
        panels.take(data, 10, 100, ("home/hermes", 1, 2))


@pytest.mark.parametrize(
    "context", [("home2/hermes", 1, 2), ("home/hermes", 3, 2), ("home/hermes", 1, 4)]
)
def test_stale_mutating_controls_refuse_route_and_dialogue_changes(context):
    panels = Panels()
    markup, tokens = panels.create(10, [[("New", "/new")]], ("home/hermes", 1, 2))
    panels.bind(tokens, 100)
    with pytest.raises(GatewayError):
        panels.take(markup.inline_keyboard[0][0].callback_data, 10, 100, context)


def test_global_navigation_and_expiration_are_bounded(monkeypatch):
    panels = Panels()
    markup, tokens = panels.create(10, [[("Menu", "/menu")]], ("home/hermes", 1, 2))
    panels.bind(tokens, 100)
    data = markup.inline_keyboard[0][0].callback_data
    assert panels.take(data, 10, 100, ("home2/hermes", 5, 6)) == "/menu"
    monkeypatch.setattr("acp_gateway.channels.telegram_ui.time.monotonic", lambda: 1e20)
    with pytest.raises(GatewayError):
        panels.take(data, 10, 100, (None, 0, None))
    for _ in range(1030):
        panels.create(10, [[("Menu", "/menu")]], (None, 0, None))
    assert len(panels.actions) == 1024
    panels.clear()
    assert not panels.actions


@pytest.mark.parametrize("context", [("other", 1, 2), ("home/hermes", 1, 3)])
def test_execution_controls_are_scoped_to_route_and_selection(context):
    panels = Panels()
    markup, tokens = panels.create(10, [[("Auto", "/mode set auto 1 2")]], ("home/hermes", 1, 2))
    panels.bind(tokens, 100)
    with pytest.raises(GatewayError):
        panels.take(markup.inline_keyboard[0][0].callback_data, 10, 100, context)


def test_editor_revisions_and_cache_eviction_suppress_stale_results():
    from types import SimpleNamespace

    panels = Panels()
    message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=1)
    old = panels.begin_edit(message)
    current = panels.begin_edit(message)
    assert not panels.current_edit(old) and panels.current_edit(current)
    for i in range(2, 260):
        panels.begin_edit(SimpleNamespace(chat=message.chat, message_id=i))
    assert len(panels.editors) == 256
    assert not panels.current_edit(current)
    panels.clear()
    assert not panels.editors
