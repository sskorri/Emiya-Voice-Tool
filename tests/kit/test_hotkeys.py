"""Global call hotkeys, using a simulated OS queue; no global registrations or injected keystrokes."""
import queue
import threading
from types import SimpleNamespace

import pytest

from ui.entry import Flags
from ui.kit.hotkeys import GlobalHotkeys, _hotkey


class FakeThread:
    def __init__(self, fail=()):
        self.fail = fail
        self.owner = threading.get_ident()
        self.keys = {}
        self.removed = []
        self.inbox = queue.Queue()
        self.waiting = threading.Event()

    def register(self, ident, combo):
        assert threading.get_ident() == self.owner
        if combo in self.fail:
            raise OSError("already registered by another application")
        self.keys[ident] = combo

    def unregister(self, ident):
        assert threading.get_ident() == self.owner
        self.removed.append(self.keys.pop(ident))

    def receive(self):
        self.waiting.set()
        return self.inbox.get(timeout=8)

    def quit(self):
        self.inbox.put(None)


class DeferredLoop:
    def __init__(self):
        self.messages = queue.Queue()

    def call_soon_threadsafe(self, callback):
        self.messages.put(callback)

    def pending(self):
        return self.messages.get(timeout=2)


def test_reconfigure_and_disable_invalidate_delayed_os_messages_and_callbacks():
    sources, actions, errors = [], [], []
    loop = DeferredLoop()

    def factory():
        sources.append(FakeThread())
        return sources[-1]

    keys = GlobalHotkeys(loop, actions.append, errors.append, driver_factory=factory)
    try:
        keys.set_bindings({})
        assert not sources
        keys.set_bindings({"F9": "call_send"})
        old = sources[0]
        assert old.waiting.wait(2) and old.owner != threading.get_ident()
        old.inbox.put(1)
        stale_send = loop.pending()
        keys.set_bindings({"F8": "call_stop"})
        assert old.removed == ["F9"] and not old.keys
        stale_send()
        sources[-1].inbox.put(1)
        loop.pending()()
        assert actions == ["call_stop"]
        sources[-1].inbox.put(1)
        stale_stop = loop.pending()
        keys.set_bindings({})
        keys.set_bindings({"F8": "call_stop"})
        stale_stop()
        assert actions == ["call_stop"]
        assert sources[1].removed == ["F8"]
        sources[-1].inbox.put(1)
        stale_shutdown = loop.pending()
    finally:
        keys.close()
    stale_shutdown()
    keys.set_bindings({"F9": "call_send"})
    assert len(sources) == 3 and actions == ["call_stop"] and all(not s.keys for s in sources)


def test_partial_os_registration_failure_keeps_other_key_reports_error_and_cleans_up():
    loop = DeferredLoop()
    errors, actions, sources = [], [], []

    def factory():
        sources.append(FakeThread(fail={"F9"}))
        return sources[-1]

    keys = GlobalHotkeys(loop, actions.append, errors.append, driver_factory=factory)
    try:
        keys.set_bindings({"F9": "call_send", "F8": "call_stop"})
        loop.pending()()
        assert "F9" in errors[-1] and "already registered" in errors[-1]
        source = sources[0]
        source.inbox.put(1)
        source.inbox.put(2)
        loop.pending()()
        assert actions == ["call_stop"]
        keys.set_bindings({})
        assert errors[-1] == "" and source.removed == ["F8"]
    finally:
        keys.close()


@pytest.mark.parametrize(("combo", "lookup", "result"), [
    ("Ctrl+Shift+F9", -1, (0x4006, 0x78)), ("Alt+Enter", -1, (0x4001, 13)),
    ("Ctrl+K", -1, (0x4002, ord("K"))), ("+", 0x1BB, (0x4004, 0xBB)),
    ("Ctrl++", 0x1BB, (0x4006, 0xBB)), ("Alt+Left", -1, (0x4001, 37)),
])
def test_native_key_translation_disables_repeat(combo, lookup, result):
    assert _hotkey(combo, lambda key: lookup) == result


