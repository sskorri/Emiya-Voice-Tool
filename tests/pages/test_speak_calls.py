"""Use the real page handlers with an in-memory audio endpoint. No windows or audio."""
import pytest

from tests.core.test_call import CABLE, Recorder
from tests.pages.test_pages_speak import model_calls, opened, settle, shell_of, until
from tests.pages.test_wiring import spy
from tests.pages.wiring.settings import CALL_WIRING as SETTINGS_WIRING
from tests.pages.wiring.speak import CALL_WIRING as SPEAK_WIRING
from ui import launch
from ui.contract.ports import AudioEndpoint


@pytest.fixture(autouse=True)
def audio_output(fake_env, monkeypatch):
    output = Recorder()
    monkeypatch.setattr(launch, "_make_audio_output", lambda flags, simulation: output)
    return output


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=row.marker)
    for row in (*SETTINGS_WIRING, *SPEAK_WIRING)
])
async def test_call_control_reaches_its_intent(user, core, fake, audio_output, monkeypatch, row):
    calls = []
    spy(monkeypatch, row.intent, calls)
    if row.route == "/settings":
        await opened(user, core, fake, row.route)
        user.find(marker="settings.tab.voice_chat").click()
        await until(lambda: bool(core.call.devices), "device list")
    else:
        core.settings.set("core", "call_device", {"id": CABLE.id, "name": CABLE.name})
        core.settings.set("core", "call_enabled", True)
        core.settings.set("core", "call_send", "switch")
        await core.call.refresh_devices()
        await opened(user, core, fake)
        if row.marker == "take.stop_sending":
            audio_output.block_first = True
            user.find(marker="take.send").click()
            await until(lambda: core.call.phase == "sending", "outgoing stream")
    calls.clear()
    found = user.find(marker=row.marker)
    if row.act == "trigger":
        found.trigger(*row.arg)
    else:
        found.click()
    await until(lambda: row.intent in calls, row.intent)


async def configure(user, core, fake):
    await opened(user, core, fake, "/settings")
    user.find(marker="settings.tab.voice_chat").click()
    settings = shell_of(user).ctx.behaviours.settings().vm
    await until(lambda: "Windows defaults use the cable" in settings.call_note, "endpoint enumeration and warning")
    user.find(marker="call.device").trigger("change", CABLE.id)
    user.find(marker="call.enable").click()
    await until(lambda: core.call.available, "choosing the call output")
    assert core.settings.get("core", "call_send") == "preview"


async def test_output_picker_preserves_endpoint_id_across_refresh_while_calls_are_off(user, core, fake, audio_output):
    other = AudioEndpoint("another-id", CABLE.name)
    audio_output.devices = (other, CABLE)
    await opened(user, core, fake, "/settings")
    user.find(marker="settings.tab.voice_chat").click()
    device = next(iter(user.find(marker="call.device").elements))
    assert device.tag == "select" and device.props["aria-label"] == "Call output device"

    def option_ids():
        return [option.props["value"] for option in device.default_slot.children]

    await until(lambda: option_ids() == ["", other.id, CABLE.id], "two endpoints with the same display name")
    user.find(marker="call.device").trigger("change", CABLE.id)
    assert device.props["value"] == CABLE.id
    audio_output.devices = (CABLE, other)
    user.find(marker="call.refresh").click()
    await until(lambda: option_ids() == ["", CABLE.id, other.id], "refreshed endpoint order")
    assert device.props["value"] == CABLE.id

    audio_output.devices = ()
    user.find(marker="call.refresh").click()
    await until(lambda: option_ids() == [""], "unplugged devices")
    assert not device.props.get("value")
    assert core.settings.get("core", "call_device") == {"id": CABLE.id, "name": CABLE.name}
    assert not core.call.available and not audio_output.plays


