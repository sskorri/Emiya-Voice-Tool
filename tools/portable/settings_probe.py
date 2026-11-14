"""In-app smoke plan installed temporarily by verify_portable_window.py."""
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback

from nicegui import Client
from ui.contract.ports import AudioEndpoint
from tests.native.device_menu_probe import check_menu


class TestOutput:
    first = AudioEndpoint("test-headphones", "Headphones (test)")
    device = AudioEndpoint("test-cable", "CABLE Input (VB-Audio Virtual Cable)")

    def list_devices(self):
        return (self.first, self.device)

    def default_roles(self):
        return ()

    def play(self, *args):
        raise AssertionError("The device-selection check attempted playback")


async def run_plan(client, plan, launch):
    report = {}
    output = TestOutput()
    async def element_id(marker):
        for _ in range(100):
            for connected in list(Client.instances.values()):
                if not connected.has_socket_connection:
                    continue
                for element in list(connected.elements.values()):
                    if marker in element._markers:
                        return connected, json.dumps(element.html_id)
            await asyncio.sleep(.05)
        raise TimeoutError(marker)

    async def click(marker):
        connected, element = await element_id(marker)
        await connected.run_javascript(f"document.getElementById({element}).click()")

    try:
        assert launch.flags.fake is None and launch.flags.test_window
        assert launch.core.call._output is None and "ui.kit.hotkeys" not in sys.modules
        assert not launch.engine.residency().resident
        launch.core.call._output = output
        await click("nav.settings")
        await click("settings.tab.effects")
        connected, effects = await element_id("settings.fx_on")
        for _ in range(100):
            if await connected.run_javascript(f"document.getElementById({effects})?.offsetWidth > 0"):
                break
            await asyncio.sleep(.05)
        else:
            raise AssertionError("The Background Effects control is hidden")
        await click("settings.tab.voice_chat")
        connected, _ = await element_id("call.device")
        details = await check_menu(connected, Path(os.environ["VOICETOOL_PORTABLE_PROBE"]).parent,
                                   output.first, output.device)
        for _ in range(100):
            if launch.core.settings.get("core", "call_device")["id"] == output.device.id:
                break
            await asyncio.sleep(.05)
        assert launch.core.settings.get("core", "call_device") == {"id": output.device.id, "name": output.device.name}
        await click("call.refresh")
        connected, element = await element_id("call.device")
        assert await connected.run_javascript(f"""(() => {{
            const select = document.getElementById({element});
            return select.value === {json.dumps(output.device.id)}
                && select.selectedOptions[0].label === {json.dumps(output.device.name)};
        }})()""")
        assert launch.core.settings.get("core", "call_enabled") is False
        assert not launch.engine.residency().resident
        report = {"effects_visible": True, "device_dropdown": True, "synthetic_device_saved": True,
                  "native_mouse_selection": True, "native_keyboard_selection_and_escape": True,
                  "selection_persisted_on_refresh": True,
                  "expanded_menu": details,
                  "loaded_models": 0, "system_hotkeys": False, "audio_played": False}
    except Exception:
        report = {"device_dropdown": False, "error": traceback.format_exc(limit=6)}
    Path(os.environ["VOICETOOL_PORTABLE_PROBE"]).write_text(json.dumps(report), encoding="utf-8")
