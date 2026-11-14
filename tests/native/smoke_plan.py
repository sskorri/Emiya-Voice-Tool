"""The in-app half of the native runs: --smoke=PLAN drives the app from inside (BUILD_SPEC §8.8).

ui\\launch.py starts `run_plan(client, plan, launch)` once, as an app-level task, after the first page load.
A plan uses controller intents (the same calls the shell's buttons make) and run_javascript probes; it
never sends desktop input. It talks to the harness (smoke.py) through files in VOICETOOL_SMOKE_DIR:
plan.jsonl gets one JSON line per event, and a `checkpoint` event waits until the harness writes
acks\\<seq>.json (the harness measures or captures from outside meanwhile). VOICETOOL_SMOKE_ARGS holds the
plan's options as JSON. A plan that fails logs its trace and still quits the app, so a run never hangs.

Plans: smoke (N1-N12), capture, quit, minrestore, routes, soak, idle (do nothing; the harness ends the run).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nicegui import Client, app, run, ui

from tests.native import tqdm_probe
from tests.native.eventlog import PLAN_EVENTS, EventLog, ack_path
from ui.contract.routes import PAGE_ID, ROUTES

if TYPE_CHECKING:
    from ui.contract.theme import Behaviours
    from ui.launch import Launch

LOG = logging.getLogger("voicetool.smoke")

SMOKE_DIR_ENV = "VOICETOOL_SMOKE_DIR"
SMOKE_ARGS_ENV = "VOICETOOL_SMOKE_ARGS"
FIELD_UID = "forge-field"
POLL_S = 0.05
CHECKPOINT_TIMEOUT_S = 30.0

# ---- browser probes (run_javascript: a bare expression, or a body with `return`) ----------------------

FIELD_JS = ("(() => { const r = window.__emiyaForgeField; const i = r && r.instances['" + FIELD_UID + "'];"
            " return i ? {frames: i.totals.frames, ticks: i.totals.ticks, running: i.running,"
            " unmounted: i.unmountedAt !== null, mounts: r.mounts, unmounts: r.unmounts} : null; })()")
SENTINEL_JS = ("(() => { const s = window.__vtSentinel; return s ? {present: true, counts: s.counts,"
               " total: s.total(), items: s.items.slice(0, 20)} : {present: false}; })()")
MEMORY_JS = ("(() => { const m = performance.memory; return {nodes: document.getElementsByTagName('*').length,"
             " heap: m ? m.usedJSHeapSize : null, heap_total: m ? m.totalJSHeapSize : null}; })()")
DRAG_JS = ("[...document.querySelectorAll('.pywebview-drag-region')].map(e => e.tagName.toLowerCase() + '.' +"
           " [...e.classList].filter(c => c.startsWith('vt-')).join('.'))")
ROUTE_JS = """(() => {
  const body = document.body;
  const font = getComputedStyle(body).fontFamily;
  return {
    path: location.pathname,
    pages: document.querySelectorAll('.vt-page').length,
    body_dark: body.classList.contains('body--dark'),
    theme_class: body.classList.contains('theme-emiya'),
    reduce_class: body.classList.contains('reduce-motion'),
    font: font,
    anchors: document.querySelectorAll('.emiya-anchor').length,
    logos: [...document.querySelectorAll('.vt-logo-img')].map(i => i.currentSrc.split('/').pop().split('?')[0]),
    reduced_os: matchMedia('(prefers-reduced-motion: reduce)').matches,
    error_text: document.body.innerText.includes('This page failed to build.'),
  };
})()"""
PATH_JS = "location.pathname + '|' + document.querySelectorAll('.vt-page').length"
SCROLL_ABOUT_JS = ("(() => { const e = document.querySelector('.is-about'); if (e) e.scrollIntoView({block: 'center'});"
                   " return !!e; })()")


class PlanError(RuntimeError):
    pass


class Driver:
    """The plan's handle on the app: events, checkpoints, the current page's controllers, probes."""

    def __init__(self, client: Client, launch: Launch, folder: Path, args: dict[str, Any]) -> None:
        self.first_client = client
        self.launch = launch
        self.core = launch.core
        self.folder = folder
        self.args = args
        self.log = EventLog(folder / PLAN_EVENTS, "plan")

    # ---------- talking to the harness ----------

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log.emit(event, **fields)

    async def checkpoint(self, name: str, *, timeout: float = CHECKPOINT_TIMEOUT_S, **fields: Any) -> dict[str, Any]:
        """Emit a checkpoint and wait for the harness's answer."""
        record = self.emit("checkpoint", name=name, **fields)
        path = ack_path(self.folder, record["seq"])
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                raise PlanError(f"no answer to checkpoint {name!r} within {timeout} s")
            await asyncio.sleep(POLL_S)
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------- the page ----------

    def client(self) -> Client:
        """The newest connected client that holds a shell (a reload makes a new one)."""
        for client in sorted(Client.instances.values(), key=lambda c: c.created, reverse=True):
            if client.has_socket_connection and self._shell_of(client) is not None:
                return client
        raise PlanError("no connected page with a shell")

    @staticmethod
    def _shell_of(client: Client) -> Any:
        for element in list(client.elements.values()):
            shell = getattr(element, "shell", None)
            if shell is not None and hasattr(shell, "ctx"):
                return shell
        return None

    def shell(self) -> Any:
        return self._shell_of(self.client())

    def behaviours(self) -> Behaviours:
        return self.shell().ctx.behaviours

    def act(self, action: Callable[[Behaviours], Any]) -> Any:
        """Call one intent inside the page's context (toasts and navigation need it)."""
        client = self.client()
        with client:
            return action(self._shell_of(client).ctx.behaviours)

    async def act_async(self, action: Callable[[Behaviours], Awaitable[Any]]) -> Any:
        client = self.client()
        with client:
            return await action(self._shell_of(client).ctx.behaviours)

    async def js(self, code: str, timeout: float = 5.0) -> Any:
        return await self.client().run_javascript(code, timeout=timeout)

    async def until(self, what: str, check: Callable[[], Any], timeout: float = 15.0) -> Any:
        """Poll check() (sync or async) until it returns something truthy."""
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            if time.monotonic() > deadline:
                raise PlanError(f"timed out waiting for {what}")
            await asyncio.sleep(POLL_S)

    def page_marker_present(self, route: str) -> bool:
        marker = f"page.{PAGE_ID[route]}"
        client = self.client()
        return client.sub_pages_router.current_path == route and any(
            marker in element._markers for element in list(client.elements.values()))  # noqa: SLF001

    async def goto(self, route: str, *, timeout: float = 10.0) -> float:
        """Navigate like the rail does (ui.navigate.to), then wait for the page marker and the browser's DOM."""
        started = time.perf_counter()
        client = self.client()
        with client:
            ui.navigate.to(route)
        await self.until(f"page {route}", lambda: self.page_marker_present(route), timeout)

        async def shown() -> bool:
            return await self.js(PATH_JS) == f"{route}|1"
        await self.until(f"the browser showing {route}", shown, timeout)
        return time.perf_counter() - started

    async def ready(self) -> dict[str, Any]:
        """The shell is built, the core ready and the field mounted (when Background Effects is on)."""
        await self.launch.ready()
        await self.until("a page with a shell", lambda: self._has_shell(), 20)
        if self.shell().field is not None:
            await self.until("the forge field", self.field_counts, 20)
        sentinel = await self.js(SENTINEL_JS)
        return self.emit("ready", route=self.client().sub_pages_router.current_path, sentinel=sentinel,
                         field=await self.field_counts())

    def _has_shell(self) -> bool:
        try:
            self.client()
        except PlanError:
            return False
        return True

    async def field_counts(self) -> Any:
        return await self.js(FIELD_JS)

    async def field_snapshot(self) -> Any:
        field = self.shell().field
        return None if field is None else await field.snapshot()

    async def route_probe(self, route: str) -> dict[str, Any]:
        # The field's acknowledgement does not wait for <picture>/<img> currentSrc to update.
        # Probe the final browser state, including hidden About/empty-state logos, after the source swap.
        async def logos_settled() -> Any:
            result = await self.js(ROUTE_JS)
            still = self.behaviours().status().vm.logo_still or result.get("reduced_os")
            expected = "logo-still.png" if still else "logo.gif"
            return result if result.get("logos") and all(src == expected for src in result["logos"]) else None

        probe = await self.until("the logo sources to settle", logos_settled, 5)
        probe["field"] = await self.field_snapshot()
        probe["marker"] = self.page_marker_present(route)
        return probe

    async def capture(self, label: str, *, settle: float = 1.2, **fields: Any) -> dict[str, Any]:
        await asyncio.sleep(settle)
        return await self.checkpoint("capture", label=label,
                                     route=self.client().sub_pages_router.current_path, **fields)

    async def cpu_window(self, label: str, seconds: float = 5.0) -> dict[str, Any]:
        """A CPU window: the harness counts the tree's cycles between the two checkpoints, the plan the frames."""
        await self.checkpoint("cpu_start", label=label)
        before, t0 = await self.field_counts(), time.perf_counter()
        await asyncio.sleep(seconds)
        after, t1 = await self.field_counts(), time.perf_counter()
        frames = None if not before or not after else after["frames"] - before["frames"]
        ticks = None if not before or not after else after["ticks"] - before["ticks"]
        span = t1 - t0
        result = {"label": label, "span_s": round(span, 3), "frames": frames, "ticks": ticks,
                  "fps": None if frames is None else round(frames / span, 2)}
        await self.checkpoint("cpu_end", label=label, frames=frames, ticks=ticks, span_s=round(span, 3))
        self.emit("cpu_frames", **result)
        return result

    # ---------- app state ----------

    def job_kind(self) -> str:
        return self.core.state.job.kind

    async def hold_and_generate(self, timeout_s: float | None = None) -> None:
        """A held fake take: FakeEngine parks speak at its first step until released."""
        fake = self.launch.engine
        if timeout_s is not None:
            import ui.core.fake as fake_module

            fake_module.HOLD_TIMEOUT_S = timeout_s
        fake.hold("speak")
        await self.act_async(lambda b: b.speak().generate(1))
        await self.until("a running take", lambda: self.job_kind() == "generate", 15)

    async def release_and_wait_idle(self, timeout: float = 30.0) -> None:
        self.launch.engine.release_all()
        await self.until("an idle app", lambda: not self.job_kind(), timeout)

    def window_visible(self) -> bool:
        return bool(self.core.state.window.visible)

    def quit(self) -> None:
        """Close through the Close button's intent (status.request_quit)."""
        self.emit("quit_requested", job=self.job_kind())
        self.act(lambda b: b.status().request_quit())

    async def quit_busy(self) -> dict[str, Any]:
        """Close while busy: the first press only arms (the app stays), the second quits."""
        self.act(lambda b: b.status().request_quit())
        armed = bool(self.behaviours().status().vm.close.armed)
        self.emit("quit_pressed_once", job=self.job_kind(), armed=armed)
        await asyncio.sleep(1.0)
        answer = await self.checkpoint("still_alive", armed=armed, job=self.job_kind())
        self.quit()
        return {"armed": armed, "alive_after_first": answer.get("alive")}