@pytest.mark.parametrize(("route", "prefix"), [("/", "speak.session"), ("/takes", "takes")])
async def test_master_switch_adds_and_removes_send_on_existing_rows(user, core, fake, route, prefix):
    await opened(user, core, fake, route)
    rows = {element.id for element in user.current_layout.descendants()
            if f"{prefix}.row.7" in element._markers}
    assert len(rows) == 1

    def send_markers():
        return {marker for element in user.current_layout.descendants() for marker in element._markers
                if marker.startswith(f"{prefix}.send.")}

    assert not send_markers()
    for enabled in (True, False, True):
        core.settings.set("core", "call_enabled", enabled)
        await settle(core, fake)
        assert send_markers() == ({f"{prefix}.send.{i}" for i in (4, 5, 6, 7)} if enabled else set())
        assert {element.id for element in user.current_layout.descendants()
                if f"{prefix}.row.7" in element._markers} == rows


async def test_send_a_saved_line_and_stop_from_takes_without_loading_a_model(user, core, fake, audio_output):
    await configure(user, core, fake)
    user.find(marker="nav.takes").click()
    await user.should_see(marker="page.takes")
    user.find(marker="takes.tab.all").click()
    await user.should_see(marker="takes.send.7")
    fake.reset_records()
    audio_output.block_first = True
    user.find(marker="takes.row.7").click()
    assert not audio_output.plays
    user.find(marker="takes.send.7").click()
    await until(lambda: core.call.phase == "sending", "start of sending")
    assert audio_output.plays == [(CABLE, core.takes.path_of(7), core.takes.path_of(7).read_bytes())]
    user.find(marker="takes.stop_sending").click()
    await until(lambda: core.call.phase == "idle", "stopped output")
    assert audio_output.stopped == [True] and not model_calls(fake)


async def test_type_generate_then_explicitly_send_and_mute_monitor(user, core, fake, audio_output):
    await configure(user, core, fake)
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    user.find(marker="speak.text").type("Ready to go.")
    user.find(marker="speak.generate").click()
    await until(lambda: core.takes.get(8) is not None, "a finished take")
    await settle(core, fake)
    assert core.takes.get(8).text.endswith("Ready to go.") and not audio_output.plays
    audio_output.block_first = True
    user.find(marker="take.hear").click()
    user.find(marker="take.send").click()
    await until(lambda: core.call.phase == "sending", "the selected WAV starts")
    playback = shell_of(user).ctx.behaviours.playback()
    assert playback.vm.target.muted and playback.vm.target.playing
    user.find(marker="take.hear").click()
    assert not playback.vm.target.muted
    user.find(marker="take.stop_sending").click()
    await until(lambda: core.call.phase == "idle", "the selected WAV stops")
    assert not playback.vm.playing and len(audio_output.plays) == 1


async def test_speak_switch_sends_only_first_take_and_saved_session_row_is_explicit(user, core, fake, audio_output):
    await configure(user, core, fake)
    user.find(marker="call.mode.switch").click()
    user.find(marker="nav.speak").click()
    await user.should_see(marker="speak.call_switch")
    user.find(marker="speak.call_switch").click()
    user.find(marker="speak.generate3").click()
    await until(lambda: core.takes.get(10) is not None, "three finished takes")
    await settle(core, fake)
    assert [p for _, p, _ in audio_output.plays] == [core.takes.path_of(8)]
    user.find(marker="speak.session.row.9").click()
    assert len(audio_output.plays) == 1
    user.find(marker="speak.session.send.9").click()
    await settle(core, fake)
    assert [p for _, p, _ in audio_output.plays] == [core.takes.path_of(8), core.takes.path_of(9)]


async def test_clear_requires_a_quiet_pause_between_presses(user, core, fake, clock):
    await opened(user, core, fake)
    original = core.session.text
    user.find(marker="speak.clear").click()
    for gap in (0.01, 0.49, 0.50):
        clock.advance(gap)
        user.find(marker="speak.clear").click()
        assert core.session.text == original
    clock.advance(0.51)
    user.find(marker="speak.clear").click()
    assert not core.session.text
