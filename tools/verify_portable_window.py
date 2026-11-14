"""Smoke-test a packaged WinForms/WebView2 launch on a never-shown Win32 desktop."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import _winapi
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.native import desktop, winprobe
from tools.build_portable import application_files


def stage_test_app(installed: Path, scratch: Path) -> None:
    """Copy program files only, sharing the large read-only wheels and model snapshots.

    Keep the interpreter, standard library, app, browser profile, locks, caches, settings
    and logs in the scratch folder. No installed library file is a test input.
    """
    for rel in (*application_files(), Path("Emiya Voice.exe"), Path("portable-build.json")):
        dest = scratch / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(installed / rel, dest)
    runtime = installed / "runtime"
    for file in runtime.iterdir():
        if file.is_file():
            dest = scratch / "runtime" / file.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(file, dest)
    shutil.copytree(runtime / "Lib", scratch / "runtime/Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
    for rel in ("runtime/DLLs", "runtime/Lib/site-packages", "models"):
        _winapi.CreateJunction(str(installed / rel), str(scratch / rel))
    tests = scratch / "tests"
    native = tests / "native"
    native.mkdir(parents=True)
    for path in (tests / "__init__.py", native / "__init__.py"):
        path.touch()
    shutil.copyfile(ROOT / "tools/portable/settings_probe.py", native / "smoke_plan.py")
    shutil.copyfile(ROOT / "tools/portable/device_menu_probe.py", native / "device_menu_probe.py")


def verify(folder: Path, capture: Path | None = None) -> dict:
    private = desktop.TestDesktop()
    result = {"input_before": private.input, "private_desktop": private.name,
              "launcher": "Emiya Voice.exe", "window": False, "webview_profile_in_folder": False}
    try:
        with tempfile.TemporaryDirectory(prefix="emiya-native-") as name, private.attached():
            before = winprobe.running_test_blocker()
            if before:
                raise RuntimeError(f"Game or unavailable process snapshot: {before}")
            scratch = Path(name)
            stage_test_app(folder, scratch)
            probe = scratch / "settings-probe.json"
            env = dict(os.environ, VOICETOOL_TEST_WINDOW="1", HF_HUB_OFFLINE="1",
                       VOICETOOL_PORTABLE_PROBE=str(probe),
                       WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--mute-audio --remote-debugging-port=0")
            args = [str(scratch / "Emiya Voice.exe"), "--smoke=portable-settings"]
            child = private.start(args, Path(os.environ["WINDIR"]), env)
            tree = winprobe.ProcessTree(child.pid)
            try:
                deadline = time.monotonic() + 50
                closing = False
                while time.monotonic() < deadline and child.poll() is None:
                    tree.refresh()
                    hwnds = winprobe.app_windows("Emiya Voice", tree.pids())
                    result["window"] |= bool(hwnds)
                    # The browser child and its profile must both have materialized.
                    profile_files = (scratch / "portable-data/temp").glob("*/EBWebView/Local State")
                    result["webview_profile_in_folder"] |= next(profile_files, None) is not None
                    ready, ack = scratch / "menu-open", scratch / "menu-captured"
                    if hwnds and ready.is_file() and not ack.exists():
                        frame = winprobe.capture_window(hwnds[0])
                        if winprobe.is_blank(winprobe.image_stats(frame)):
                            raise RuntimeError("Empty private-window capture")
                        if capture:
                            capture.parent.mkdir(parents=True, exist_ok=True)
                            frame.save(capture)
                        result["captured_size"] = frame.size
                        ack.touch()
                    if hwnds and result["webview_profile_in_folder"] and not closing and probe.is_file():
                        time.sleep(1)
                        result.update(json.loads(probe.read_text(encoding="utf-8")))
                        winprobe.user32.PostMessageW(hwnds[0], 0x0010, 0, 0)  # WM_CLOSE, private HWND only
                        closing = True
                    time.sleep(.1)
                result["exit_code"] = child.poll()
            finally:
                if child.poll() is None:
                    tree.refresh()
                    tree.kill_all()
                    child.wait(10)
                tree.close()
                child.close()
    finally:
        result["input_after"] = desktop.input_name()
        private.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--capture", type=Path, help="Save the expanded menu from the private test window")
    args = parser.parse_args()
    os.environ["VOICETOOL_TEST_BLOCK_PROCESSES"] = "destiny2.exe,destiny2launcher.exe"
    data = verify(args.folder.resolve(), args.capture)
    print(json.dumps(data, indent=2))
    raise SystemExit(0 if data.get("device_dropdown") and data.get("effects_visible")
                     and data["exit_code"] == 0 and data["window"] and data["webview_profile_in_folder"]
                     and data["input_before"] == data["input_after"] != data["private_desktop"] else 1)
