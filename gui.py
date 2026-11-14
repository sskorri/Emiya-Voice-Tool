"""Emiya Voice: the entry script (BUILD_SPEC §6.2.11).

    venv\\Scripts\\pythonw.exe gui.py                    the app (no console, like a desktop shortcut)
    venv\\Scripts\\python.exe  gui.py --fake[=SCENARIO]  FakeEngine on scratch data in .dev
    flags: --browser [--no-show], --data-root=PATH, --smoke[=PLAN], --fx-telemetry, --debug-webview

Both processes run this module level: the server (__main__) and pywebview's window process, which
re-imports it as __mp_main__. The steps keep the spike-proven order: streams, flags, environment,
manifest (a stdlib read, so its title names the app), single instance, MIME types, NiceGUI, window
chrome. The helpers live in ui.entry; nothing
here imports torch, engine, config, ui.core, ui.launch or the theme at module level, so the window
process stays light. Under NiceGUI's user fixture (SIMULATION) there are no streams to fix, no instance
lock and no window: entry.fail() raises BootFailure instead of exiting, and main() returns None once the
app is configured. --smoke (or VOICETOOL_TEST_WINDOW=1) opens a test window on a free screen without
taking the focus, or skips the run with exit code 8.
"""
from __future__ import annotations

import os
import sys

from ui import entry
from ui.contract.manifest import window_args

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WINDOW_PROCESS = __name__ == "__mp_main__"
SIMULATION = entry.is_simulation()


def _window_ready() -> None:
    """pywebview's start callback, in a thread of the window process (§6.2.11 step 11)."""
    import webview  # NiceGUI has imported it in this process already

    if webview.windows:
        entry.window_ready(webview.windows[0], icon=MANIFEST.window.icon, test_window=FLAGS.test_window)


# 1. pythonw: None streams go to logs\gui.log or logs\gui-window.log before anything writes to them
REDIRECTED = not SIMULATION and entry.redirect_streams(os.path.join(APP_ROOT, "logs"), window=IS_WINDOW_PROCESS)

# 2. flags (the user fixture's come from its environment)
FLAGS = entry.simulation_flags() if SIMULATION else entry.read_flags(sys.argv[1:])
if REDIRECTED and not IS_WINDOW_PROCESS:
    sys.excepthook = entry.crash_hook(quiet=FLAGS.test_window)
if FLAGS.test_window:
    entry.enable_test_events()

# 3. Offline Mode, read-only, and what NiceGUI, WebView2, huggingface_hub and tqdm read at import
OFFLINE = entry.read_offline(entry.data_root(APP_ROOT, FLAGS) / entry.SETTINGS_FILE)
os.environ.update(entry.environment(FLAGS, APP_ROOT, offline=OFFLINE, simulation=SIMULATION))

# 4. the active theme's manifest (a stdlib read); its window title names the app from here on
MANIFEST = entry.load_theme_manifest(os.path.join(APP_ROOT, "ui", "themes", entry.ACTIVE_THEME))
if isinstance(MANIFEST, int):
    if IS_WINDOW_PROCESS:
        raise entry.BootFailure(MANIFEST, MANIFEST.text)
    entry.fail(MANIFEST, MANIFEST.text, quiet=FLAGS.test_window)
entry.use_app_name(MANIFEST.window.title)

# 5. single instance: the server only, before the slow NiceGUI import; a second launch raises the running window
INSTANCE_LOCK = None
if __name__ == "__main__" and not SIMULATION:
    INSTANCE_LOCK = entry.take_instance_lock(entry.instance_lock_path(APP_ROOT, fake=FLAGS.fake is not None))
    if isinstance(INSTANCE_LOCK, int):
        BROUGHT_FORWARD = not FLAGS.test_window and entry.bring_window_forward(MANIFEST.window.title)
        entry.fail(INSTANCE_LOCK, INSTANCE_LOCK.text, quiet=FLAGS.test_window or BROUGHT_FORWARD)

# where the window goes: centred on the primary, or a test window's free screen (chosen by the server)
PLACEMENT = entry.window_placement(MANIFEST, FLAGS, window_process=IS_WINDOW_PROCESS, simulation=SIMULATION)
if isinstance(PLACEMENT, int):
    entry.fail(PLACEMENT, PLACEMENT.text, quiet=True)

# 6. MIME types this venv lacks
entry.register_font_types()
if not SIMULATION:
    entry.prime_platform_cache()              # platform.system() in aiohttp must not create a console host

# 7. NiceGUI
from nicegui import app, ui  # noqa: E402

if IS_WINDOW_PROCESS and not FLAGS.browser:
    from nicegui.native import window_icon

    entry.prepare_window_icons(window_icon)

# Before webview.start constructs the form: its background callback is too late to prevent Show().
if IS_WINDOW_PROCESS and FLAGS.test_window and not FLAGS.browser:
    from webview.platforms.winforms import BrowserView

    entry.prevent_test_activation(BrowserView.BrowserForm)

# 8. window chrome, identical in both processes (the window process's re-imported values win)
if not FLAGS.browser:
    app.native.window_args.update(window_args(MANIFEST), **PLACEMENT)
    app.native.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    if not SIMULATION:
        app.native.start_args["func"] = _window_ready
    if FLAGS.debug_webview:
        app.native.start_args["debug"] = True

# 9. ui.run's arguments
RUN_KWARGS = entry.run_arguments(MANIFEST, FLAGS)

# 10. only the server composes the app; in the window process ui.run returns at once
if __name__ == "__main__":
    from ui.launch import main

    CODE = main(FLAGS, MANIFEST, RUN_KWARGS, app_root=APP_ROOT, instance_lock=INSTANCE_LOCK)
    if CODE:
        entry.fail(CODE, getattr(CODE, "text", f"stopped with exit code {CODE}"), quiet=FLAGS.test_window)
elif __name__ == "__mp_main__":
    ui.run(**RUN_KWARGS)