@pytest.mark.parametrize("combo", ["Ctrl+F12", "Win+G", "AudioVolumeUp", "Ctrl+?", "Ctrl+"])
def test_unsupported_global_keys_are_reported(combo):
    with pytest.raises(ValueError):
        _hotkey(combo, lambda key: -1)


class Signal:
    def __init__(self):
        self.handlers = []

    def subscribe(self, cb):
        self.handlers.append(cb)

    def unsubscribe(self, cb):
        self.handlers.remove(cb)

    def emit(self, *args):
        for cb in self.handlers:
            cb(*args)


class FakePort:
    def __init__(self, loop, dispatch, error):
        self.dispatch, self.error = dispatch, error
        self.commands = {}
        self.closed = False

    def set_bindings(self, commands):
        self.commands = commands.copy()

    def close(self):
        self.closed = True
        self.commands.clear()


async def test_composition_reads_settings_and_suspends_global_bindings_during_capture():
    from ui.launch import _CallKeys

    handlers, say = [], []
    config = {"call_enabled": False, "keys": {"call_send": ("F9", ""), "call_stop": ("F8", "")}}
    settings = SimpleNamespace(changed=Signal(), get=lambda section,key: config[key])
    events = SimpleNamespace(changed=Signal(), say=lambda msg, bad: say.append((msg, bad)), notify=lambda: None)
    service = SimpleNamespace(hotkeys_suspended=False, send=lambda take: handlers.append(take),
                              stop=lambda: handlers.append("stop"))
    core = SimpleNamespace(call=service, events=events, settings=settings, session=SimpleNamespace(take_id=7))
    keys = _CallKeys(core, FakePort)
    keys.start()
    src = keys.source
    assert src.commands == {}
    config["call_enabled"] = True
    settings.changed.emit("core", "call_enabled")
    assert src.commands == {"F9": "call_send", "F8": "call_stop"}
    core.session.take_id = 8
    src.dispatch("call_send")
    src.dispatch("call_stop")
    service.hotkeys_suspended = True
    src.dispatch("call_send")  # even before the coalesced notification
    events.changed.emit()
    assert src.commands == {} and handlers == [8, "stop"]
    service.hotkeys_suspended = False
    events.changed.emit()
    src.error("F9 is in use")
    assert service.hotkey_error == "F9 is in use" and say == [("F9 is in use", True)]
    config["call_enabled"] = False
    settings.changed.emit("core", "call_enabled")
    src.dispatch("call_stop")
    keys.close()
    assert src.closed and handlers == [8, "stop"]
    assert not events.changed.handlers and not settings.changed.handlers


@pytest.mark.parametrize(("flags", "simulation", "uses_hotkeys"), [
    (Flags(), False, True), (Flags(), True, False), (Flags(fake="speak"), False, False),
    (Flags(browser=True), False, False), (Flags(smoke="quit"), False, False),
    (Flags(test_window=True), False, False),
])
def test_only_interactive_windows_app_installs_hotkey_lifecycle(monkeypatch, flags, simulation, uses_hotkeys):
    from ui import launch

    starts, stops = [], []
    keys = SimpleNamespace(start=object(), close=object())
    monkeypatch.setattr(launch, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launch, "_CallKeys", lambda *args: keys)
    monkeypatch.setattr(launch, "app", SimpleNamespace(on_startup=starts.append, on_shutdown=stops.append))
    monkeypatch.setattr(launch, "register_native", lambda *args: None)
    core = SimpleNamespace(start=None, stop=None, call=SimpleNamespace(shutdown=None), scheduler=None)
    launch._register_handlers(SimpleNamespace(flags=flags, simulation=simulation, replay=None, core=core))
    assert (keys.start in starts) is uses_hotkeys
    assert (keys.close in stops) is uses_hotkeys