# ---- plans ----------------------------------------------------------------------------------------------

async def plan_smoke(d: Driver) -> None:
    """N1-N12: the window, icons, routes, sentinel, CPU windows, minimize, tqdm, second launch, quit."""
    await d.ready()
    await d.checkpoint("window")                                         # N1, N2, N10 (from outside)
    d.emit("drag", regions=await d.js(DRAG_JS),
           direct_only=bool(app.native.settings.get("DRAG_REGION_DIRECT_TARGET_ONLY")))      # N12
    for route in ROUTES:                                                 # N3
        took = await d.goto(route)
        probe = await d.route_probe(route)
        d.emit("route", route=route, took_s=round(took, 3), probe=probe, policy="system")
        await d.capture(f"route_{PAGE_ID[route]}")
    await d.goto("/settings")
    for tab in ("effects", "keys", "general"):
        d.act(lambda b, tab=tab: b.settings().set_tab(tab))
        await d.capture(f"settings_{tab}")
    await d.js(SCROLL_ABOUT_JS)
    await d.capture("settings_about")
    await d.goto("/")
    d.act(lambda b: b.settings().set_reduce_motion("always"))           # Reduce Motion "Always"
    await d.until("the still field", _still(d), 10)
    for route in ("/", "/settings"):
        await d.goto(route)
        d.emit("route", route=route, probe=await d.route_probe(route), policy="always")
    await d.goto("/")
    await d.capture("reduce_motion_always")
    await d.cpu_window("reduce_motion")
    d.act(lambda b: b.settings().set_reduce_motion("system"))
    await d.until("the running field", _running(d), 10)
    d.act(lambda b: b.settings().set_pref("fx_on", False))               # Effects off
    await d.until("no field", lambda: d.shell().field is None, 10)
    d.emit("route", route="/", probe=await d.route_probe("/"), policy="fx_off")
    await d.cpu_window("fx_off")
    d.act(lambda b: b.settings().set_pref("fx_on", True))
    await d.until("the field back", lambda: d.shell().field is not None, 10)
    await d.until("the running field", _running(d), 10)
    await asyncio.sleep(2.0)
    await d.cpu_window("idle")                                           # N5
    await d.hold_and_generate()
    await asyncio.sleep(1.0)
    await d.cpu_window("working")
    await d.release_and_wait_idle()
    await asyncio.sleep(1.0)
    await d.checkpoint("minimize")                                       # N6 (ShowWindow from outside)
    await d.until("the minimized event", lambda: not d.window_visible(), 5)
    await asyncio.sleep(0.5)
    await d.cpu_window("minimized")
    await d.checkpoint("restore")
    await d.until("the restored event", d.window_visible, 5)
    await d.until("the running field", _running(d), 10)
    d.emit("tqdm", result=await run.io_bound(tqdm_probe.tqdm_job))       # N11
    d.emit("sentinel", result=await d.js(SENTINEL_JS))                   # N4
    await d.checkpoint("second_launch", timeout=40)                      # N9
    d.quit()                                                             # N8


