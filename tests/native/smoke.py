"""The native-window harness (BUILD_SPEC §8.8 N1-N12, plus the reliability bench): python tests\\native\\smoke.py
[--only=smoke,browser,captures,quit,minimize,routes,soak,busy] or pytest -m native.

Every launch is a test window (--smoke) on a unique private Win32 desktop that never receives input.
The harness and both app entry points verify the desktop before creating any app windows. The harness
  * pre-flights each launch (no free screen -> "skipped: no free screen") and stops every further window
    run as soon as one of our windows was ever the foreground (polled every 50 ms);
  * runs the FakeEngine only, on a scratch data root, with TEMP/TMP, NiceGUI's storage and the instance
    lock in the run's own folder (the app's install folder gets nothing: see `--app` below);
  * drives the app from inside only (smoke_plan.py; no desktop input), and minimizes and restores from
    outside with ShowWindow(SW_SHOWMINNOACTIVE), then posts SC_RESTORE to the owner thread;
  * kills only its own process tree (by PID and creation time), after 120 s (420 s for the soak).

`smoke.py --app -- <gui.py flags>` is the launcher the harness starts with pythonw: it runs gui.py with the
instance lock and NICEGUI_STORAGE_PATH pointed into the run folder, and makes pywebview's window process
start through this file too (as __mp_main__), so that process writes its stderr to the run folder instead
of creating logs\\ in the install folder. Results go to VOICETOOL_NATIVE_OUT (default %TEMP%\\voicetool-native):
results.json, results.txt, captures\\*.png with manifest.json, and runs\\<name>\\ per launch.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]
if sys.path and Path(sys.path[0] or ".").resolve() == HERE:   # run as a script: never import tests\native\x bare
    sys.path[0] = str(APP_ROOT)
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import json  # noqa: E402
import re  # noqa: E402
import runpy  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable, Iterable  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from dataclasses import asdict, dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

from tests.native import desktop as isolation  # noqa: E402

GUI = APP_ROOT / "gui.py"
LAUNCHER_FLAG = "--app"
OUT_ENV = "VOICETOOL_NATIVE_OUT"                # results.json, results.txt, runs\ (default %TEMP%\voicetool-native)
CAPTURES_ENV = "VOICETOOL_NATIVE_CAPTURES"      # the PNG captures and manifest.json (default <out>\captures)
SMOKE_DIR_ENV = "VOICETOOL_SMOKE_DIR"           # = smoke_plan.SMOKE_DIR_ENV (smoke_plan imports NiceGUI)
SMOKE_ARGS_ENV = "VOICETOOL_SMOKE_ARGS"
STORAGE_ENV = "VOICETOOL_HARNESS_STORAGE"       # the launcher's NICEGUI_STORAGE_PATH
LOCK_DIR_ENV = "VOICETOOL_HARNESS_LOCK_DIR"     # the launcher's folder for gui.lock / gui-dev.lock
WINDOW_LOG = "window.log"


# ---- the launcher (`smoke.py --app -- <flags>`, and pywebview's window process) -------------------------

def _patch_entry() -> None:
    """Point the instance lock and NiceGUI's storage into the run folder (both processes call this)."""
    from ui import entry

    storage, lock_dir = os.environ.get(STORAGE_ENV), os.environ.get(LOCK_DIR_ENV)
    if storage:
        original = entry.environment

        def environment(*args: Any, **kwargs: Any) -> dict[str, str]:
            values = original(*args, **kwargs)
            if "NICEGUI_STORAGE_PATH" in values:
                values["NICEGUI_STORAGE_PATH"] = storage
            return values

        entry.environment = environment
    if lock_dir:
        entry.instance_lock_path = lambda app_root, *, fake: Path(lock_dir) / ("gui-dev.lock" if fake else "gui.lock")


def _launch_server(flags: list[str]) -> None:
    """The server: gui.py as __main__, with the window process started through this file."""
    import multiprocessing.spawn as spawn

    folder = Path(os.environ[SMOKE_DIR_ENV]).parent
    log = open(folder / "server.log", "a", encoding="utf-8", buffering=1, errors="backslashreplace")  # noqa: SIM115
    sys.stdout = sys.__stdout__ = sys.stderr = sys.__stderr__ = log
    isolation.require_private(os.environ.get(isolation.DESKTOP_ENV))
    _patch_entry()
    original = spawn.get_preparation_data
    launcher = str(Path(__file__).resolve())

    def preparation(name: str) -> dict[str, Any]:
        data = original(name)
        data.pop("init_main_from_name", None)
        data["init_main_from_path"] = launcher
        return data

    spawn.get_preparation_data = preparation
    sys.argv = [str(GUI), *flags]
    runpy.run_path(str(GUI), run_name="__main__")


def _launch_window(namespace: dict[str, Any]) -> None:
    """pywebview's window process: stderr into the run folder, then gui.py as __mp_main__.

    gui.py's names (its _window_ready, which the parent pickled by reference) are copied into this module,
    the __mp_main__ that multiprocessing unpickles against.
    """
    import faulthandler

    folder = os.environ.get(SMOKE_DIR_ENV)
    if folder and (sys.stdout is None or sys.stderr is None or os.environ.get(STORAGE_ENV)):
        stream = open(Path(folder) / WINDOW_LOG, "a", encoding="utf-8", buffering=1,  # noqa: SIM115
                      errors="backslashreplace")
        sys.stdout = sys.__stdout__ = stream
        sys.stderr = sys.__stderr__ = stream
        faulthandler.enable(stream)
    isolation.require_private(os.environ.get(isolation.DESKTOP_ENV))
    _patch_entry()
    names = runpy.run_path(str(GUI), run_name="__mp_main__")
    namespace.update({key: value for key, value in names.items() if key not in namespace})


if __name__ == "__mp_main__":
    _launch_window(globals())


# ---- the harness ------------------------------------------------------------------------------------------

from tests.native import clock, winprobe  # noqa: E402
from tests.native.eventlog import PLAN_EVENTS, Tail, write_ack  # noqa: E402

TITLE = "Emiya Voice"
WINDOW_SIZE = (1280, 800)
LIMIT_S = 120.0
SOAK_LIMIT_S = 420.0
FOCUS_POLL_S = 0.05
TREE_POLL_S = 0.25
TCP_POLL_S = 1.0
STARTUP_S = 20.0                     # N10 looks for console windows during this long after launch
EXIT_FAST_S = 3.0                    # N8: exit within this after the Close intent
SECOND_LAUNCH_S = 1.0                # N9
IDLE_CPU_MAX = 14.7                  # N5: % of one core at the cap (§8.8)
REDUCE_CPU_MAX = 3.0
FPS_CAP = 60
ICON_MAX_DISTANCE = 1.0
DRAG_EXPECTED = ["div.vt-brand", "h1.vt-title", "header.vt-header"]
QUIET_SWITCHES = ("ElasticOverscroll", "--disable-background-networking", "--disable-component-update",
                  "--disable-domain-reliability", "--no-pings", "--no-proxy-server",
                  "--autoplay-policy=no-user-gesture-required")
