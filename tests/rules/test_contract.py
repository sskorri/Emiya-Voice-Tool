"""Contract tests (U1).

- Every §4 record and state mirror, §5.0 KitPort, the §5.1 plans and the §5.12 view models exist with
  their fields, frozen or bindable as the spec says, and bindings to them are never polled.
- The controller protocols mirror the §5.2-§5.11 signatures.
- The manifest loader handles the theme_boot spike's 21 hostile inputs (adapted to theme.toml) and the
  contract's own rules; run_kwargs() serves --browser on loopback only.
- The test layout keeps tests\\engine from shadowing the app's engine package (§8.1).
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import os
import pickle
import socket
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui import binding

from ui.contract import CONTRACT_VERSION, markers, plans, ports, records, routes, state, theme
from ui.contract import manifest as mf
from ui.contract import viewmodels as vms

ROOT = Path(__file__).resolve().parents[2]
ICO_BYTES = b"\x00\x00\x01\x00\x01\x00" + bytes(16)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def _run_python(code: str, *args: str, cwd: Path = ROOT) -> str:
    """Run code in a fresh interpreter (offline) and return its stdout."""
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


# ---- types and their fields ----------------------------------------------------------------------

FROZEN_FIELDS: dict[type, str] = {
    # §4 records (contract/records.py)
    records.VoiceRow: "voice_id backend backend_enabled language language_name exaggeration created "
                      "reference_name reference_path clip_s pt pt_exists consent",
    records.Take: "id voice backend language text exaggeration cfg_weight seconds gen_seconds tokens "
                  "hit_token_limit created session starred wav sample_rate peaks missing",
    records.LoadEvent: "t kind backend from_backend seconds gb peak_gb reason device cold",
    records.ClipInfo: "clip_id name source staged in_library duration_s selection_s samplerate channels fmt "
                      "subtype peak_dbfs clipped silent peaks",
    records.CheckRow: "label detail tone",
    records.ClipMark: "label pct",
    # plans.py
    plans.ActionState: "enabled label reason pending aria tone fill indeterminate armed tooltip hint",
    plans.Toast: "text bad seq",
    plans.PlayerTarget: "seq src playing at muted",
    plans.GenerateRequest: "voice_id backend text language exaggeration cfg_weight call_epoch",
    plans.ForgeRequest: "voice_id clip backend language exaggeration consent replace",
    plans.AfterGenerate: "reqs",
    plans.AfterForge: "req",
    plans.SwitchPlan: "to_backend from_backend reason label needs_bytes needs_estimated free_after_bytes "
                      "low_vram about_s last_s after",
    plans.ConfirmRequest: "token kind style title lines primary danger switch",
    # §5.12 row and nested VMs
    vms.DockRowVM: "backend label tip state info info_tone enabled dim ring flash_seq spark_seq",
    vms.VoiceGroupVM: "backend label chip chip_tone enabled rows",
    vms.VoiceItemVM: "voice_id sub sub_tone selected flash_seq audition_label enabled",
    vms.LangItemVM: "code name selected mark",
    vms.TakeCardVM: "visible generating pinned title caption caption_warn star_visible starred wave play "
                    "time_text ab_visible ab_a_label ab_b_label ab_current save",
    vms.WaveVM: "take_id bars length_s generating fill new_seq spark_seq",
    vms.TakeRowVM: "id text lang voice model_label expr_text guid_text length_text when_text when_tip current "
                   "pin_a pin_b starred missing is_new flash_seq",
    vms.GeneratingRowVM: "take_no voice expr_text guid_text",
    vms.VoiceListRowVM: "voice_id model_label model_resident model_tip language_name expr_text clip_text "
                        "clip_missing created_text created_tip selected",
    vms.VoiceDetailsVM: "voice_id title caption chip_text chip_tone play playing bars bars_aria language_mode "
                        "language_name lang_open lang_items expr_enabled expr expr_text ref_text ref_tip "
                        "ref_missing cache_text cache_missing reveal_enabled consent_ok consent_text "
                        "edits_enabled speak_label reforge copy copy_visible replace delete",
    vms.CompareVM: "sides rows note note_differs play_label playing time_text show_ab keep_a keep_b reuse",
    vms.CompareSideVM: "side take_id title title_active length_text bars progress missing",
    vms.CompareRowVM: "label a b differs",
    vms.ModelCardVM: "backend label tip state chip_text chip_tone ring facts action action_kind ringed flash_seq",
    vms.FactVM: "label value tip",
    vms.KeyRowVM: "id label key_text alt_text key_listening alt_listening key_aria alt_aria",
    vms.AboutVM: "engine gpu gpu_tip models_in_vram registry registry_tip cache cache_tip clips clips_tip takes",
    vms.SearchItem: "item_id label route_text",
    vms.BindResult: "ok text",
    # theme.py and manifest.py
    theme.Motion: "policy fx_enabled",
    theme.ThemeContext: "core behaviours prefs motion asset browser fx_telemetry",
    mf.WindowSpec: "title frameless size min_size resizable background_color text_select center icon",
    mf.ThemeManifest: "slug name contract dark css window palette fps_cap prefs folder",
}

BINDABLE_FIELDS: dict[type, str] = {
    # §4.10 mirrors (contract/state.py)
    state.EngineState: "resident failed enabled max_resident device rev",
    state.JobState: "kind phase source target voice take_no queued started_at elapsed_s audio_s fill rev",
    state.DeviceState: "detected cuda name short_name total_bytes free_bytes app_bytes other_bytes peak_bytes "
                       "error measured_at",
    state.WindowState: "visible",
    state.Session: "voice_id text language exaggeration cfg_weight take_id pin_a pin_b call_switch",
    state.LibraryState: "rows status error corrupt_files registry_mtime rev",
    state.TakesState: "takes next_id rev",
    state.LogState: "lines",
    state.HistoryState: "events",
    state.AboutFacts: "engine gpu models_in_vram registry cache clips takes",
    # §5.12 view models
    vms.ShellVM: "title route chip_text chip_tone chip_dot dock_rows dock_status dock_status_tone vram_value "
                 "vram_gold vram_app_pct vram_other_pct vram_detail vram_tip meter_visible free "
                 "show_failed_actions retry load_default ask ask_dont ask_dont_visible ask_then close log_open "
                 "toast art_visible art_page script_dark reduce_always logo_still search_hint search_items "
                 "search_badge",
    vms.SpeakVM: "library_state library_message groups count_text show_sample sample_tip delivery_enabled "
                 "presets expr expr_text expr_tone expr_tip expr_tick guid guid_text language_mode "
                 "language_name lang_open lang_items show_reset note note_tone show_guid0 stats_text "
                 "stats_warn toolbar_mode lang_chip_text tags clear text_lang generate generate3 "
                 "show_cancel_queue summary_text summary_tone card session_visible session_rows generating_row",
    vms.VoicesVM: "query backend_filter segments count_text rows empty_text details no_details_text",
    vms.ForgeVM: "clip_state clip_file clip_meta clip_meta_tone bars marks sel_pct bars_aria clip_note checks play "
                 "playing browse name name_msg name_tone name_bad fix_label segments model_note model_note_tone "
                 "lang_locked language_name lang_open lang_items expr expr_text expr_enabled expr_note consent "
                 "consent_req_tone inputs_enabled forge tracing trace_progress forged speak_label test_line "
                 "another",
    vms.TakesVM: "tab list_title count_text query voice_label model_label voice_options model_options rows "
                 "more_count empty_text export folder purge foot_text compare",
    vms.ModelsVM: "cards vram_value vram_gold vram_app_pct vram_other_pct vram_detail vram_tip meter_visible free "
                  "legend_app legend_other legend_free gpu_line gpu_tip warmup idle_min confirm_switches "
                  "limit_text limit_tip history history_empty",
    vms.SettingsVM: "tab out_dir_text out_tip browse autoplay stop_drops_queue device device_note default_backend backend_options "
                    "offline fx_on fx_speed fx_speed_text work_glow reduce_motion saber_art key_rows about "
                    "call_enabled call_devices call_device call_note call_send call_hear call_overlap call_delay "
                    "call_shortcuts",
    vms.PlaybackVM: "target playing position duration call_enabled call_available call_active call_hear "
                    "call_switch_visible call_status",
}


def _field_names(cls: type) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


@pytest.mark.parametrize("cls", list(FROZEN_FIELDS), ids=lambda c: c.__name__)
def test_frozen_types_have_the_listed_fields(cls: type) -> None:
    assert _field_names(cls) == tuple(FROZEN_FIELDS[cls].split())
    assert cls.__dataclass_params__.frozen


@pytest.mark.parametrize("cls", list(BINDABLE_FIELDS), ids=lambda c: c.__name__)
def test_bindable_types_have_the_listed_fields(cls: type) -> None:
    assert _field_names(cls) == tuple(BINDABLE_FIELDS[cls].split())
    assert all(isinstance(vars(cls)[name], binding.BindableProperty) for name in _field_names(cls))
    cls()  # every mirror and VM has a default for each field


@pytest.mark.parametrize("cls", list(BINDABLE_FIELDS), ids=lambda c: c.__name__)
def test_bindings_to_bindable_fields_are_never_polled(cls: type) -> None:
    """A binding from a bindable field adds no active link (R9's precondition)."""
    source, target = cls(), types.SimpleNamespace(value=None)
    before = list(binding.active_links)
    try:
        for name in _field_names(cls):
            binding.bind_from(target, "value", source, name)
            assert target.value == getattr(source, name)
        assert binding.active_links == before
    finally:
        binding.remove([source, target])


def test_app_state_bundles_every_mirror_and_is_frozen() -> None:
    app_state = state.AppState()
    expected = {"engine": state.EngineState, "job": state.JobState, "device": state.DeviceState,
                "window": state.WindowState, "library": state.LibraryState, "takes": state.TakesState,
                "log": state.LogState, "history": state.HistoryState, "about": state.AboutFacts}
    assert {name: type(getattr(app_state, name)) for name in _field_names(state.AppState)} == expected
    with pytest.raises(dataclasses.FrozenInstanceError):
        app_state.job = state.JobState()  # type: ignore[misc]
    assert state.AppState().job is not app_state.job


def test_records_round_trip_and_defaults() -> None:
    take = records.Take(id=7, voice="aiko", backend="chatterbox-multilingual", language="ja", text="t",
                        exaggeration=0.5, cfg_weight=0.5, seconds=4.6, gen_seconds=6.9, tokens=115,
                        hit_token_limit=False, created="2026-11-13T14:31:00+01:00", session="s",
                        starred=True, wav="outputs/takes/aiko_take7.wav", peaks=(1, 2, 3))
    assert (take.sample_rate, take.missing) == (24000, False)
    saved = {k: v for k, v in dataclasses.asdict(take).items() if k != "missing"}
    assert records.Take(**saved) == take
    event = records.LoadEvent(t="2026-11-13T14:00:00+01:00", kind="loaded", backend="chatterbox-turbo")
    assert (event.from_backend, event.reason, event.device, event.cold) == ("", "", "cuda", False)
    assert records.CheckRow("Length", "11.2 s · ideal").tone == "ok"


def test_plans_defaults_and_hashability() -> None:
    action = plans.ActionState(False, "Pick a Voice")
    assert (action.reason, action.pending, action.aria, action.tone, action.fill, action.indeterminate,
            action.armed, action.tooltip, action.hint) == ("", None, "", "primary", None, False, False, "", ())
    plan = plans.SwitchPlan("chatterbox-turbo", "chatterbox-multilingual", "generate",
                            "Switch to Turbo & Generate", 3 << 30, False, 6 << 30, False,
                            after=plans.AfterGenerate((plans.GenerateRequest("host-turbo", "chatterbox-turbo",
                                                                             "Hi.", "en", None, None),)))
    request = plans.ConfirmRequest("tok", "switch_model", "named", switch=plan)
    hash(plans.ActionState(True, "Switch to Turbo & Generate", pending=request, hint=("Ctrl", "Enter")))
    assert plans.PlayerTarget() == plans.PlayerTarget(0, "", False, None)
    assert plans.Toast("Freed 3.4 GB").bad is False


def test_contract_version() -> None:
    """1.1 added StatusController.escape() and TakesController.opened(); a theme declaring contract [1, 0] still
    loads (the major matches)."""
    assert CONTRACT_VERSION == (1, 2)


def test_routes_page_ids_and_titles() -> None:
    assert routes.ROUTES == ("/", "/voices", "/forge", "/takes", "/models", "/settings")
    assert dict(routes.PAGE_ID) == {"/": "speak", "/voices": "voices", "/forge": "forge", "/takes": "takes",
                                    "/models": "models", "/settings": "settings"}
    assert dict(routes.TITLES) == {"/": "Speak", "/voices": "Voices", "/forge": "Forge", "/takes": "Takes",
                                   "/models": "Models", "/settings": "Settings"}
    with pytest.raises(TypeError):
        routes.PAGE_ID["/x"] = "x"  # type: ignore[index]


def test_markers() -> None:
    assert markers.SHELL_MARKERS == ("nav.speak", "nav.voices", "nav.forge", "nav.takes", "nav.models",
                                     "nav.settings", "status")
    assert markers.FRAMELESS_MARKERS == ("window.minimize", "window.close")
    assert dict(markers.CONFIRM_MARKERS) == {"arm": "confirm.arm", "ask": "confirm.ask"}
    assert tuple(markers.REQUIRED_MARKERS) == routes.ROUTES
    assert markers.REQUIRED_MARKERS["/settings"] == ("settings.core", "settings.about")  # no settings.theme yet
    assert markers.REQUIRED_MARKERS["/"] == ("speak.voice", "speak.text", "speak.delivery", "speak.generate",
                                             "speak.status", "take.player")
    for route, names in markers.REQUIRED_MARKERS.items():
        assert names and all(name.split(".")[0] in (routes.PAGE_ID[route], "take") for name in names)


# ---- protocols -----------------------------------------------------------------------------------

def _shape(func: Callable[..., Any]) -> str:
    """'async (a, *, b=)' for a method: parameter names after self, '=' for a default, '*' for keyword-only."""
    out, keyword_only = [], False
    for param in list(inspect.signature(func).parameters.values())[1:]:
        if param.kind is param.KEYWORD_ONLY and not keyword_only:
            out.append("*")
            keyword_only = True
        out.append(param.name if param.default is param.empty else f"{param.name}=")
    return ("async " if inspect.iscoroutinefunction(func) else "") + "(" + ", ".join(out) + ")"


def _methods(protocol: type) -> dict[str, str]:
    """Public methods declared on the protocol and its protocol bases, as _shape strings."""
    found: dict[str, str] = {}
    for cls in reversed(protocol.__mro__):
        if cls is object or not getattr(cls, "_is_protocol", False):
            continue
        found.update({name: _shape(member) for name, member in vars(cls).items()
                      if inspect.isfunction(member) and not name.startswith("_")})
    return found


CONFIRMABLE = {"confirm": "async (token)", "dismiss": "()"}

PROTOCOLS: dict[type, dict[str, str]] = {
    ports.KitPort: {
        "pick_file": "async (*, title, filters, start_dir)", "pick_folder": "async (*, title, start_dir)",
        "save_as": "async (*, default_name, filters, start_dir)", "reveal": "(path)", "open_folder": "(path)",
        "quit": "()", "minimize": "()",
    },
    vms.StatusController: {
        "toggle_log": "()", "close_log": "()", "request_quit": "()", "minimize": "()", "on_route": "(path)",
        "escape": "()", "dismiss_toast": "()",
    },
    vms.ModelsController: {
        **CONFIRMABLE, "request_switch": "(backend, *, origin)", "set_dont_ask": "(on)", "free_vram": "async ()",
        "set_warmup": "(mode)", "set_idle_unload": "(minutes)", "set_confirm_switches": "(on)",
    },
    vms.PlaybackController: {
        "send_take": "(take_id)", "stop_sending": "()",
        "play_take": "(take_id)", "toggle": "()", "seek": "(seconds)", "ab_switch": "(take_id)",
        "pin": "(take_id, side)", "star": "(take_id)", "prev": "()", "next": "()", "audition": "(voice_id)",
        "play_url": "(url)", "stop": "()", "on_player_state": "(playing, position, duration, ended, error)",
        "save_as": "async ()", "reveal": "()",
    },
    vms.CommandRegistry: {
        "normalize": "(key, *, ctrl, shift, alt)", "bindings": "()", "combos": "()",
        "hint_parts": "(command_id)", "bind": "(command_id, slot, combo)", "handle": "(command_id, route)",
        "set_focus_search": "(focus)",
    },
    vms.SearchController: {"results": "(query)", "run": "(item_id)", "clear": "()"},
    vms.SpeakController: {
        "stop_take": "()", "set_call_switch": "(on)",
        **CONFIRMABLE, "pick_voice": "(voice_id)", "set_text": "(text)", "insert_tag": "(tag, caret)",
        "set_delivery": "(*, exaggeration=, cfg_weight=, language=)", "apply_preset": "(name)",
        "reset_delivery": "()", "set_guidance_zero": "()", "use_japanese": "()",
        "toggle_language_list": "(open=)", "clear_text": "()", "generate": "async (n=, token=)",
        "cancel_queue": "()", "opened": "()",
    },
    vms.VoicesController: {
        **CONFIRMABLE, "select": "(voice_id)", "set_filter": "(*, query=, backend=)",
        "edit": "async (voice_id, *, language=, exaggeration=)", "toggle_language_list": "(open=)",
        "delete": "async (voice_id)", "reforge": "(voice_id)", "forge_copy": "(voice_id)",
        "replace_clip": "async (voice_id)", "reveal_cache": "async (voice_id)", "speak_with": "(voice_id)",
        "toggle_clip_playback": "(voice_id)", "opened": "()",
    },
    vms.ForgeController: {
        **CONFIRMABLE, "browse": "async ()", "pick_clip": "async (path)", "drop_paths": "async (paths)",
        "upload": "async (name, data)", "use_sample": "async ()",
        "prefill": "(*, voice_id, mode, clip_path=)", "set_name": "(name)", "fix_name": "()",
        "set_backend": "(backend)", "set_language": "(code)", "toggle_language_list": "(open=)",
        "set_exaggeration": "(value)", "set_consent": "(given)", "forge": "async (token=)",
        "speak_with": "()", "test_line": "async ()", "forge_another": "()", "toggle_clip_playback": "()",
        "opened": "()",
    },
    vms.TakesController: {
        **CONFIRMABLE, "set_tab": "(tab)", "set_query": "(text)", "set_voice_filter": "(voice_id)",
        "set_model_filter": "(backend)", "row_click": "(take_id)", "compare_play": "()", "keep": "(side)",
        "reuse_settings": "()", "purge_unstarred": "async ()", "export_starred": "async ()",
        "show_folder": "()", "show_more": "()", "opened": "()",
    },
    vms.SettingsController: {
        "refresh_call_devices": "async ()", "select_call_device": "(device_id)", "set_call_preference": "(field, value)",
        **CONFIRMABLE, "set_tab": "(tab)", "browse_output_folder": "async ()", "set_autoplay": "(on)",
        "set_stop_drops_queue": "(on)",
        "set_device": "(device)", "set_default_backend": "(backend)", "set_offline": "(on)",
        "set_reduce_motion": "(policy)", "set_pref": "(key, value)", "preview_pref": "(key, value)",
        "listen": "(command_id, slot)",
        "stop_listening": "()", "capture": "(command_id, slot, *, key, ctrl, shift, alt, composing=)",
    },
    theme.Behaviours: {name: "()" for name in ("status", "models", "playback", "commands", "search", "speak",
                                                "voices", "forge", "takes", "settings")},
    theme.CoreView: {"take_url": "(take_id)", "clip_url": "(voice_id)", "staged_url": "(clip_id)"},
    theme.CoreEventsView: {"say": "(text, *, bad=)"},
    theme.ThemePrefs: {"get": "(key)", "set": "(key, value)"},
    theme.Theme: {"build_shell": "(ctx, mount_pages)"},
    vms.Confirmable: CONFIRMABLE,
}

PROTOCOL_ATTRIBUTES: dict[type, dict[str, str]] = {
    vms.StatusController: {"vm": "ShellVM"},
    vms.ModelsController: {"vm": "ModelsVM"},
    vms.PlaybackController: {"vm": "PlaybackVM"},
    vms.SpeakController: {"vm": "SpeakVM"},
    vms.VoicesController: {"vm": "VoicesVM"},
    vms.ForgeController: {"vm": "ForgeVM"},
    vms.TakesController: {"vm": "TakesVM"},
    vms.SettingsController: {"vm": "SettingsVM"},
    theme.CoreView: {"state": "AppState", "session": "Session", "events": "CoreEventsView"},
    theme.ThemePrefs: {"changed": "Event[[str]]"},
    theme.Theme: {"manifest": "ThemeManifest", "pages": "Mapping[Route, PageBuilder]"},
    theme.CoreEventsView: {"changed": "Event[[]]", "toast": "Event[[Toast]]", "flash": "Event[[str]]",
                           "spark": "Event[[str]]", "take_ready": "Event[[int]]", "forge_started": "Event[[str]]",
                           "forged": "Event[[str]]", "dropped": "Event[[tuple[str, ...]]]"},
}


@pytest.mark.parametrize("protocol", list(PROTOCOLS), ids=lambda p: p.__name__)
def test_protocol_signatures(protocol: type) -> None:
    assert _methods(protocol) == PROTOCOLS[protocol]


CONTROLLERS = [vms.Confirmable, vms.StatusController, vms.ModelsController, vms.PlaybackController,
               vms.CommandRegistry, vms.SearchController, vms.SpeakController, vms.VoicesController,
               vms.ForgeController, vms.TakesController, vms.SettingsController]


@pytest.mark.parametrize("protocol", CONTROLLERS, ids=lambda p: p.__name__)
def test_every_intent_of_the_contract_is_documented(protocol: type) -> None:
    """A second theme codes against these intents: each one says what it does and what it refuses (R5)."""
    undocumented = [name for name, member in vars(protocol).items()
                    if callable(member) and not name.startswith("_") and not (member.__doc__ or "").strip()]
    assert undocumented == []


@pytest.mark.parametrize("protocol", list(PROTOCOL_ATTRIBUTES), ids=lambda p: p.__name__)
def test_protocol_attributes(protocol: type) -> None:
    assert protocol.__annotations__ == PROTOCOL_ATTRIBUTES[protocol]


def test_kit_port_is_runtime_checkable() -> None:
    class FakeKit:
        async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
            return None

        async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
            return None

        async def save_as(self, *, default_name: str, filters: tuple[str, ...],
                          start_dir: Path | None) -> Path | None:
            return None

        def reveal(self, path: Path) -> None: ...
        def open_folder(self, path: Path) -> None: ...
        def quit(self) -> None: ...

        def minimize(self) -> bool:
            return False

    class NoQuit(FakeKit):
        quit = None  # type: ignore[assignment]

    assert isinstance(FakeKit(), ports.KitPort)
    assert not isinstance(object(), ports.KitPort)
    assert _methods(ports.KitPort) == {name: _shape(getattr(FakeKit, name)) for name in PROTOCOLS[ports.KitPort]}
    assert not isinstance(NoQuit(), ports.KitPort)


def test_light_contract_modules_import_without_nicegui() -> None:
    """gui.py and ui.entry import the manifest before NiceGUI, in both processes."""
    code = ("import json, sys\n"
            "import ui.contract.manifest, ui.contract.routes, ui.contract.markers, ui.contract.records, "
            "ui.contract.plans, ui.contract.ports\n"
            "heavy = ('nicegui', 'torch', 'engine', 'config', 'numpy', 'webview', 'fastapi', 'starlette')\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in heavy)))\n")
    assert json.loads(_run_python(code)) == []


def test_whole_contract_imports_nothing_from_the_app() -> None:
    """The contract never pulls in the engine, torch or another ui layer (themes import it at build time)."""
    code = ("import json, sys\n"
            "import ui.contract.state, ui.contract.viewmodels, ui.contract.theme\n"
            "app = ('torch', 'chatterbox', 'engine', 'config', 'soundfile', 'send2trash')\n"
            "layers = ('ui.core', 'ui.kit', 'ui.behaviours', 'ui.themes', 'ui.launch', 'ui.entry')\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in app or m.startswith(layers))))\n")
    assert json.loads(_run_python(code)) == []


# ---- the manifest --------------------------------------------------------------------------------

SPEC_TOML = """\
slug = "emiya"
name = "Emiya"
contract = [1, 0]
dark = true
css = ["emiya.css"]                  # emiya.css @imports pages/*.css

[window]
title = "Emiya Voice"                # M L5
frameless = true
size = [1280, 800]                   # M L19
min_size = [1280, 800]
resizable = false
background_color = "#0D0E10"         # M L17
text_select = false
center = true
icon = "static/icon.ico"

[palette]
primary = "#FCDB7F"
dark_page = "#0D0E10"
negative = "#F0706E"
warning = "#F2C14E"

[fx]
fps_cap = 60

[prefs]
fx_on = true
fx_speed = 100
work_glow = true
saber_art = "show"
"""

VALID: dict[str, Any] = {
    "slug": "emiya", "name": "Emiya", "contract": [1, 0], "dark": True, "css": ["emiya.css"],
    "window": {"title": "Emiya Voice", "frameless": True, "size": [1280, 800], "min_size": [1280, 800],
               "resizable": False, "background_color": "#0D0E10", "text_select": False, "center": True,
               "icon": "static/icon.ico"},
    "palette": {"primary": "#FCDB7F", "dark_page": "#0D0E10", "negative": "#F0706E", "warning": "#F2C14E"},
    "fx": {"fps_cap": 60},
    "prefs": {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"},
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"


def _toml(data: dict[str, Any]) -> str:
    """A TOML document: scalars first, then one [table] per dict value."""
    lines = [f"{key} = {_toml_value(value)}" for key, value in data.items() if not isinstance(value, dict)]
    for key, value in data.items():
        if isinstance(value, dict):
            lines += ["", f"[{key}]"] + [f"{k} = {_toml_value(v)}" for k, v in value.items()]
    return "\n".join(lines) + "\n"


def _theme_folder(base: Path, content: str | bytes | None = None, name: str = "emiya") -> Path:
    """A theme folder with icon and stylesheet; content is theme.toml (default: the §6.1 manifest)."""
    folder = base / name
    (folder / "static").mkdir(parents=True, exist_ok=True)
    (folder / "static" / "icon.ico").write_bytes(ICO_BYTES)
    (folder / "static" / "emiya.css").write_text("body {}\n", encoding="utf-8")
    raw = SPEC_TOML if content is None else content
    (folder / "theme.toml").write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
    return folder


@pytest.fixture
def spec_manifest(tmp_path: Path) -> mf.ThemeManifest:
    return mf.load_manifest(_theme_folder(tmp_path))


def test_spec_manifest_loads(spec_manifest: mf.ThemeManifest, tmp_path: Path) -> None:
    m = spec_manifest
    folder = (tmp_path / "emiya").resolve()
    assert (m.slug, m.name, m.contract, m.dark) == ("emiya", "Emiya", (1, 0), True)
    assert (m.css, m.fps_cap) == (("emiya.css",), 60)
    assert m.window == mf.WindowSpec(title="Emiya Voice", frameless=True, size=(1280, 800), min_size=(1280, 800),
                                     resizable=False, background_color="#0D0E10", text_select=False, center=True,
                                     icon=folder / "static" / "icon.ico")
    assert dict(m.palette) == VALID["palette"]
    assert dict(m.prefs) == VALID["prefs"]
    assert (m.folder, m.static_dir, m.static_url) == (folder, folder / "static", "/theme/emiya")
    assert mf.load_manifest(str(tmp_path / "emiya")) == m


def test_window_args_are_exact(spec_manifest: mf.ThemeManifest) -> None:
    args = mf.window_args(spec_manifest)
    assert args == {"resizable": False, "min_size": (1280, 800), "background_color": "#0D0E10",
                    "text_select": False, "easy_drag": False}
    assert pickle.loads(pickle.dumps(args)) == args  # NiceGUI pickles them for the window process


def test_native_run_kwargs_are_exact_and_loopback(spec_manifest: mf.ThemeManifest) -> None:
    kwargs = mf.run_kwargs(spec_manifest)
    assert kwargs == {"native": True, "reload": False, "frameless": True, "window_size": (1280, 800),
                      "dark": True, "title": "Emiya Voice", "favicon": str(spec_manifest.window.icon),
                      "show_welcome_message": False, "reconnect_timeout": 10.0}
    assert Path(kwargs["favicon"]).is_absolute()
    assert kwargs.get("host", "127.0.0.1") == "127.0.0.1"  # native mode binds loopback unless told otherwise


def test_browser_run_kwargs_bind_loopback_only(spec_manifest: mf.ThemeManifest) -> None:
    kwargs = mf.run_kwargs(spec_manifest, browser=True)
    assert kwargs["host"] == "127.0.0.1"
    assert set(kwargs) == {"native", "reload", "show", "host", "port", "dark", "title", "favicon"}
    assert (kwargs["native"], kwargs["reload"], kwargs["show"], kwargs["dark"]) == (False, False, True, True)
    assert 8080 <= kwargs["port"] <= 8999
    assert "frameless" not in kwargs and "window_size" not in kwargs  # either would switch native mode on


def test_browser_port_skips_a_port_in_use(spec_manifest: mf.ThemeManifest) -> None:
    first = mf.run_kwargs(spec_manifest, browser=True)["port"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", first))
        holder.listen()
        assert mf.run_kwargs(spec_manifest, browser=True)["port"] != first


def test_real_theme_manifest_runs_on_loopback() -> None:
    folder = ROOT / "ui" / "themes" / "emiya"
    if not (folder / "theme.toml").is_file():
        pytest.skip("ui\\themes\\emiya\\theme.toml is T0's and not built yet")
    m = mf.load_manifest(folder)
    assert mf.run_kwargs(m, browser=True)["host"] == "127.0.0.1"
    assert mf.run_kwargs(m).get("host", "127.0.0.1") == "127.0.0.1"


def _valid_text() -> str:
    return _toml(VALID)


def _replace(old: str, new: str) -> bytes:
    text = _valid_text()
    assert old in text
    return text.replace(old, new, 1).encode("utf-8")


# The theme_boot spike's 21 hostile ui_settings.json inputs (verify\vharness.py test_unit), each rewritten as a
# theme.toml. "ok" cases are the spike's benign controls (a BOM, extra keys); everything else must be refused.
HOSTILE: dict[str, tuple[Callable[[], bytes] | None, str]] = {
    "bom": (lambda: b"\xef\xbb\xbf" + _valid_text().encode("utf-8"), "ok"),
    "utf16_le_bom": (lambda: _valid_text().encode("utf-16"), "error"),
    "empty_file": (lambda: b"", "error"),
    "whitespace": (lambda: b"   \n", "error"),
    "toml_array": (lambda: b'["emiya"]', "error"),
    "toml_string": (lambda: b'"emiya"', "error"),
    "bare_null": (lambda: b"null", "error"),
    "slug_empty": (lambda: _replace('slug = "emiya"', 'slug = ""'), "error"),
    "slug_number": (lambda: _replace('slug = "emiya"', "slug = 5"), "error"),
    "slug_list": (lambda: _replace('slug = "emiya"', 'slug = ["emiya"]'), "error"),
    "uppercase": (lambda: _replace('slug = "emiya"', 'slug = "EMIYA"'), "error"),
    "padded": (lambda: _replace('slug = "emiya"', 'slug = " emiya "'), "error"),
    "traversal": (lambda: _replace('slug = "emiya"', 'slug = "../emiya"'), "error"),
    "dup_keys": (lambda: _replace('slug = "emiya"', 'slug = "emiya"\nslug = "neon"'), "error"),
    "nan_value": (lambda: _replace("fps_cap = 60", "fps_cap = nan"), "error"),
    "invalid_utf8": (lambda: _replace('name = "Emiya"', 'name = "Em\x00iya"').replace(b"\x00", b"\xff"), "error"),
    "nested_ok": (lambda: (_valid_text() + '\n[extra]\nx = [1, 2]\n').encode("utf-8"), "ok"),
    "trailing_comma": (lambda: _replace('slug = "emiya"', 'slug = "emiya"\nbad = {a = 1,}'), "error"),
    "huge_padding": (lambda: _valid_text().encode("utf-8") + b" " * 5_000_000, "error"),
    "init_py_is_not_theme": (lambda: _replace('slug = "emiya"', 'slug = "__init__"'), "error"),
    "settings_is_a_directory": (None, "error"),
}


def _snapshot(path: Path) -> tuple[Any, ...]:
    info = path.stat()
    return (path.is_dir(), info.st_mtime_ns, None if path.is_dir() else path.read_bytes())


def test_hostile_set_has_the_spike_size() -> None:
    assert len(HOSTILE) == 21


@pytest.mark.parametrize("case", list(HOSTILE))
def test_hostile_manifest_inputs(case: str, tmp_path: Path) -> None:
    """Each input yields a manifest or a ManifestError (never another exception) and never touches the file."""
    make, outcome = HOSTILE[case]
    folder = _theme_folder(tmp_path)
    toml = folder / "theme.toml"
    if make is None:
        toml.unlink()
        toml.mkdir()
    else:
        toml.write_bytes(make())
    before = _snapshot(toml)
    if outcome == "ok":
        assert mf.load_manifest(folder).window.title == "Emiya Voice"
    else:
        with pytest.raises(mf.ManifestError):
            mf.load_manifest(folder)
    assert _snapshot(toml) == before


def _mutated(change: Callable[[dict[str, Any]], None]) -> str:
    data = copy.deepcopy(VALID)
    change(data)
    return _toml(data)


def _set(section: str | None, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        (data if section is None else data[section])[key] = value
    return change


RULE_CASES: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "slug_mismatch": (_set(None, "slug", "forge"), "error"),
    "name_empty": (_set(None, "name", " "), "error"),
    "contract_major": (_set(None, "contract", [2, 0]), "error"),
    "contract_minor_newer": (_set(None, "contract", [1, 3]), "ok"),
    "contract_short": (_set(None, "contract", [1]), "error"),
    "contract_text": (_set(None, "contract", "1.0"), "error"),
    "contract_bool": (_set(None, "contract", [True, 0]), "error"),
    "dark_text": (_set(None, "dark", "yes"), "error"),
    "dark_auto": (_set(None, "dark", "auto"), "ok"),
    "css_empty": (_set(None, "css", []), "error"),
    "css_missing": (_set(None, "css", ["missing.css"]), "error"),
    "css_not_css": (_set(None, "css", ["notes.txt"]), "error"),
    "css_traversal": (_set(None, "css", ["../outside.css"]), "error"),
    "css_absolute": (_set(None, "css", ["C:/Windows/emiya.css"]), "error"),
    "css_duplicate": (_set(None, "css", ["emiya.css", "emiya.css"]), "error"),
    "window_missing": (lambda data: data.pop("window"), "error"),
    "window_not_table": (_set(None, "window", "Emiya Voice"), "error"),
    "title_blank": (_set("window", "title", "  "), "error"),
    "frameless_text": (_set("window", "frameless", "true"), "error"),
    "center_missing": (lambda data: data["window"].pop("center"), "error"),
    "size_bool": (_set("window", "size", [True, 800]), "error"),
    "size_three": (_set("window", "size", [1280, 800, 1]), "error"),
    "size_zero": (_set("window", "size", [0, 800]), "error"),
    "size_float": (_set("window", "size", [1280.0, 800]), "error"),
    "size_huge": (_set("window", "size", [100_000, 800]), "error"),
    "min_size_over": (_set("window", "min_size", [1300, 800]), "error"),
    "background_named": (_set("window", "background_color", "black"), "error"),
    "background_short": (_set("window", "background_color", "#FFF"), "error"),
    "background_lower": (_set("window", "background_color", "#0d0e10"), "ok"),
    "icon_missing": (_set("window", "icon", "static/none.ico"), "error"),
    "icon_not_ico": (_set("window", "icon", "static/emiya.css"), "error"),
    "icon_traversal": (_set("window", "icon", "../icon.ico"), "error"),
    "icon_absolute": (_set("window", "icon", "C:/Windows/icon.ico"), "error"),
    "palette_value": (lambda data: data["palette"].update(primary="gold"), "error"),
    "palette_key": (lambda data: data["palette"].update(sparkle="#FFFFFF"), "error"),
    "palette_custom": (lambda data: data["palette"].update(emiya_glow="#FFFFFF"), "ok"),
    "palette_not_table": (_set(None, "palette", "gold"), "error"),
    "palette_absent": (lambda data: data.pop("palette"), "ok"),
    "fps_negative": (_set("fx", "fps_cap", -1), "error"),
    "fps_bool": (_set("fx", "fps_cap", True), "error"),
    "fps_float": (_set("fx", "fps_cap", 60.5), "error"),
    "fps_too_high": (_set("fx", "fps_cap", 1000), "error"),
    "fx_absent": (lambda data: data.pop("fx"), "ok"),
    "prefs_float": (_set("prefs", "fx_speed", 1.5), "error"),
    "prefs_table": (_set("prefs", "fx_on", {"a": 1}), "error"),
    "prefs_list": (_set("prefs", "fx_on", [1]), "error"),
    "unknown_key_ignored": (_set(None, "description", "Dark forge"), "ok"),
}


@pytest.mark.parametrize("case", list(RULE_CASES))
def test_manifest_rules(case: str, tmp_path: Path) -> None:
    change, outcome = RULE_CASES[case]
    folder = _theme_folder(tmp_path, _mutated(change))
    (folder / "static" / "notes.txt").write_text("x", encoding="utf-8")
    (folder / "outside.css").write_text("x", encoding="utf-8")
    (tmp_path / "icon.ico").write_bytes(ICO_BYTES)
    if outcome == "ok":
        mf.load_manifest(folder)
    else:
        with pytest.raises(mf.ManifestError):
            mf.load_manifest(folder)


def _plain_lower_case(data: dict[str, Any]) -> None:
    del data["fx"], data["palette"]
    data["window"]["background_color"] = "#0d0e10"


def test_manifest_normalises_and_defaults(tmp_path: Path) -> None:
    m = mf.load_manifest(_theme_folder(tmp_path, _mutated(_plain_lower_case)))
    assert (m.window.background_color, m.fps_cap, dict(m.palette)) == ("#0D0E10", mf.DEFAULT_FPS_CAP, {})
    auto = mf.load_manifest(_theme_folder(tmp_path / "auto", _mutated(_set(None, "dark", "auto"))))
    assert auto.dark is None and mf.run_kwargs(auto)["dark"] is None


@pytest.mark.parametrize("line", ['# see https://example.com/theme', 'name = "Emiya http://x"'])
def test_manifest_refuses_urls(line: str, tmp_path: Path) -> None:
    text = _valid_text().replace('name = "Emiya"', line + '\nname = "Emiya"' if line.startswith("#") else line)
    with pytest.raises(mf.ManifestError, match="URL"):
        mf.load_manifest(_theme_folder(tmp_path, text))


def test_manifest_folder_problems(tmp_path: Path) -> None:
    with pytest.raises(mf.ManifestError, match="not found"):
        mf.load_manifest(tmp_path / "missing")
    other = _theme_folder(tmp_path, name="other")
    with pytest.raises(mf.ManifestError, match="folder name"):
        mf.load_manifest(other)
    with pytest.raises(mf.ManifestError, match="KiB"):
        mf.load_manifest(_theme_folder(tmp_path / "big", _valid_text() + "#" * mf.MAX_MANIFEST_BYTES))


# ---- the test package layout ---------------------------------------------------------------------

ENGINE_PROBE = """\
import json, sys
from pathlib import Path
from _pytest.pathlib import import_path, resolve_pkg_root_and_module_name
root = Path(sys.argv[1])
package = root / "tests" / "engine" / "__init__.py"
pkg_root, name = resolve_pkg_root_and_module_name(package)
module = import_path(package, mode="prepend", root=root, consider_namespace_packages=False)
import engine
print(json.dumps({"pkg_root": str(pkg_root), "name": name, "module": module.__name__, "engine": engine.__file__}))
"""


def _probe_engine(root: Path, cwd: Path) -> dict[str, str]:
    """Import tests\\engine exactly as pytest's default (prepend) mode does, then import engine."""
    cwd.mkdir(parents=True, exist_ok=True)
    return json.loads(_run_python(ENGINE_PROBE, str(root), cwd=cwd))


def test_pytest_imports_tests_engine_as_a_subpackage(tmp_path: Path) -> None:
    """`python -m pytest tests\\engine` resolves `import engine` to the app's package, not the test folder."""
    found = _probe_engine(ROOT, tmp_path / "cwd")
    assert (Path(found["pkg_root"]), found["name"], found["module"]) == (ROOT, "tests.engine", "tests.engine")
    assert Path(found["engine"]) == ROOT / "engine" / "__init__.py"


def test_engine_probe_detects_shadowing(tmp_path: Path) -> None:
    """Without tests\\__init__.py the same probe sees the test folder shadow the app's engine."""
    app = tmp_path / "app"
    for folder in (app / "engine", app / "tests" / "engine"):
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("", encoding="utf-8")
    shadowed = _probe_engine(app, tmp_path / "cwd")
    assert (shadowed["module"], Path(shadowed["engine"])) == ("engine", app / "tests" / "engine" / "__init__.py")
    (app / "tests" / "__init__.py").write_text("", encoding="utf-8")
    fixed = _probe_engine(app, tmp_path / "cwd")
    assert (fixed["module"], Path(fixed["engine"])) == ("tests.engine", app / "engine" / "__init__.py")


def test_engine_test_modules_resolve_under_tests() -> None:
    """Every module pytest collects from tests\\engine gets a tests.engine.* name."""
    from _pytest.pathlib import resolve_pkg_root_and_module_name

    folder = ROOT / "tests" / "engine"
    for path in [*sorted(folder.glob("*.py")), folder / "conftest.py", folder / "test_probe.py"]:
        pkg_root, name = resolve_pkg_root_and_module_name(path)
        assert (pkg_root, name) == (ROOT, f"tests.engine.{path.stem}".removesuffix(".__init__"))


def test_every_test_folder_is_a_package() -> None:
    """§8.1: a test folder without __init__.py imports as a top-level package and can shadow the app's."""
    tests = ROOT / "tests"
    folders = [tests, *(p for p in tests.rglob("*") if p.is_dir() and "__pycache__" not in p.parts)]
    missing = [str(f.relative_to(ROOT)) for f in folders if any(f.glob("*.py")) and not (f / "__init__.py").is_file()]
    assert missing == []
    assert not (ROOT / "__init__.py").exists()  # the app root itself must not become a package