def _still(d: Driver) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        snap = await d.field_snapshot()
        return bool(snap and snap.get("still") and not snap.get("running"))
    return check


def _running(d: Driver) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        snap = await d.field_snapshot()
        return bool(snap and snap.get("running"))
    return check


async def plan_capture(d: Driver) -> None:
    """One scenario's board: open its page, wait for its state, capture, then quit (arming first when busy)."""
    await d.ready()
    scenario = d.launch.engine.scenario
    route = d.args.get("route") or scenario.route
    if route != d.client().sub_pages_router.current_path:
        await d.until(f"the scenario's page {route}", lambda: d.page_marker_present(route), 10)
    wait = d.args.get("wait")
    if wait == "busy":
        await d.until("a busy app", lambda: bool(d.job_kind()), 15)
    elif wait == "idle":
        await d.until("an idle app", lambda: not d.job_kind(), 60)
    elif wait == "forged":
        await d.until("the forged result", lambda: not d.job_kind() and d.core.forge_draft.forged, 60)
    d.emit("route", route=route, probe=await d.route_probe(route), policy="system", scenario=scenario.name)
    await d.capture(d.args.get("label") or scenario.name, settle=float(d.args.get("settle", 1.5)))
    if d.job_kind():
        d.emit("close_while_busy", **await d.quit_busy())
    else:
        d.quit()