EXIT_ALREADY_RUNNING = 5
EXIT_NO_FREE_SCREEN = 8
TEST_LINE = re.compile(r"voicetool\.testwindow (\{.*\})")
PYTHONW = APP_ROOT / "venv" / "Scripts" / "pythonw.exe"
INCIDENT_FILE = "focus_incident.json"


class NoFreeScreen(Exception):
    """skipped: no free screen (a skip, never a failure)."""


class FocusStop(Exception):
    """A window run earlier in this output folder took the foreground: every further window run stops."""


def out_root() -> Path:
    return Path(os.environ.get(OUT_ENV) or Path(tempfile.gettempdir()) / "voicetool-native")


@dataclass
class Spec:
    """One launch."""

    name: str
    scenario: str = "speak"
    plan: str = "quit"
    args: dict[str, Any] = field(default_factory=dict)
    fx_telemetry: bool = True
    browser: bool = False
    limit_s: float = LIMIT_S
    lock_dir: Path | None = None


@dataclass
class Check:
    id: str
    ok: bool | None                  # None: skipped
    numbers: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def result(self) -> str:
        return "skip" if self.ok is None else ("pass" if self.ok else "fail")


class Session:
    """The window runs written to one output folder: the stop rule, the cycle rate, captures and results."""

    def __init__(self, out: Path | None = None) -> None:
        self.out = Path(out or out_root())
        self.runs_dir = self.out / "runs"
        self.captures = Path(os.environ.get(CAPTURES_ENV) or self.out / "captures")
        for folder in (self.runs_dir, self.captures):
            folder.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.out / "lock"
        self.lock_dir.mkdir(exist_ok=True)
        self._cycle_hz: float | None = None
        self.runs: dict[str, dict[str, Any]] = {}
        self.checks: dict[str, Check] = {}

    # ---------- the stop rule ----------

    def incident_file(self) -> Path:
        return self.out / INCIDENT_FILE

    def stopped(self) -> str | None:
        path = self.incident_file()
        return path.read_text(encoding="utf-8") if path.exists() else None

    def record_incident(self, run: str, incidents: list[dict[str, Any]]) -> None:
        self.incident_file().write_text(json.dumps({"run": run, "incidents": incidents}, default=str),
                                        encoding="utf-8")

    def focus_incidents(self) -> int:
        return sum(len(run.get("focus", {}).get("incidents", [])) for run in self.runs.values())

    # ---------- measurement ----------

    @property
    def cycle_hz(self) -> float:
        if self._cycle_hz is None:
            self._cycle_hz = winprobe.cycle_counter_hz()
        return self._cycle_hz

    # ---------- runs ----------

    def launch(self, spec: Spec, handlers: dict[str, Callable[[Run, dict[str, Any]], dict[str, Any]]] | None = None,
               ) -> Run:
        """Pre-flight, launch, serve the plan's checkpoints until the app exits (or the limit), record."""
        stopped = self.stopped()
        if stopped:
            raise FocusStop(f"stopped: a window took the foreground earlier ({stopped})")
        run = Run(self, spec, handlers or {})
        try:
            blocker = winprobe.running_test_blocker()
            if blocker:
                run.record["skipped"] = f"deferred: {blocker}"
                raise NoFreeScreen(run.record["skipped"])
            screen = None if spec.browser else run.preflight()
            if not spec.browser and screen is None:
                run.record["skipped"] = "skipped: no free screen"
                raise NoFreeScreen(run.record["skipped"])
            run.record["screen"] = screen
            foreground = winprobe.foreground_window()
            run.record["foreground_at_launch"] = {"hwnd": foreground, "pid": winprobe.window_pid(foreground),
                                                  "class": winprobe.class_name(foreground)}
            desktop = isolation.TestDesktop()
            try:
                run.desktop = desktop
                run.record["desktop"] = {"test": desktop.name, "input_before": desktop.input}
                # A brand-new worker can attach before it owns windows or hooks. Destroy the worker
                # before closing its desktop. The caller stays on the user's desktop throughout.
                with ThreadPoolExecutor(max_workers=1) as worker:
                    worker.submit(run.execute).result()
            finally:
                try:
                    run.record["desktop"]["input_after"] = isolation.input_name()
                finally:
                    desktop.close()
        finally:
            self.runs[spec.name] = run.record
        if run.record.get("skipped"):
            raise NoFreeScreen(run.record["skipped"])
        if run.focus_incidents:
            self.record_incident(spec.name, run.focus_incidents)
        return run

    def add(self, check: Check) -> Check:
        self.checks[check.id] = check
        return check

    def manifest_entry(self, entry: dict[str, Any]) -> None:
        path = self.captures / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {"captures": []}
        manifest["captures"] = [e for e in manifest["captures"] if e.get("label") != entry["label"]] + [entry]
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    def write_results(self, extra: dict[str, Any] | None = None) -> Path:
        """Merge this session's runs and checks into results.json and rewrite results.txt."""
        path = self.out / "results.json"
        try:
            results = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results = {}
        results.setdefault("runs", {}).update(self.runs)
        results.setdefault("checks", {}).update({cid: asdict(c) | {"result": c.result}
                                                 for cid, c in self.checks.items()})
        results["focus_incidents"] = sum(len(r.get("focus", {}).get("incidents", []))
                                         for r in results["runs"].values())
        results["monitors"] = winprobe.monitors()
        results["cycle_hz"] = self._cycle_hz or results.get("cycle_hz")
        results["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if extra:
            results.update(extra)
        path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        (self.out / "results.txt").write_text(summary_text(results), encoding="utf-8")
        return path


def summary_text(results: dict[str, Any]) -> str:
    lines = [f"Native runs ({results.get('updated')}), focus incidents: {results.get('focus_incidents')}",
             f"monitors: {[(m['device'], m['rect'], m['hz'], 'primary' if m['primary'] else '') for m in results.get('monitors', [])]}",
             ""]
    for cid in sorted(results.get("checks", {}), key=_check_order):
        check = results["checks"][cid]
        lines.append(f"{cid:<28} {check['result'].upper():<5} {check['numbers']}")
    lines.append("")
    for name, run in results.get("runs", {}).items():
        lines.append(f"run {name}: exit={run.get('exit_code')} wall={run.get('wall_s')} s "
                     f"skipped={run.get('skipped') or '-'} killed={run.get('killed') or '-'} "
                     f"plan={run.get('plan_status')} focus={len(run.get('focus', {}).get('incidents', []))}")
    return "\n".join(lines) + "\n"


def _check_order(cid: str) -> tuple[int, str]:
    match = re.match(r"N(\d+)", cid)
    return (int(match.group(1)), cid) if match else (100, cid)


class Run:
    """One launch: its folder, process tree, focus watch, plan events and checkpoints, and its record."""

    def __init__(self, session: Session, spec: Spec,
                 handlers: dict[str, Callable[[Run, dict[str, Any]], dict[str, Any]]]) -> None:
        self.session = session
        self.spec = spec
        self.folder = (session.runs_dir / spec.name).resolve()
        if self.folder.parent != session.runs_dir.resolve():
            raise ValueError("a native run name must name one folder inside runs")
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.smoke_dir = self.folder / "smoke"
        self.temp = self.folder / "temp"
        self.data = self.folder / "data"
        for folder in (self.smoke_dir, self.temp):
            folder.mkdir(parents=True)
        self.handlers = {**DEFAULT_HANDLERS, **handlers}
        self.record: dict[str, Any] = {"spec": {k: str(v) if isinstance(v, Path) else v
                                                for k, v in asdict(spec).items()}}
        self.events: list[dict[str, Any]] = []
        self.proc: isolation.Child | None = None
        self.desktop: isolation.TestDesktop | None = None
        self.tree: winprobe.ProcessTree | None = None
        self.hwnd = 0
        self.t_launch = 0.0
        self.t_exit: float | None = None
        self.focus_incidents: list[dict[str, Any]] = []
        self.cycle_samples: list[tuple[float, int]] = []
        self.cpu_time_samples: list[tuple[float, float]] = []
        self.cpu_time_marks: dict[str, float] = {}
        self.cpu_process_marks: dict[str, dict[int, float]] = {}
        self.cpu_marks: dict[str, tuple[float, int]] = {}
        self.cpu: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.consoles: list[dict[str, Any]] = []
        self.webview_cmdlines: list[str] = []
        self.expected: dict[str, Any] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._focus_monitor: threading.Thread | None = None
        self._tail = Tail(self.smoke_dir / PLAN_EVENTS)
        self.killed: list[int] = []

    # ---------- launch ----------

    def preflight(self) -> dict[str, Any] | None:
        """The screen ui.entry's rule picks right now, or None ("skipped: no free screen")."""
        from ui import entry

        desktop = entry.probe_desktop()
        candidates = tuple(s for s in desktop.screens if (s.bounds.width, s.bounds.height) == (1920, 1080))
        choice = entry.choose_screen(entry.Desktop(candidates, desktop.foreground, desktop.notification_state),
                                     os.environ.get(entry.TEST_SCREEN_ENV))
        self.record["preflight"] = {"reason": choice.reason, "screens": [s.device for s in desktop.screens],
                                    "foreground": None if desktop.foreground is None else desktop.foreground.hwnd}
        if choice.screen is None:
            return None
        screen = choice.screen
        return {"device": screen.device, "primary": screen.primary, "work": [screen.work.left, screen.work.top,
                screen.work.right, screen.work.bottom], "dpi": screen.dpi}

    def command(self) -> list[str]:
        flags = [f"--fake={self.spec.scenario}", f"--data-root={self.data}", f"--smoke={self.spec.plan}"]
        if self.spec.fx_telemetry:
            flags.append("--fx-telemetry")
        if self.spec.browser:
            flags += ["--browser", "--no-show"]
        return [str(PYTHONW), str(Path(__file__).resolve()), LAUNCHER_FLAG, "--", *flags]

    def environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
        env.update(HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                   TEMP=str(self.temp), TMP=str(self.temp), VOICETOOL_TEST_WINDOW="1",
                   VOICETOOL_TEST_RETURN_HWND="0",
                   **{SMOKE_DIR_ENV: str(self.smoke_dir), SMOKE_ARGS_ENV: json.dumps(self.spec.args),
                      STORAGE_ENV: str(self.folder / "nicegui"),
                      LOCK_DIR_ENV: str(self.spec.lock_dir or self.session.lock_dir)})
        if self.record.get("screen"):
            env["VOICETOOL_TEST_SCREEN"] = self.record["screen"]["device"]
        return env

    def launch_process(self) -> tuple[isolation.Child, winprobe.ProcessTree]:
        """Start on the verified desktop and immediately take ownership of the process handle/tree."""
        assert self.desktop is not None
        proc = self.desktop.start(self.command(), APP_ROOT, self.environment())
        try:
            return proc, winprobe.ProcessTree(proc.pid)
        except BaseException:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
            finally:
                proc.close()
            raise

    def execute(self) -> None:
        assert self.desktop is not None
        blocker = winprobe.running_test_blocker()
        if blocker:
            self.record["skipped"] = f"deferred: {blocker}"
            return
        with self.desktop.attached():
            try:
                self.start()
                self.serve()
            finally:
                self.finish()

    def start(self) -> None:
        assert self.desktop is not None
        self.t_launch = clock.now()
        self.proc, self.tree = self.launch_process()
        self._monitor = threading.Thread(target=self._watch, name=f"watch {self.spec.name}", daemon=True)
        self._focus_monitor = threading.Thread(target=self._watch_focus, name="watch input desktop", daemon=True)
        self._focus_monitor.start()
        self._monitor.start()

    # ---------- the watch thread: focus every 50 ms, the tree every 250 ms, TCP every second ----------

    def _watch_focus(self) -> None:
        """This new thread remains on the harness process desktop and observes the user's foreground."""
        assert self.tree is not None and self.desktop is not None
        observer_desktop = isolation.current_name()
        self.record["desktop"]["foreground_observer"] = observer_desktop
        if observer_desktop != self.desktop.input:
            self.record["skipped"] = "deferred: foreground observer lost access to the input desktop"
            return
        ours_since: float | None = None
        while not self._stop.is_set():
            now = clock.now()
            with self._lock:
                pids = self.tree.pids()
            fg = winprobe.foreground_window()
            ours = bool(fg) and winprobe.window_pid(fg) in pids
            if ours and ours_since is None:
                ours_since = now
                self.focus_incidents.append({"t": now, "after_launch_s": round(now - self.t_launch, 3), "hwnd": fg,
                                             "class": winprobe.class_name(fg), "seen_by": "poll"})
                self.record["focus_stop"] = True
            elif not ours and ours_since is not None:
                self.focus_incidents[-1]["held_s"] = round(now - ours_since, 3)
                ours_since = None
            self._stop.wait(FOCUS_POLL_S)

    def _watch(self) -> None:
        assert self.desktop is not None and self.tree is not None
        with self.desktop.attached():
            self._watch_processes()

    def _watch_processes(self) -> None:
        assert self.tree is not None
        next_tree = next_tcp = 0.0
        while not self._stop.is_set():
            now = clock.now()
            if now >= next_tree:
                next_tree = now + TREE_POLL_S
                with self._lock:
                    for proc in self.tree.refresh():
                        if proc.cmdline:
                            self.webview_cmdlines.append(proc.cmdline)
                    self.cycle_samples.append((now, self.tree.cycles()))
                    self.cpu_time_samples.append((now, self.tree.cpu_seconds()))
                    pids = self.tree.pids()
                if not self.hwnd and not self.spec.browser:
                    found = winprobe.app_windows(TITLE, pids)
                    if found:
                        self.hwnd = found[0]
                        self.record["window_found_s"] = round(now - self.t_launch, 3)
                if now - self.t_launch < STARTUP_S or self.t_exit is None:
                    for console in winprobe.console_windows(pids):
                        console["after_launch_s"] = round(now - self.t_launch, 3)
                        if all(c["hwnd"] != console["hwnd"] for c in self.consoles):
                            self.consoles.append(console)
            if now >= next_tcp:
                next_tcp = now + TCP_POLL_S
                blocker = winprobe.running_test_blocker()
                if blocker:
                    self.record["skipped"] = f"deferred: {blocker}"
                    return
                for endpoint in winprobe.tcp_endpoints(pids):
                    key = (endpoint["local"], endpoint["lport"], endpoint["remote"], endpoint["rport"],
                           endpoint["state"])
                    self.endpoints.setdefault(key, endpoint)
            time.sleep(FOCUS_POLL_S)

    # ---------- serving the plan ----------

    def serve(self) -> None:
        """Answer checkpoints until the app exits; past the limit, kill our own tree."""
        if self.proc is None:
            return
        deadline = self.t_launch + self.spec.limit_s
        while True:
            if self.record.get("focus_stop") or self.record.get("skipped"):
                self.record["killed"] = self.record.get("skipped") or "focus incident: stopped the test window immediately"
                with self._lock:
                    assert self.tree is not None
                    self.killed = self.tree.kill_all()
                self.proc.wait(timeout=10)
                self.t_exit = clock.now()
                break
            for event in self._tail.read():
                self.events.append(event)
                if event.get("event") == "checkpoint":
                    self._answer(event)
            if self.proc.poll() is not None:
                self.t_exit = clock.now()
                for event in self._tail.read():
                    self.events.append(event)
                break
            if clock.now() > deadline:
                self.record["killed"] = f"limit {self.spec.limit_s} s"
                with self._lock:
                    assert self.tree is not None
                    self.killed = self.tree.kill_all()
                self.proc.wait(timeout=10)
                self.t_exit = clock.now()
                break
            time.sleep(0.02)

    def _answer(self, event: dict[str, Any]) -> None:
        name = event.get("name", "")
        handler = self.handlers.get(name)
        started = clock.now()
        try:
            payload = handler(self, event) if handler else {"error": f"no handler for {name!r}"}
        except Exception as error:   # the plan gets the error and decides; the harness never hangs
            payload = {"error": f"{type(error).__name__}: {error}"}
        payload["handled_ms"] = round((clock.now() - started) * 1000, 1)
        self.record.setdefault("checkpoints", []).append({"seq": event["seq"], "name": name, **payload})
        write_ack(self.smoke_dir, event["seq"], payload)

    # ---------- after the run ----------

    def finish(self) -> None:
        record = self.record
        if self.proc is None:
            return
        if self.proc.poll() is None:
            with self._lock:
                assert self.tree is not None
                self.killed = self.tree.kill_all()
            self.proc.wait(timeout=10)
            record["killed"] = record.get("killed") or "harness ended the run"
            self.t_exit = clock.now()
        assert self.tree is not None
        gone = self._wait_tree_gone(5.0)
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join()
        if self._focus_monitor is not None:
            self._focus_monitor.join(timeout=2)
        tree = self.tree
        record.update(
            exit_code=self.proc.returncode, wall_s=round((self.t_exit or clock.now()) - self.t_launch, 3),
            tree=tree.summary(), tree_gone_after_exit_s=gone, killed_pids=self.killed,
            focus={"incidents": self.focus_incidents, "guard": self.guard_events()},
            consoles=self.consoles, endpoints=list(self.endpoints.values()), webview_cmdlines=self.webview_cmdlines,
            cpu=self.cpu, temp_left=self.temp_left(), log_problems=self.log_problems(),
            plan_status=self.plan_status(), events=self.events, window_found_s=record.get("window_found_s"),
        )
        for hit in record["focus"]["guard"]:  # even a failed restoration means our form was activated
            if not any(abs(i["t"] - hit["t_wall"]) < 1.0 for i in self.focus_incidents):
                self.focus_incidents.append({"t": hit["t_wall"], "after_launch_s": round(hit["t_wall"] -
                                             self.t_launch, 3), "seen_by": "guard", "took_ms": hit.get("took_ms")})
        tree.close()
        self.proc.close()

    def _wait_tree_gone(self, seconds: float) -> float | None:
        assert self.tree is not None
        started = clock.now()
        while clock.now() - started < seconds:
            with self._lock:
                self.tree.refresh()
                if not self.tree.alive():
                    return round(clock.now() - started, 3)
            time.sleep(0.1)
        with self._lock:
            self.record["left_alive"] = [p.pid for p in self.tree.alive()]
            self.killed += self.tree.kill_all()
        return None

    def logs(self) -> dict[str, str]:
        out = {}
        for name in ("server.log", f"smoke/{WINDOW_LOG}"):
            path = self.folder / name
            out[name] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return out

    def test_events(self) -> list[dict[str, Any]]:
        found = []
        for text in self.logs().values():
            for match in TEST_LINE.finditer(text):
                try:
                    found.append(json.loads(match.group(1)))
                except ValueError:
                    continue
        return found

    def guard_events(self) -> list[dict[str, Any]]:
        return [e for e in self.test_events() if e.get("event") == "focus_returned"]

    def log_problems(self) -> dict[str, list[str]]:
        problems: dict[str, list[str]] = {}
        for name, text in self.logs().items():
            lines = [line for line in text.splitlines()
                     if "Traceback" in line or " ERROR " in line or "CRITICAL" in line]
            if lines:
                problems[name] = lines[:40]
        return problems

    def temp_left(self) -> list[str]:
        return sorted(str(p.relative_to(self.temp)) for p in self.temp.rglob("*") if p.is_dir())[:50]

    def plan_status(self) -> str:
        names = [e.get("event") for e in self.events]
        for status in ("plan_failed", "plan_done", "plan_cancelled", "plan_started"):
            if status in names:
                return status
        return "no plan events"

    def event(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("event") == name), None)

    def events_named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]

    def checkpoint(self, name: str) -> dict[str, Any] | None:
        return next((c for c in self.record.get("checkpoints", []) if c["name"] == name), None)

    def quit_to_exit_s(self) -> float | None:
        quit_event = self.events_named("quit_requested")
        if not quit_event or self.t_exit is None:
            return None
        return round(self.t_exit - quit_event[-1]["t"], 3)

    def cycles_at(self, t: float) -> int:
        """The tree's cycles now (exact read), recorded under `t`."""
        with self._lock:
            assert self.tree is not None
            self.tree.refresh()
            value = self.tree.cycles()
        self.cycle_samples.append((t, value))
        return value


