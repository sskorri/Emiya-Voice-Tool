"""I2: the native smoke run N1-N12 (BUILD_SPEC §8.8), adapted to the user's test-window rules.

    pytest -m native tests\\native\\test_native_smoke.py      (opens test windows: only with the user's go-ahead)

One launch (`--fake=speak --smoke=smoke --fx-telemetry`) answers N1-N6 and N8-N12; one `--browser --no-show`
launch answers N7's second half. A run with no free screen is a skip ("skipped: no free screen"). If any
of our windows was ever the foreground, the focus check fails and every later window run in the same
output folder is skipped ("stopped: ...") until the incident file there is removed.

The tests without the native mark check the harness itself (no window, no process): the stop rule, the
pre-flight skip, the launcher's redirections and the check arithmetic.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.native import eventlog, smoke
from ui import entry

SMOKE_CHECKS = ("N1 window", "N2 icons", "N3 routes", "N4 sentinel", "N5 cpu", "N6 minimized", "N7 offline (native)",
                "N8 quit", "N9 second launch", "N10 no console", "N11 tqdm", "N12 drag regions",
                "focus")


def _attempt(session: smoke.Session, step: Any) -> dict[str, Any]:
    """Run one harness step; a skip or a stop becomes the outcome's reason instead of an error."""
    try:
        step()
        if session.focus_incidents():
            return {"fail": "a test window was activated; further window runs are stopped"}
    except smoke.NoFreeScreen as skip:
        return {"skip": str(skip)}
    except smoke.FocusStop as stop:
        if session.focus_incidents():      # this session's own run took the focus: that is a failure
            return {"fail": str(stop)}
        return {"skip": str(stop)}
    finally:
        session.write_results()
    return {}


@pytest.fixture(scope="module")
def smoke_run() -> tuple[smoke.Session, dict[str, Any]]:
    session = smoke.Session()
    return session, _attempt(session, lambda: smoke.run_smoke(session))


@pytest.fixture(scope="module")
def browser_run() -> tuple[smoke.Session, dict[str, Any]]:
    session = smoke.Session()
    return session, _attempt(session, lambda: smoke.run_browser(session))


def assert_check(session: smoke.Session, outcome: dict[str, Any], cid: str) -> None:
    if "skip" in outcome:
        pytest.skip(outcome["skip"])
    assert "fail" not in outcome, outcome.get("fail")
    check = session.checks.get(cid)
    assert check is not None, f"{cid} was not measured; runs: {list(session.runs)}"
    assert check.ok, f"{cid}: {check.numbers}\n{json.dumps(check.detail, default=str)[:4000]}"


@pytest.mark.native
@pytest.mark.parametrize("cid", SMOKE_CHECKS)
def test_native_smoke(smoke_run: tuple[smoke.Session, dict[str, Any]], cid: str) -> None:
    assert_check(*smoke_run, cid)


@pytest.mark.native
def test_browser_mode_binds_loopback_only(browser_run: tuple[smoke.Session, dict[str, Any]]) -> None:
    assert_check(*browser_run, "N7 offline (browser)")


# ---- the harness itself (no window, no process) ----------------------------------------------------------

def _desktop(covered: bool) -> entry.Desktop:
    bounds = entry.Rect(0, 0, 1920, 1080)
    screen = entry.Screen("\\\\.\\DISPLAY1", True, bounds, bounds, 96, bounds)
    window = entry.Foreground(42, True, False, bounds if covered else entry.Rect(100, 100, 900, 700),
                              "\\\\.\\DISPLAY1")
    return entry.Desktop((screen,), window, 2)