async def plan_quit(d: Driver) -> None:
    await d.ready()
    await asyncio.sleep(float(d.args.get("settle", 1.0)))
    d.quit()


async def plan_minrestore(d: Driver) -> None:
    """Minimize and restore from outside, `cycles` times: no activation, no field frames while minimized."""
    await d.ready()
    await asyncio.sleep(1.0)
    for cycle in range(int(d.args.get("cycles", 10))):
        await d.checkpoint("minimize", cycle=cycle)
        await d.until("the minimized event", lambda: not d.window_visible(), 5)
        await asyncio.sleep(0.3)
        before = await d.field_counts()
        await asyncio.sleep(float(d.args.get("hold_s", 1.5)))
        after = await d.field_counts()
        await d.checkpoint("restore", cycle=cycle)
        await d.until("the restored event", d.window_visible, 5)
        await asyncio.sleep(0.4)
        again = await d.field_counts()
        await asyncio.sleep(0.6)
        later = await d.field_counts()
        d.emit("minimized_frames", cycle=cycle, frames=after["frames"] - before["frames"],
               running_after=later["frames"] > again["frames"])
    d.emit("sentinel", result=await d.js(SENTINEL_JS))
    d.quit()


async def plan_routes(d: Driver) -> None:
    """`count` route changes from inside (seeded); DOM nodes and JS heap sampled every `every` changes."""
    await d.ready()
    count, every = int(d.args.get("count", 300)), int(d.args.get("every", 50))
    seed = int(d.args.get("seed", 4))
    rng = random.Random(seed)
    current = d.client().sub_pages_router.current_path
    d.emit("memory", change=0, **await d.js(MEMORY_JS))
    took: list[float] = []
    trace: list[str] = []
    for change in range(1, count + 1):
        route = rng.choice([r for r in ROUTES if r != current])
        trace.append(route)
        try:
            took.append(await d.goto(route))
        except PlanError:
            d.emit("route_failed", change=change, route=route, seed=seed, trace=trace[-20:])
            raise
        current = route
        if change % every == 0:
            d.emit("memory", change=change, took_avg_s=round(sum(took[-every:]) / every, 4),
                   took_max_s=round(max(took[-every:]), 4), **await d.js(MEMORY_JS))
    d.emit("routes_done", count=count, seed=seed, took_avg_s=round(sum(took) / len(took), 4),
           took_max_s=round(max(took), 4), sentinel=await d.js(SENTINEL_JS))
    d.quit()