_DROPPED_ENV = frozenset({"PYTEST_CURRENT_TEST", "NICEGUI_USER_SIMULATION", "NICEGUI_STORAGE_PATH", "VOICETOOL_FAKE",
                          "WEBVIEW2_USER_DATA_FOLDER",
                          "VOICETOOL_DATA_DIR", "VOICETOOL_ENGINE", "VOICETOOL_CLOCK", "PYTHONPATH"})


# ---- checkpoint handlers (run in the harness while the plan waits) ---------------------------------------

def _need_window(run: Run, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while not run.hwnd and time.monotonic() < deadline:
        time.sleep(0.05)
    if not run.hwnd:
        raise RuntimeError("our window was not found")
    return run.hwnd


def on_window(run: Run, _event: dict[str, Any]) -> dict[str, Any]:
    """N1 geometry and chrome, N2 icons (the 32 and 16 px frames of the theme's icon.ico)."""
    hwnd = _need_window(run)
    info = winprobe.window_info(hwnd)
    ico = APP_ROOT / "ui" / "themes" / "emiya" / "static" / "icon.ico"
    info["icon_big_distance"] = winprobe.icon_distance(hwnd, winprobe.ICON_BIG, ico, 32)
    info["icon_small_distance"] = winprobe.icon_distance(hwnd, winprobe.ICON_SMALL, ico, 16)
    with run._lock:  # noqa: SLF001
        assert run.tree is not None
        info["window_pid_in_tree"] = info["pid"] in run.tree.pids()
    run.record["window"] = info
    return {"ok": True}


def on_capture(run: Run, event: dict[str, Any]) -> dict[str, Any]:
    """A PrintWindow capture of our window (not the screen), saved under captures\\ with a manifest entry."""
    hwnd = _need_window(run)
    label = re.sub(r"[^a-z0-9_.-]+", "_", str(event.get("label", "capture")).lower())
    image = winprobe.capture_window(hwnd)
    stats = winprobe.image_stats(image)
    path = run.session.captures / f"{label}.png"
    image.save(path)
    entry = {"label": label, "file": str(path), "run": run.spec.name, "scenario": run.spec.scenario,
             "route": event.get("route"), "stats": stats, "blank": winprobe.is_blank(stats),
             "t": time.strftime("%H:%M:%S")}
    run.session.manifest_entry(entry)
    run.record.setdefault("captures", []).append(entry)
    return {"ok": True, "blank": entry["blank"], **stats}


def on_cpu_start(run: Run, event: dict[str, Any]) -> dict[str, Any]:
    now = clock.now()
    run.cpu_marks[event["label"]] = (now, run.cycles_at(now))
    with run._lock:  # noqa: SLF001
        assert run.tree is not None
        run.cpu_time_marks[event["label"]] = run.tree.cpu_seconds()
        run.cpu_process_marks[event["label"]] = {pid: p.cpu_seconds() for pid, p in run.tree.procs.items()}
    return {"ok": True}


def on_cpu_end(run: Run, event: dict[str, Any]) -> dict[str, Any]:
    now = clock.now()
    cycles = run.cycles_at(now)
    t0, c0 = run.cpu_marks[event["label"]]
    span = now - t0
    with run._lock:  # noqa: SLF001
        assert run.tree is not None
        cpu_seconds = run.tree.cpu_seconds() - run.cpu_time_marks[event["label"]]
        previous = run.cpu_process_marks[event["label"]]
        processes = [{"pid": pid, "exe": p.exe,
                      "cpu_pct_one_core": round((p.cpu_seconds() - previous.get(pid, 0)) / span * 100, 2)}
                     for pid, p in run.tree.procs.items()]
    cpu_percent = cpu_seconds / span * 100 if span > 0 else None
    ticks, frames, plan_span = event.get("ticks"), event.get("frames"), event.get("span_s") or span
    run.cpu[event["label"]] = {
        "cpu_pct_one_core": None if cpu_percent is None else round(cpu_percent, 2),
        "cpu_seconds": cpu_seconds, "cpu_method": "GetProcessTimes kernel+user",
        "cpu_cycles_delta": cycles - c0, "processes": processes, "span_s": round(span, 3),
        "frames": frames, "ticks": ticks,
        "fps": None if frames is None else round(frames / plan_span, 2),
        "tick_hz": None if ticks is None else round(ticks / plan_span, 2),
        "iconic": bool(run.hwnd) and winprobe.is_iconic(run.hwnd),
    }
    return {"ok": True, **run.cpu[event["label"]]}


def on_minimize(run: Run, event: dict[str, Any]) -> dict[str, Any]:
    hwnd = _need_window(run)
    before = winprobe.foreground_window()
    winprobe.show_minimized_no_activate(hwnd)
    iconic = _wait(lambda: winprobe.is_iconic(hwnd), 2.0)
    after = winprobe.foreground_window()
    result = {"ok": iconic, "iconic": iconic, "foreground_before": before, "foreground_after": after,
              "foreground_changed": before != after, "cycle": event.get("cycle")}
    run.record.setdefault("minimize", []).append(result)
    return result


def on_restore(run: Run, event: dict[str, Any]) -> dict[str, Any]:
    hwnd = _need_window(run)
    before = winprobe.foreground_window()
    winprobe.show_no_activate(hwnd)
    shown = _wait(lambda: not winprobe.is_iconic(hwnd), 2.0)
    after = winprobe.foreground_window()
    result = {"ok": shown, "restored": shown, "foreground_before": before, "foreground_after": after,
              "activated": after == hwnd, "cycle": event.get("cycle"), "window": winprobe.window_info(hwnd)}
    run.record.setdefault("restore", []).append(result)
    return result


def on_still_alive(run: Run, _event: dict[str, Any]) -> dict[str, Any]:
    alive = run.proc is not None and run.proc.poll() is None and bool(run.hwnd) and winprobe.is_window(run.hwnd)
    run.record["still_alive"] = alive
    return {"ok": True, "alive": alive}


def on_second_launch(run: Run, _event: dict[str, Any]) -> dict[str, Any]:
    """N9: the same launch again, while this one holds the instance lock: exit 5 in under a second, no window."""
    second = Spec(name=f"{run.spec.name}-second", scenario=run.spec.scenario, plan="idle",
                  lock_dir=run.spec.lock_dir or run.session.lock_dir, limit_s=15)
    probe = Run(run.session, second, {})
    blocker = winprobe.running_test_blocker()
    if blocker:
        run.record["skipped"] = f"deferred: {blocker}"
        return {"ok": False, "reason": run.record["skipped"]}
    assert run.desktop is not None
    probe.desktop = run.desktop
    probe.record["screen"] = run.record["screen"]
    started = clock.now()
    proc, tree = probe.launch_process()
    windows: list[int] = []
    focus = False
    try:
        while proc.poll() is None and clock.now() - started < 15:
            tree.refresh()
            windows += winprobe.app_windows(TITLE, tree.pids())
            fg = winprobe.foreground_window()
            focus = focus or (bool(fg) and winprobe.window_pid(fg) in tree.pids())
            time.sleep(0.02)
        elapsed = clock.now() - started
        if proc.poll() is None:
            tree.kill_all()
            proc.wait(timeout=10)
    finally:
        tree.close()
        proc.close()
    result = {"ok": True, "exit_code": proc.returncode, "elapsed_s": round(elapsed, 3), "windows": len(windows),
              "foreground_taken": focus, "log": (probe.folder / "server.log").read_text(errors="replace")[-600:]}
    run.record["second_launch"] = result
    if focus:
        run.focus_incidents.append({"t": clock.now(), "seen_by": "poll", "run": second.name})
    return result


DEFAULT_HANDLERS: dict[str, Callable[[Run, dict[str, Any]], dict[str, Any]]] = {
    "window": on_window, "capture": on_capture, "cpu_start": on_cpu_start, "cpu_end": on_cpu_end,
    "minimize": on_minimize, "restore": on_restore, "still_alive": on_still_alive,
    "second_launch": on_second_launch,
}


def _wait(check: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


# ---- the run sets and their checks ------------------------------------------------------------------------

def run_smoke(session: Session) -> Run:
    """The main N-run: --fake=speak --smoke=smoke --fx-telemetry."""
    run = session.launch(Spec(name="smoke", plan="smoke"))
    for check in smoke_checks(run, session):
        session.add(check)
    return run


def smoke_checks(run: Run, session: Session) -> list[Check]:
    rec = run.record
    checks: list[Check] = []
    window = rec.get("window") or {}
    screen = rec.get("screen") or {}
    rect, work = window.get("rect"), screen.get("work")
    if rect and work:
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        dx = rect[0] - (work[0] + (work[2] - work[0] - width) // 2)
        dy = rect[1] - (work[1] + (work[3] - work[1] - height) // 2)
    else:
        dx = dy = None
    n1 = {"title": window.get("title") == TITLE, "pid_in_tree": window.get("window_pid_in_tree"),
          "no_caption": window.get("has_caption") is False, "no_thickframe": window.get("has_thickframe") is False,
          "client_1280x800": window.get("client_logical") == [1280.0, 800.0],
          "centred_on_chosen_screen": dx is not None and abs(dx) <= 2 and abs(dy) <= 2,
          "on_chosen_screen": (window.get("monitor") or {}).get("device") == screen.get("device"),
          "topmost": window.get("topmost"), "noactivate": window.get("noactivate")}
    checks.append(Check("N1 window", all(n1[k] for k in n1 if k not in ("topmost", "noactivate")),
                        f"client {window.get('client_logical')} rect {rect} on {screen.get('device')} work {work} "
                        f"offset ({dx}, {dy}) px; topmost={window.get('topmost')} noactivate={window.get('noactivate')}",
                        n1))
    big, small = window.get("icon_big_distance"), window.get("icon_small_distance")
    checks.append(Check("N2 icons", big is not None and small is not None and big <= ICON_MAX_DISTANCE
                        and small <= ICON_MAX_DISTANCE, f"ICON_BIG vs 32 px {big}, ICON_SMALL vs 16 px {small}",
                        {"big": big, "small": small}))
    checks.append(route_check(run))
    sentinel = (run.event("sentinel") or {}).get("result") or {}
    checks.append(Check("N4 sentinel", bool(sentinel.get("present")) and sentinel.get("total") == 0,
                        f"present={sentinel.get('present')} counts={sentinel.get('counts')}", sentinel))
    checks.append(cpu_check(run))
    minimized = run.cpu.get("minimized") or {}
    mins = rec.get("minimize") or [{}]
    checks.append(Check("N6 minimized", minimized.get("frames") == 0 and bool(minimized.get("iconic"))
                        and mins[0].get("iconic") is True,
                        f"frames while minimized {minimized.get('frames')}, IsIconic {minimized.get('iconic')}, "
                        f"cpu {minimized.get('cpu_pct_one_core')} %", minimized))
    checks.append(offline_check(run))
    checks.append(quit_check("N8 quit", run))
    second = rec.get("second_launch") or {}
    checks.append(Check("N9 second launch", second.get("exit_code") == EXIT_ALREADY_RUNNING
                        and (second.get("elapsed_s") or 99) < SECOND_LAUNCH_S and second.get("windows") == 0,
                        f"exit {second.get('exit_code')} in {second.get('elapsed_s')} s, windows {second.get('windows')}",
                        {k: v for k, v in second.items() if k != "log"}))
    checks.append(Check("N10 no console", not rec.get("consoles"), f"console windows {len(rec.get('consoles') or [])}",
                        {"consoles": rec.get("consoles")}))
    tqdm = (run.event("tqdm") or {}).get("result") or {}
    locks = tqdm.get("locks") or {}
    checks.append(Check("N11 tqdm", tqdm.get("ok") == 3 and bool(locks) and all(locks.values()),
                        f"{tqdm.get('ok')}/3 ok, locks {locks}, stderr_is_none={tqdm.get('stderr_is_none')}", tqdm))
    drag = run.event("drag") or {}
    regions = sorted(drag.get("regions") or [])
    checks.append(Check("N12 drag regions", regions == DRAG_EXPECTED and drag.get("direct_only") is True,
                        f"{regions}, DRAG_REGION_DIRECT_TARGET_ONLY={drag.get('direct_only')}", drag))
    checks.append(focus_check(session))
    return checks


def route_check(run: Run) -> Check:
    """N3: every route under Follow Windows, then Reduce Motion "Always" and Effects off."""
    problems: list[str] = []
    rows = []
    for event in run.events_named("route"):
        probe, route, policy = event.get("probe") or {}, event.get("route"), event.get("policy")
        rows.append({"route": route, "policy": policy, **{k: probe.get(k) for k in
                     ("marker", "body_dark", "theme_class", "font", "anchors", "logos", "field", "reduce_class")}})
        expected_logo = {"system": "logo.gif", "always": "logo-still.png", "fx_off": "logo.gif"}[policy]
        field_state = probe.get("field")
        wrong = [k for k, ok in (
            ("marker", probe.get("marker")), ("body--dark", probe.get("body_dark")),
            ("theme-emiya", probe.get("theme_class")), ("anchors", probe.get("anchors") == 4),
            ("Segoe UI", str(probe.get("font", "")).strip("'\"").startswith("Segoe UI")),
            ("no failed page", not probe.get("error_text")),
            ("logos", bool(probe.get("logos")) and (probe.get("reduced_os") or
                                                     all(src == expected_logo for src in probe["logos"]))),
            ("field", (field_state is None) if policy == "fx_off" else
             bool(field_state and (field_state.get("still") if policy == "always" else field_state.get("running")))),
            ("reduce-motion class", probe.get("reduce_class") == (policy == "always")),
        ) if not ok]
        if wrong:
            problems.append(f"{route} ({policy}): {', '.join(wrong)}")
    captures = run.record.get("captures") or []
    blank = [c["label"] for c in captures if c.get("blank")]
    ok = bool(rows) and not problems and not blank and len(rows) >= 9
    return Check("N3 routes", ok, f"{len(rows)} route probes, {len(captures)} captures, problems {problems or 0}, "
                 f"blank {blank or 0}", {"rows": rows, "problems": problems})


def cpu_check(run: Run) -> Check:
    hz = (run.record.get("window") or {}).get("monitor", {}).get("hz")
    cpu = run.cpu
    idle, reduce = cpu.get("idle") or {}, cpu.get("reduce_motion") or {}
    fps_ok = all(abs(((cpu.get(k) or {}).get(metric) or 0) - FPS_CAP) <= 3
                 for k in ("idle", "working") for metric in ("tick_hz", "fps"))
    ok = (idle.get("cpu_pct_one_core") is not None and idle["cpu_pct_one_core"] <= IDLE_CPU_MAX
          and reduce.get("cpu_pct_one_core") is not None and reduce["cpu_pct_one_core"] <= REDUCE_CPU_MAX and fps_ok)
    numbers = "; ".join(f"{k} {v.get('cpu_pct_one_core')} % ({v.get('tick_hz')} ticks/s, {v.get('fps')} fps)"
                        for k, v in cpu.items())
    return Check("N5 cpu", ok, f"at {hz} Hz: {numbers}", {"hz": hz, **cpu})


def offline_check(run: Run) -> Check:
    """N7 (native half): every TCP endpoint of the tree is loopback; WebView2 got the quiet switches."""
    bad = [e for e in run.record.get("endpoints", []) if not _loopback_endpoint(e)]
    browser_cmd = next((c for c in run.webview_cmdlines if "--type=" not in c), "")
    missing = [s for s in QUIET_SWITCHES if s not in browser_cmd]
    ok = bool(run.record.get("endpoints")) and not bad and bool(browser_cmd) and not missing
    return Check("N7 offline (native)", ok, f"{len(run.record.get('endpoints', []))} endpoints, non-loopback {len(bad)}, "
                 f"switches missing {missing or 0}", {"bad": bad, "missing": missing,
                                                      "browser_cmdline": browser_cmd[:2000]})


def _loopback_endpoint(endpoint: dict[str, Any]) -> bool:
    local, remote = endpoint["local"], endpoint["remote"]
    if endpoint["state"] == "LISTEN":
        return winprobe.is_loopback(local)
    return (winprobe.is_loopback(local) or winprobe.is_unspecified(local)) and (
        winprobe.is_loopback(remote) or winprobe.is_unspecified(remote))


def quit_check(cid: str, run: Run) -> Check:
    rec = run.record
    took = run.quit_to_exit_s()
    left = rec.get("temp_left") or []
    profile = [p for p in left if "EBWebView" in p]
    problems = rec.get("log_problems") or {}
    traceback = {name: [line for line in lines if "Traceback" in line] for name, lines in problems.items()}
    traceback = {k: v for k, v in traceback.items() if v}
    detail = {"exit_code": rec.get("exit_code"), "quit_to_exit_s": took, "on_shutdown": bool(run.event("on_shutdown")),
              "tree_gone_after_exit_s": rec.get("tree_gone_after_exit_s"), "webview_profile_left": profile,
              "temp_left": left, "traceback": traceback, "killed": rec.get("killed")}
    ok = (rec.get("exit_code") == 0 and took is not None and took <= EXIT_FAST_S and detail["on_shutdown"]
          and rec.get("tree_gone_after_exit_s") is not None and not profile and not traceback and not rec.get("killed"))
    return Check(cid, ok, f"exit {rec.get('exit_code')} {took} s after Close, on_shutdown={detail['on_shutdown']}, "
                 f"tree gone {rec.get('tree_gone_after_exit_s')} s later, profile left {len(profile)}, "
                 f"tracebacks {sum(map(len, traceback.values()))}", detail)


def focus_check(session: Session) -> Check:
    incidents = session.focus_incidents()
    guard = sum(len(r.get("focus", {}).get("guard", [])) for r in session.runs.values())
    return Check("focus", incidents == 0, f"{incidents} incidents over {len(session.runs)} launches "
                 f"(guard activations {guard})", {"incidents": incidents})


def run_browser(session: Session) -> Check:
    """N7 (browser half): gui.py --browser --no-show: every listening socket of the tree is on 127.0.0.1."""

    run = session.launch(Spec(name="browser", plan="idle", browser=True, limit_s=12, fx_telemetry=False))
    listening = [e for e in run.record.get("endpoints", []) if e["state"] == "LISTEN"]
    bad = [e for e in listening if e["local"] != "127.0.0.1"]
    others = [e for e in run.record.get("endpoints", []) if not _loopback_endpoint(e)]
    check = Check("N7 offline (browser)", bool(listening) and not bad and not others,
                  f"listening {[(e['local'], e['lport']) for e in listening]}, non-loopback {len(bad) + len(others)}",
                  {"listening": listening, "bad": bad + others})
    return session.add(check)


CAPTURE_RUNS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("firstrun", "firstrun", {}),
    ("switch", "switch", {"settle": 2.0}),
    ("lowvram", "lowvram", {"settle": 2.5}),
    ("failure", "failure", {"wait": "idle", "settle": 2.0}),
    ("loading", "loading", {"wait": "busy", "settle": 2.0}),
    ("generating", "generating", {"wait": "busy", "settle": 2.0}),
    ("forge", "forge", {"settle": 2.0}),
    ("forged", "forged", {"wait": "forged", "settle": 2.0}),
    ("voices", "voices", {}),
    ("takes", "takes", {}),
    ("models", "models", {}),
)


def run_captures(session: Session, names: Iterable[str] | None = None) -> list[Check]:
    """One launch per board; busy boards also check that Close arms first and the second Close quits."""
    wanted = set(names) if names is not None else None
    checks = []
    for label, scenario, args in CAPTURE_RUNS:
        if wanted is not None and label not in wanted:
            continue
        run = session.launch(Spec(name=f"capture-{label}", scenario=scenario, plan="capture",
                                  args={"label": label, **args}))
        captures = run.record.get("captures") or []
        probe = (run.event("route") or {}).get("probe") or {}
        problems = []
        if not captures or captures[0].get("blank"):
            problems.append("no capture" if not captures else "blank capture")
        if run.plan_status() != "plan_done":
            problems.append(run.plan_status())
        if run.record.get("exit_code") != 0:
            problems.append(f"exit {run.record.get('exit_code')}")
        if label == "firstrun" and len(probe.get("logos") or []) < 2:
            problems.append(f"logos {probe.get('logos')}")
        checks.append(session.add(Check(f"capture {label}", not problems,
                                        f"{len(captures)} capture(s), logos {probe.get('logos')}, "
                                        f"exit {run.record.get('exit_code')} {run.quit_to_exit_s()} s after Close"
                                        + (f"; problems {problems}" if problems else ""),
                                        {"captures": captures, "probe": probe})))
        busy = run.event("close_while_busy")
        if busy is not None:
            took = run.quit_to_exit_s()
            ok = (busy.get("armed") is True and busy.get("alive_after_first") is True and
                  run.record.get("exit_code") == 0 and took is not None and took <= EXIT_FAST_S)
            checks.append(session.add(Check(f"close while busy ({label})", ok,
                                            f"first Close armed={busy.get('armed')}, alive 1 s later="
                                            f"{busy.get('alive_after_first')}, second Close: exit "
                                            f"{run.record.get('exit_code')} in {took} s", busy)))
    return checks


def run_quit_cycles(session: Session, count: int = 10) -> Check:
    """Launch and quit through the Close path `count` times."""
    rows = []
    for cycle in range(count):
        run = session.launch(Spec(name=f"cycle-{cycle:02d}", plan="quit"))
        check = quit_check(f"cycle {cycle}", run)
        rows.append({"cycle": cycle, "ok": check.ok, "numbers": check.numbers,
                     "window_found_s": run.record.get("window_found_s"), **check.detail})
    failed = [r["cycle"] for r in rows if not r["ok"]]
    took = [r["quit_to_exit_s"] for r in rows if r["quit_to_exit_s"] is not None]
    return session.add(Check("launch/quit x10", not failed and len(rows) == count,
                             f"{len(rows) - len(failed)}/{len(rows)} ok; Close to exit max "
                             f"{max(took) if took else None} s, mean {round(sum(took) / len(took), 3) if took else None} s"
                             + (f"; failed {failed}" if failed else ""), {"rows": rows}))


def run_minrestore(session: Session, cycles: int = 10) -> Check:
    run = session.launch(Spec(name="minrestore", plan="minrestore", args={"cycles": cycles}))
    frames = [e.get("frames") for e in run.events_named("minimized_frames")]
    running = [e.get("running_after") for e in run.events_named("minimized_frames")]
    mins, rests = run.record.get("minimize") or [], run.record.get("restore") or []
    activated = [r for r in rests if r.get("activated")] + [m for m in mins if m.get("foreground_changed")]
    ok = (len(frames) == cycles and all(f == 0 for f in frames) and all(running) and all(m.get("iconic") for m in mins)
          and all(r.get("restored") for r in rests) and not activated and not run.focus_incidents
          and run.record.get("exit_code") == 0)
    return session.add(Check("minimize/restore x10", ok,
                             f"{len(frames)} cycles, frames while minimized {frames}, field running after restore "
                             f"{sum(1 for r in running if r)}/{len(running)}, activations {len(activated)}",
                             {"frames": frames, "minimize": mins, "restore": rests}))


def run_routes(session: Session, count: int = 300, seed: int = 4) -> Check:
    run = session.launch(Spec(name="routes", plan="routes", args={"count": count, "seed": seed, "every": 50},
                              fx_telemetry=False))
    samples = run.events_named("memory")
    done = run.event("routes_done") or {}
    sentinel = done.get("sentinel") or {}
    nodes = [s.get("nodes") for s in samples]
    heap = [s.get("heap") for s in samples]
    ok = (bool(done) and sentinel.get("total") == 0 and len(samples) == count // 50 + 1
          and not steady_growth(nodes[1:]) and not steady_growth(heap[1:]))
    return session.add(Check("300 route changes", ok,
                             f"seed {seed}; JS errors {sentinel.get('total')}; DOM nodes {nodes}; JS heap MB "
                             f"{[round(h / 1e6, 2) if h else None for h in heap]}; route change avg "
                             f"{done.get('took_avg_s')} s max {done.get('took_max_s')} s; plan {run.plan_status()}",
                             {"samples": samples, "done": done}))


def steady_growth(values: list[Any], tolerance: float = 0.10) -> bool:
    """Every sample above the last and the end more than `tolerance` above the start: a leak, not noise."""
    clean = [v for v in values if isinstance(v, (int, float))]
    if len(clean) < 3:
        return False
    rising = all(b > a for a, b in zip(clean, clean[1:], strict=False))
    return rising and clean[-1] > clean[0] * (1 + tolerance)


def run_soak(session: Session, seconds: float = 300.0, sample_s: float = 30.0) -> Check:
    run = session.launch(Spec(name="soak", plan="soak", args={"seconds": seconds, "sample_s": sample_s},
                              limit_s=SOAK_LIMIT_S))
    samples = run.events_named("soak_sample")
    started = run.event("soak_started")
    cpu = soak_cpu(run, started["t"] if started else None, sample_s, seconds)
    heap = [s.get("heap") for s in samples]
    nodes = [s.get("nodes") for s in samples]
    sentinel = (run.event("sentinel") or {}).get("result") or {}
    jobs = {s.get("job") for s in samples}
    spread = (max(cpu) - min(cpu)) if cpu else None
    ok = (bool(samples) and jobs == {"generate"} and sentinel.get("total") == 0 and not steady_growth(heap)
          and not steady_growth(nodes) and cpu and spread is not None and spread <= max(5.0, 0.5 * max(cpu))
          and run.record.get("exit_code") == 0)
    return session.add(Check("5-minute soak", bool(ok),
                             f"{len(samples)} samples, job {sorted(j for j in jobs if j)}; CPU % per {sample_s:.0f} s "
                             f"{cpu}; JS heap MB {[round(h / 1e6, 2) if h else None for h in heap]}; DOM nodes {nodes}; "
                             f"JS errors {sentinel.get('total')}", {"samples": samples, "cpu": cpu}))


def soak_cpu(run: Run, start: float | None, step: float, seconds: float) -> list[float]:
    """The tree's CPU (% of one core) per `step` from the cycle samples of the watch thread."""
    if start is None:
        return []
    samples = sorted(run.cpu_time_samples)
    out = []
    t = start
    while t + step <= start + seconds + 0.5:
        window = [s for s in samples if t <= s[0] <= t + step]
        if len(window) >= 2:
            (t0, c0), (t1, c1) = window[0], window[-1]
            out.append(round((c1 - c0) / (t1 - t0) * 100, 2))
        t += step
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [LAUNCHER_FLAG]:
        _launch_server(args[2:] if args[1:2] == ["--"] else args[1:])
        return 0
    only = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("--only=")), None)
    session = Session()
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("smoke", lambda: run_smoke(session)), ("browser", lambda: run_browser(session)),
        ("captures", lambda: run_captures(session)), ("quit", lambda: run_quit_cycles(session)),
        ("minimize", lambda: run_minrestore(session)), ("routes", lambda: run_routes(session)),
        ("soak", lambda: run_soak(session)),
    ]
    for name, step in steps:
        if only and name not in only:
            continue
        try:
            step()
        except NoFreeScreen as skip:
            session.add(Check(name, None, str(skip)))
        except FocusStop as stop:
            session.add(Check(name, None, str(stop)))
            break
        finally:
            session.write_results()
    print((session.out / "results.txt").read_text(encoding="utf-8"))
    return 0 if all(c.ok is not False for c in session.checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
