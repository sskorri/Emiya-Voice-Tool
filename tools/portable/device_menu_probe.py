"""Drive a scratch WebView through its own profile's DevTools port. Never send system input."""
import asyncio
import json
import uuid
from pathlib import Path

import aiohttp


async def check_menu(client, scratch: Path, first, second):
    nonce = uuid.uuid4().hex
    await client.run_javascript(f"document.documentElement.dataset.testMenu = '{nonce}'")
    port_file, = (scratch / "portable-data/temp").rglob("DevToolsActivePort")
    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{port}/json/list") as response:
            pages = await response.json()
        for page in pages:
            if page["type"] != "page" or not page["url"].startswith("http://127.0.0.1:"):
                continue
            async with http.ws_connect(page["webSocketDebuggerUrl"], timeout=5) as ws:
                async def command(method, **params):
                    request_id = 1  # one outstanding request per test connection
                    await ws.send_json({"id": request_id, "method": method, "params": params})
                    while True:
                        response = await asyncio.wait_for(ws.receive_json(), timeout=5)
                        if response.get("id") == request_id:
                            if "error" in response:
                                raise RuntimeError(response["error"])
                            return response["result"]

                async def js(expression):
                    data = await command("Runtime.evaluate", expression=expression, returnByValue=True)
                    if "exceptionDetails" in data:
                        raise RuntimeError(data["exceptionDetails"].get("text"))
                    return data["result"].get("value")

                if await js("document.documentElement.dataset.testMenu") != nonce:
                    continue
                widget = """document.querySelector('select[aria-label="Call output device"]')"""
                async def wait_for(expression):
                    for _ in range(60):
                        if await js(expression):
                            return
                        await asyncio.sleep(.05)
                    state = await js(f"""({{tag: document.activeElement.tagName, value: {widget}.value,
                        open: {widget}.matches(':open'), focus: document.hasFocus(), events: window.menuChecks}})""")
                    raise AssertionError((expression, state))

                async def key(name, code):
                    for phase in ("keyDown", "keyUp"):
                        printable = {"Space": " ", "Enter": "\r"}.get(name, "")
                        await command("Input.dispatchKeyEvent", type=phase, key=" " if name == "Space" else name,
                                      code=name, windowsVirtualKeyCode=code,
                                      text=printable if phase == "keyDown" else "")

                async def click(x, y):
                    await command("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                    for phase, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
                        await command("Input.dispatchMouseEvent", type=phase, x=x, y=y, button="left",
                                      buttons=buttons, clickCount=1)

                await wait_for(f"{widget}?.options.length === 3 && {widget}.offsetWidth")
                await js("""window.menuChecks = []; document.addEventListener('keydown', e => {
                    queueMicrotask(() => window.menuChecks.push([e.key, e.target.tagName, e.defaultPrevented]));
                }, true)""")
                rect = await js(f"{widget}.getBoundingClientRect().toJSON()")
                await click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                await wait_for(f"{widget}.matches(':open')")
                details = await js(f"""(() => {{
                    const el = {widget};
                    const picker = getComputedStyle(el, '::picker(select)');
                    return {{background: picker.backgroundColor, radius: parseFloat(picker.borderRadius),
                        appearance: picker.appearance, supported: CSS.supports('appearance','base-select'),
                        options: Array.from(el.options, o => ({{label: o.label, bounds: o.getBoundingClientRect().toJSON()}})),
                        viewport: [innerWidth, innerHeight]}};
                }})()""")
                assert details["supported"] and details["appearance"] == "base-select", details
                assert details["radius"] >= 6 and details["background"] != "rgba(0, 0, 0, 0)", details
                assert [o["label"] for o in details["options"][1:]] == [first.name, second.name], details
                width, height = details["viewport"]
                for option in details["options"]:
                    box = option["bounds"]
                    assert box["width"] > 0 and 0 <= box["left"] < box["right"] <= width, box
                    assert box["height"] > 0 and 0 <= box["top"] < box["bottom"] <= height, box
                (scratch / "menu-open").touch()
                for _ in range(100):
                    if (scratch / "menu-captured").exists():
                        break
                    await asyncio.sleep(.05)
                else:
                    raise TimeoutError("Waiting for PrintWindow")
                # Choose the first real endpoint using the actual mouse target, then change
                # it with arrow keys and Enter. Escape must retain that committed choice.
                box = details["options"][1]["bounds"]
                await click(box["x"] + 15, box["y"] + box["height"] / 2)
                await wait_for(f"!{widget}.matches(':open') && {widget}.value === {json.dumps(first.id)}")
                await command("Emulation.setFocusEmulationEnabled", enabled=True)
                await js(f"{widget}.focus()")
                await key("Space", 32)
                await wait_for(f"{widget}.matches(':open')")
                await key("ArrowDown", 40)
                await key("Enter", 13)
                await wait_for(f"!{widget}.matches(':open') && {widget}.value === {json.dumps(second.id)}")
                await click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                await wait_for(f"{widget}.matches(':open')")
                await key("ArrowUp", 38)
                await key("Escape", 27)
                await wait_for(f"!{widget}.matches(':open') && {widget}.value === {json.dumps(second.id)}")
                return details
        raise RuntimeError("Could not authenticate the private scratch WebView")