async def plan_soak(d: Driver) -> None:
    """`seconds` with the FX field on and a held fake take; heap, DOM and frames every `sample_s`."""
    await d.ready()
    seconds, sample_s = float(d.args.get("seconds", 300)), float(d.args.get("sample_s", 30))
    await d.hold_and_generate(timeout_s=seconds + 120)
    started = time.monotonic()
    d.emit("soak_started", seconds=seconds)
    sample = 0
    while True:
        counts = await d.field_counts()
        d.emit("soak_sample", n=sample, elapsed_s=round(time.monotonic() - started, 2), job=d.job_kind(),
               field=counts, **await d.js(MEMORY_JS))
        sample += 1
        if time.monotonic() - started >= seconds:
            break
        await asyncio.sleep(max(0.0, min(sample_s, seconds - (time.monotonic() - started))))
    d.emit("sentinel", result=await d.js(SENTINEL_JS))
    await d.release_and_wait_idle()
    d.quit()


async def plan_idle(d: Driver) -> None:
    await d.ready()


async def plan_cpu_probe(d: Driver) -> None:
    """Compare native CPU time with the paused playhead pulse isolated; never sends desktop input."""
    await d.ready()
    await asyncio.sleep(2)
    await d.cpu_window("idle_baseline")
    wave = next(e for e in d.client().elements.values() if type(e).__name__ == "EmiyaTakeWave")
    found = await d.js(f"(() => {{ const e=getElement({wave.id}); window.__cpuPulse=e.pulse; return !!window.__cpuPulse; }})()")
    if not found:
        raise PlanError("the take waveform's pulse driver was not found")
    await d.js("window.__cpuResume=window.__cpuPulse.sync.bind(window.__cpuPulse);"
               "window.__cpuPulse.sync=()=>window.__cpuPulse.animation.pause(); window.__cpuPulse.sync()")
    await asyncio.sleep(1)
    await d.cpu_window("field_without_pulse")
    await d.js("document.querySelector('.emiya-take-wave').style.transform='translateZ(0)';"
               "window.__cpuPulse.sync=window.__cpuResume; window.__cpuPulse.sync()")
    await asyncio.sleep(1)
    await d.cpu_window("pulse_60_wave_layer")
    d.act(lambda b: b.settings().set_reduce_motion("always"))
    await d.until("still field", _still(d), 10)
    await asyncio.sleep(1)
    await d.cpu_window("reduced_motion")
    d.quit()


PLANS: dict[str, Callable[[Driver], Awaitable[None]]] = {
    "smoke": plan_smoke, "all": plan_smoke, "capture": plan_capture, "quit": plan_quit,
    "minrestore": plan_minrestore, "routes": plan_routes, "soak": plan_soak, "idle": plan_idle,
    "cpu-probe": plan_cpu_probe,
}


async def run_plan(client: Client, plan: str, launch: Launch) -> None:
    """ui\\launch.py's hook (an app-level background task, after the first page load)."""
    folder = Path(os.environ.get(SMOKE_DIR_ENV) or ".")
    try:
        args = json.loads(os.environ.get(SMOKE_ARGS_ENV) or "{}")
    except ValueError:
        args = {}
    driver = Driver(client, launch, folder, args)
    app.on_shutdown(lambda: driver.emit("on_shutdown"))
    body = PLANS.get(plan)
    driver.emit("plan_started", plan=plan, args=args, scenario=getattr(launch.engine, "scenario", None) and
                launch.engine.scenario.name)
    if body is None:
        driver.emit("plan_failed", error=f"unknown plan {plan!r}")
        return
    try:
        await body(driver)
        driver.emit("plan_done", plan=plan)
    except asyncio.CancelledError:
        driver.emit("plan_cancelled", plan=plan)
        raise
    except Exception as error:
        driver.emit("plan_failed", plan=plan, error=f"{type(error).__name__}: {error}",
                    trace=traceback.format_exc(limit=12))
        LOG.warning("--smoke=%s failed: %s", plan, error)
        if plan != "idle":
            await _quit_after_failure(driver)


async def _quit_after_failure(driver: Driver) -> None:
    """Let every held fake call go on, wait a little for the job to end, then close (twice when still busy)."""
    release = getattr(driver.launch.engine, "release_all", None)
    if release is not None:
        release()
    try:
        await driver.until("an idle app", lambda: not driver.job_kind(), 15)
    except PlanError:
        pass
    try:
        if driver.job_kind():
            driver.act(lambda b: b.status().request_quit())
        driver.quit()
    except Exception as error:
        driver.emit("quit_failed", error=f"{type(error).__name__}: {error}")