@pytest.fixture
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Popen records instead of starting anything: these tests must never start the app."""
    started: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        started.append(args)
        raise AssertionError("the harness tried to launch the app")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(smoke.isolation, "TestDesktop", refuse)
    return started


def test_a_focus_incident_stops_every_later_window_run(tmp_path: Path, no_launch: list[Any]) -> None:
    session = smoke.Session(tmp_path)
    session.record_incident("smoke", [{"t": 1.0, "seen_by": "poll"}])
    with pytest.raises(smoke.FocusStop, match="stopped"):
        session.launch(smoke.Spec(name="cycle-00"))
    assert not no_launch
    assert "cycle-00" not in session.runs
    assert smoke.Session(tmp_path).stopped()          # a new session in the same folder stays stopped


def test_no_free_screen_is_a_skip_and_launches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       no_launch: list[Any]) -> None:
    monkeypatch.setattr(entry, "probe_desktop", lambda: _desktop(covered=True))
    monkeypatch.delenv("VOICETOOL_TEST_BLOCK_PROCESSES", raising=False)
    monkeypatch.delenv(entry.TEST_SCREEN_ENV, raising=False)
    session = smoke.Session(tmp_path)
    with pytest.raises(smoke.NoFreeScreen, match="skipped: no free screen"):
        session.launch(smoke.Spec(name="smoke"))
    assert not no_launch
    assert session.runs["smoke"]["skipped"] == "skipped: no free screen"


def test_run_cleanup_cannot_escape_the_sessions_runs_folder(tmp_path: Path) -> None:
    session = smoke.Session(tmp_path / "out")
    protected = tmp_path / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="inside runs"):
        smoke.Run(session, smoke.Spec(name="../.."), {})
    assert protected.read_text(encoding="utf-8") == "keep"


def test_any_focus_incident_fails_the_native_step(tmp_path: Path) -> None:
    session = smoke.Session(tmp_path)
    def incident() -> None:
        session.runs["last-window"] = {"focus": {"incidents": [{"seen_by": "guard", "ok": False}]}}
    assert "fail" in _attempt(session, incident)


def test_preflight_names_the_screen_the_app_will_pick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "probe_desktop", lambda: _desktop(covered=False))
    monkeypatch.delenv(entry.TEST_SCREEN_ENV, raising=False)
    run = smoke.Run(smoke.Session(tmp_path), smoke.Spec(name="x"), {})
    assert run.preflight() == {"device": "\\\\.\\DISPLAY1", "primary": True, "work": [0, 0, 1920, 1080], "dpi": 96}


def test_game_snapshot_defers_before_private_desktop_or_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              no_launch: list[Any]) -> None:
    monkeypatch.setenv("VOICETOOL_TEST_BLOCK_PROCESSES", "Destiny2.exe, destiny2launcher.exe")
    monkeypatch.setattr(smoke.winprobe, "list_processes", lambda **kw: [(88, 1, "DESTINY2.EXE")])
    with pytest.raises(smoke.NoFreeScreen, match="deferred: DESTINY2.EXE"):
        smoke.Session(tmp_path).launch(smoke.Spec(name="game"))
    assert not no_launch


def test_game_snapshot_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICETOOL_TEST_BLOCK_PROCESSES", "destiny2.exe")

    def inaccessible(*, strict: bool):
        assert strict
        raise PermissionError("snapshot denied")

    monkeypatch.setattr(smoke.winprobe, "list_processes", inaccessible)
    assert "snapshot denied" in smoke.winprobe.running_test_blocker()


def test_child_refuses_input_or_unexpected_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "VoiceTool-test-abc"
    monkeypatch.setattr(smoke.isolation, "_name", lambda _: name)
    monkeypatch.setattr(smoke.isolation, "input_name", lambda: name)
    with pytest.raises(RuntimeError, match="outside its private desktop"):
        smoke.isolation.require_private(name)
    monkeypatch.setattr(smoke.isolation, "input_name", lambda: "Default")
    with pytest.raises(RuntimeError, match="outside its private desktop"):
        smoke.isolation.require_private("VoiceTool-test-unrelated")
    smoke.isolation.require_private(name)


def test_private_observers_release_their_desktop_even_after_a_failed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = smoke.isolation.TestDesktop.__new__(smoke.isolation.TestDesktop)
    desktop.name, desktop.handle = "VoiceTool-test-fixture", 31
    assigned = []
    monkeypatch.setattr(smoke.isolation, "thread_desktop", lambda tid: 27)
    monkeypatch.setattr(smoke.isolation, "set_desktop", lambda h: assigned.append(h) or True)
    monkeypatch.setattr(smoke.isolation, "require_private", lambda name: None)
    with pytest.raises(ValueError, match="failed probe"):
        with desktop.attached():
            raise ValueError("failed probe")
    assert assigned == [31, 27]


def test_failure_during_private_preparation_records_input_and_closes_desktop(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []

    class Desktop:
        name, input = "VoiceTool-test-fixture", "Default"

        def close(self):
            closed.append(self.name)

    def refused(_):
        raise RuntimeError("could not attach")

    monkeypatch.delenv("VOICETOOL_TEST_BLOCK_PROCESSES", raising=False)
    monkeypatch.setattr(smoke.isolation, "TestDesktop", Desktop)
    monkeypatch.setattr(smoke.isolation, "input_name", lambda: "Default")
    monkeypatch.setattr(smoke.Run, "execute", refused)
    session = smoke.Session(tmp_path)
    with pytest.raises(RuntimeError, match="could not attach"):
        session.launch(smoke.Spec(name="failed-preparation", browser=True))
    assert closed == [Desktop.name]
    assert session.runs["failed-preparation"]["desktop"]["input_after"] == "Default"


def test_1440p_display_cannot_replace_the_required_1080p_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                no_launch: list[Any]) -> None:
    bounds = entry.Rect(0, 0, 2560, 1440)
    monitor = entry.Screen("DISPLAY2", True, bounds, bounds, 96, bounds)
    monkeypatch.setattr(entry, "probe_desktop", lambda: entry.Desktop((monitor,), None, None))
    monkeypatch.delenv("VOICETOOL_TEST_BLOCK_PROCESSES", raising=False)
    with pytest.raises(smoke.NoFreeScreen, match="no free screen"):
        smoke.Session(tmp_path).launch(smoke.Spec(name="wrong-display"))
    assert not no_launch


def test_the_launch_is_a_scratch_test_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", "elsewhere")
    run = smoke.Run(smoke.Session(tmp_path), smoke.Spec(name="x", scenario="generating", plan="capture",
                                                        args={"label": "g"}), {})
    command, env = run.command(), run.environment()
    assert command[0].lower().endswith("pythonw.exe") and command[2:4] == [smoke.LAUNCHER_FLAG, "--"]
    assert "--fake=generating" in command and "--smoke=capture" in command
    assert f"--data-root={run.data}" in command and str(tmp_path) in str(run.data)
    assert env["TEMP"] == env["TMP"] == str(run.temp)
    assert env["VOICETOOL_TEST_WINDOW"] == "1" and env["VOICETOOL_TEST_RETURN_HWND"] == "0"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["HF_HUB_OFFLINE"] == "1"
    assert json.loads(env[smoke.SMOKE_ARGS_ENV]) == {"label": "g"}
    assert "PYTEST_CURRENT_TEST" not in env and "NICEGUI_STORAGE_PATH" not in env
    assert str(tmp_path) in env[smoke.STORAGE_ENV] and str(tmp_path) in env[smoke.LOCK_DIR_ENV]


def test_the_launcher_keeps_the_install_folder_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "environment", entry.environment)          # restored after the test
    monkeypatch.setattr(entry, "instance_lock_path", entry.instance_lock_path)
    monkeypatch.setenv(smoke.STORAGE_ENV, str(tmp_path / "nicegui"))
    monkeypatch.setenv(smoke.LOCK_DIR_ENV, str(tmp_path / "lock"))
    smoke._patch_entry()  # noqa: SLF001
    flags = entry.read_flags(["--fake=speak", "--smoke=quit"], env={})
    values = entry.environment(flags, smoke.APP_ROOT, offline=True, simulation=False, env={})
    assert values["NICEGUI_STORAGE_PATH"] == str(tmp_path / "nicegui")
    assert entry.instance_lock_path(smoke.APP_ROOT, fake=True) == tmp_path / "lock" / "gui-dev.lock"
    assert "--no-proxy-server" in values[entry.WEBVIEW2_ENV]


def test_steady_growth_means_every_sample_rising_and_more_than_ten_percent() -> None:
    assert smoke.steady_growth([100, 104, 108, 115])
    assert not smoke.steady_growth([100, 104, 103, 115])         # not monotonic: noise
    assert not smoke.steady_growth([100, 101, 102, 103])         # monotonic but under 10 %
    assert not smoke.steady_growth([100, None, 120])             # too few samples


def test_only_loopback_endpoints_pass() -> None:
    def endpoint(state: str, local: str, remote: str) -> dict[str, Any]:
        return {"state": state, "local": local, "lport": 1, "remote": remote, "rport": 2}

    assert smoke._loopback_endpoint(endpoint("LISTEN", "127.0.0.1", "0.0.0.0"))  # noqa: SLF001
    assert not smoke._loopback_endpoint(endpoint("LISTEN", "0.0.0.0", "0.0.0.0"))  # noqa: SLF001
    assert smoke._loopback_endpoint(endpoint("ESTABLISHED", "127.0.0.1", "127.0.0.1"))  # noqa: SLF001
    assert not smoke._loopback_endpoint(endpoint("ESTABLISHED", "192.168.1.5", "20.1.2.3"))  # noqa: SLF001


def _finished_run(tmp_path: Path, **record: Any) -> smoke.Run:
    run = smoke.Run(smoke.Session(tmp_path), smoke.Spec(name="q"), {})
    run.record.update(record)
    run.events = [{"event": "quit_requested", "t": 100.0}, {"event": "on_shutdown", "t": 100.5}]
    run.t_exit = 101.2
    return run


def test_the_quit_check_wants_exit_0_within_3_s_and_nothing_left(tmp_path: Path) -> None:
    good = _finished_run(tmp_path, exit_code=0, tree_gone_after_exit_s=0.1, temp_left=["voicetool-fake-audio"],
                         log_problems={})
    assert smoke.quit_check("q", good).ok
    left = _finished_run(tmp_path, exit_code=0, tree_gone_after_exit_s=0.1, temp_left=["tmpab\\EBWebView"],
                         log_problems={})
    assert not smoke.quit_check("q", left).ok
    slow = _finished_run(tmp_path, exit_code=0, tree_gone_after_exit_s=0.1, temp_left=[], log_problems={})
    slow.t_exit = 103.5
    assert not smoke.quit_check("q", slow).ok
    crashed = _finished_run(tmp_path, exit_code=1, tree_gone_after_exit_s=0.1, temp_left=[],
                            log_problems={"server.log": ["Traceback (most recent call last):"]})
    assert not smoke.quit_check("q", crashed).ok


def test_the_event_tail_never_returns_half_a_line(tmp_path: Path) -> None:
    path = tmp_path / "plan.jsonl"
    log = eventlog.EventLog(path, "plan")
    tail = eventlog.Tail(path)
    log.emit("a")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write('{"seq": 2, "event": "b"')                  # a torn write
    assert [e["event"] for e in tail.read()] == ["a"]
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("}\n")
    assert [e["event"] for e in tail.read()] == ["b"]
    eventlog.write_ack(tmp_path, 7, {"ok": True})
    assert json.loads(eventlog.ack_path(tmp_path, 7).read_text(encoding="utf-8")) == {"ok": True}
