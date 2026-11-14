# Emiya Voice Tool

Emiya Voice Tool is a local, GPU-accelerated voice cloning app with a themeable desktop interface.
It creates a voice from a short reference clip and generates speech from typed text.
Type a message, pick a voice and hit Generate.

Built around [Chatterbox](https://github.com/resemble-ai/chatterbox) (Resemble AI,
MIT-licensed)

For the Windows one-folder distribution, open **Emiya Voice.exe** in the extracted folder.
It includes Python, CUDA-enabled PyTorch, and local model and cache directories.
The [portable guide](docs/portable.md) covers the folder layout, building a clean copy for a friend,
and [testing a complete conversation in Discord](docs/portable.md#test-a-complete-conversation).

## What belongs in Git

Use the project folder as the repository root. Commit the entry scripts, requirements, licenses, README,
`pytest.ini`, `.gitignore`, and the source in `engine/`, `ui/`, `chatterbox/`, `scripts/`, `tools/`,
`tests/` and `docs/`. The local Chatterbox tree contains changes used by the app and the portable builder.
The optional design mockups, virtual environment, build output and raw test evidence stay local.
Some links in the historical reliability reports refer to that local evidence.

For a new, empty GitHub repository, run the following in the source folder to preview the first commit.
If the GitHub repository already has commits, use a clone of that repository as the starting point.

```powershell
git init -b main
git add --dry-run .
```

After reviewing the list, stage and commit the source. For a new GitHub repository, paste its
HTTPS or SSH URL when prompted.

```powershell
git add .
git commit -m "Add Emiya Voice source"
git remote add origin (Read-Host "GitHub repository URL")
git push -u origin main
```

The portable copy under `dist/` can contain reference recordings, registered voices, take WAVs,
scripts spoken in those takes and personal settings. Use the builder's fresh destination for a friend.
If publishing that build through GitHub Releases, each archive part must be smaller than 2 GiB.
[Clean build instructions](docs/portable.md#verify-or-rebuild) · [GitHub release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)

---

## Responsible use

This tool clones a real, identifiable person's voice. A few ground rules worth
keeping in place no matter what you build on top of this:

- Only load reference clips from people who've actually agreed to have their
  voice cloned, and only for what they agreed to.
- Keep generated audio inside the loop of people who know it's synthetic. Don't
  send it to someone family, a partner, an employer, a stranger who'd
  reasonably believe they're hearing the real person, unless everyone involved
  is in on it.

---

## Architecture

```
      cli.py  /  app.py (HTTP)
              │
              ▼
   ┌───────────────────────────┐          ┌───────────────────────────┐
   │       VoiceEngine         │───────▶  │   cache/registry.json    │
   │    engine/engine.py       │          │   voice_id -> backend,    │
   └─────────────┬─────────────┘          │   reference_wav,          │
                 │ backend lookup         │   exaggeration, language  │
                 ▼                        └───────────────────────────┘
   ┌─────────────────────────────────┐
   │ ChatterboxMultilingualBackend   │
   │ engine/backends/multilingual.py │
   │ one Lock guards the shared      │
   │ model.conds across requests     │
   └─────────────────────────────────┘
```

The engine runs as its own persistent process (FastAPI server, or the CLI for
one-off calls) so the model only loads once.

- `load_voice(voice_id, reference_wav, **params) -> voice_id` — validates
  `voice_id`, runs `prepare_conditionals()` once (or reuses a cache hit), and
  records the result in both `cache/{backend}/{voice_id}.pt` and the registry.
- `speak(text, voice_id, **params) -> wav bytes` — serialized behind a
  per-backend lock, so two concurrent requests for two different voices can't
  interleave and cross-talk.
- `list_voices() -> list[str]` — reads from the registry, not a directory
  glob, so a `.pt` with no matching registry entry won't show up (see
  **Status** below for what that means if you have pre-existing cache files).

`voice_id` is restricted to letters, digits, `_` and `-`
(`engine/backends/base.py`), since it becomes a filename on disk — this blocks
a path-traversal `voice_id` like `../../etc/passwd`.

Only `ChatterboxMultilingualBackend` exists today. The interface is written so
a second backend (Turbo, for lower latency — see Phase 6) is a small addition
later, not a rewrite.

### Project structure

```
voice-tool/
├── venv/
├── engine/
│   ├── __init__.py
│   ├── engine.py            # VoiceEngine — backend-agnostic
│   ├── registry.py          # persists voice -> backend/config to cache/registry.json
│   └── backends/
│       ├── __init__.py
│       ├── base.py          # abstract interface + validate_voice_id()
│       └── multilingual.py  # ChatterboxMultilingualTTS wrapper, thread-safe
├── scripts/                 # one-off dev/check scripts — not part of the app
│   ├── check_gpu.py         # verifies CUDA is visible to torch
│   ├── check_model.py       # verifies the model loads
│   └── test_roundtrip.py    # cold/warm cache smoke test
├── voices/
│   └── reference/           # raw reference clips, e.g. friend.wav
├── cache/                   # cached .pt conditionals + registry.json
├── outputs/                 # generated .wav files
├── app.py                   # FastAPI server
├── cli.py                   # terminal interface (load / speak / list / info)
├── config.py
├── requirements.txt         # pinned runtime packages
└── requirements-dev.txt     # + pytest tools
```

---

## Requirements

- Python 3.11 (Chatterbox is developed and tested on 3.11; `>=3.10` is the
  hard floor per its `pyproject.toml`)
- NVIDIA GPU with a CUDA-capable driver (Chatterbox Multilingual needs roughly 2–4 GB)
- `ffmpeg` and `git` on PATH
- On Linux, `libsndfile1` and standard build tools (`build-essential` on
  Debian/Ubuntu) for audio I/O libraries

---

## Install steps

**For a Windows checkout of this repository**, open PowerShell in the directory containing
`gui.py` and the included `chatterbox/`. With 64-bit Python 3.11 installed, create the development
environment using the pinned dependencies. Node.js is also used by the visual parity tests.

```powershell
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.6.0+cu124 torchaudio==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -m pip check
```

Then go to [the tests](#its-tests). The manual instructions below describe how the local Chatterbox
source was originally prepared from upstream.

Chatterbox's own install has two known rough edges, installing in this order avoids both.

**1. Create the project and a Python 3.11 venv**

```bash
mkdir voice-tool && cd voice-tool

# Windows (PowerShell / CMD)
py -3.11 -m venv venv
venv\Scripts\activate

# Linux
python3.11 -m venv venv
source venv/bin/activate
```

**2. Upgrade the base toolchain**

```bash
python -m pip install --upgrade pip setuptools wheel
```

**3. Clone Chatterbox from source (to allow watermark removal)**

```bash
git clone https://github.com/resemble-ai/chatterbox.git
```

OR

Go to [Chatterbox](https://github.com/resemble-ai/chatterbox) 
Click Code to download the zip

Verify that the extracted folder contains the src directory and pyproject.toml.

Then extract its contents into your voice-tool folder,
and finally run

```bash
cd chatterbox
```

**4. Strip out the PerTh watermark mechanism and Git dependency**

Choose the command block matching your OS before installing:

*On Windows (Using PowerShell)*

```powershell
# Remove watermark lines
Get-ChildItem -Path src/chatterbox/*.py | ForEach-Object { (Get-Content $_.FullName) -replace 'torch\.from_numpy\(watermarked_wav\)', 'torch.from_numpy(wav)' | Set-Content $_.FullName }

# Remove Git dependency requirement for Perth
(Get-Content pyproject.toml) | Where-Object { $_ -notmatch 'resemble-perth @ git' } | Set-Content pyproject.toml

# Remove gradio: the app's UI is NiceGUI, and gradio 6.8.0 caps starlette below what NiceGUI needs
(Get-Content pyproject.toml) | Where-Object { $_ -notmatch '"gradio==' } | Set-Content pyproject.toml
```

*On Linux (Using sed)*

```bash
# Remove watermark lines
sed -i 's/torch.from_numpy(watermarked_wav)/torch.from_numpy(wav)/g' src/chatterbox/*.py

# Remove Git dependency requirement for Perth
sed -i '/resemble-perth @ git/d' pyproject.toml

# Remove gradio: the app's UI is NiceGUI, and gradio 6.8.0 caps starlette below what NiceGUI needs
sed -i '/"gradio==/d' pyproject.toml
```

**Verify Modifications**

*On Windows (PowerShell):*

```powershell
# Verify watermarking is commented out (should show lines with #)
Select-String -Path src/chatterbox/*.py -Pattern "apply_watermark"

# Verify Git dependency is removed (should return nothing)
Select-String -Path pyproject.toml -Pattern "resemble-perth @ git"

# Verify gradio is removed (should return nothing)
Select-String -Path pyproject.toml -Pattern '"gradio=='
```

*On Linux (Bash):*

```bash
# Verify watermarking is commented out (should show lines with #)
grep "apply_watermark" src/chatterbox/*.py

# Verify Git dependency is removed (should return nothing)
grep "resemble-perth @ git" pyproject.toml

# Verify gradio is removed (should return nothing)
grep '"gradio==' pyproject.toml
```

**Expected Output for Watermarks:**

```powershell
src\chatterbox\mtl_tts.py:354:            # watermarked_wav = self.watermarker.apply_watermark(wav, sample_rate=self.sr)
src\chatterbox\tts.py:271:            # # watermarked_wav = self.watermarker.apply_watermark(wav, sample_rate=self.sr)
src\chatterbox\tts_turbo.py:319:        # # watermarked_wav = self.watermarker.apply_watermark(wav, sample_rate=self.sr)
src\chatterbox\vc.py:103:            # watermarked_wav = self.watermarker.apply_watermark(wav, sample_rate=self.sr)
```

**5. Install dependencies and Chatterbox in editable mode**

```bash
# Install Perth first (prevents it from upgrading NumPy to 2.x later)
pip install https://github.com/resemble-ai/Perth/archive/refs/heads/master.zip

# Pin NumPy 1.x and PyTorch CUDA 12.4 builds
pip install "numpy>=1.24,<2.0"
pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124

# Install Chatterbox locally
pip install -e .
```

**6. Re-pin the CUDA build of torch (prevents pip from falling back to CPU wheels)**

```bash
pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124 --force-reinstall --no-deps
cd ../   # Return to your main voice-tool project root
```

**6b. Install the desktop UI (NiceGUI) and the test tools**

From the project root:

```bash
pip install "nicegui[native]==3.19.0" send2trash==2.2.0
pip install pytest==9.2.0 pytest-asyncio==1.4.1.post1   # tests only
pip check
```

Keep NiceGUI on 3.19: 4.0 has breaking changes. `pip check` should report no
broken requirements. `requirements.txt` is the full pinned set of runtime
packages, and `requirements-dev.txt` adds the test tools.

**7. Verify GPU access**

`scripts/check_gpu.py`:

```python
import torch

print(f"PyTorch version: {torch.__version__}")
print(f"CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
```

Run with `python scripts/check_gpu.py`. If `CUDA available` prints `False`,
stop and fix the torch install before continuing, everything after this
depends on it.

**8. First model load**

`scripts/check_model.py`:

```python
import time
from chatterbox.mtl_tts import ChatterboxMultilingualTTS

print("Loading Chatterbox Multilingual...")
start = time.time()
model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
print(f"Loaded in {time.time() - start:.1f}s")
```

Run with `python scripts/check_model.py`. The first run downloads the model
weights (a few GB, one-time). Expect 30–90 seconds for the first load and
10–20 seconds on subsequent ones.

---

## Preparing the reference clip

- **6–15 seconds**, mono WAV, 22.05 kHz or higher
- No background noise, no clipping, consistent conversational tone
- If extracting from an existing recording: isolate the section in Audacity,
  apply noise reduction if there's hum, normalize to about -3 dB peak, export
  as mono WAV

Save it as `voices/reference/friend.wav`. This step is backend-agnostic,
it stays the same regardless of which Chatterbox variant you use.

---

## Usage

The engine is usable two ways right now: `cli.py` for the terminal, and
`app.py` (FastAPI) for anything that talks HTTP. The Phase 5 desktop app is
built with NiceGUI. Both sit on top of the same `VoiceEngine`, so a voice
registered through one is immediately visible to the other — they share
`cache/registry.json`.

### CLI

```bash
# Register a voice from a reference clip
python cli.py load friend voices/reference/friend.wav

# Generate speech
python cli.py speak friend "Hey, it's me, just testing this out." --out outputs/test.wav

# List registered voices
python cli.py list

# Inspect what's stored for a voice
python cli.py info friend
```

Re-registering an existing `voice_id` is a cache hit by default — it reloads
the saved conditionals without reprocessing the reference clip. To swap in a
new reference clip or a different `exaggeration` for a voice you've already
registered, pass `--force`:

```bash
python cli.py load friend voices/reference/friend_v2.wav --exaggeration 0.7 --force
```

`--exaggeration` only matters at load time (it's baked into the cached
conditionals). `--cfg-weight` only matters at speak time, and can differ
generation to generation. `--language` sets the default; `speak` can override
it per call.

### HTTP API

```bash
uvicorn app:app --reload
```

| Method | Path | Body / params | Returns |
|---|---|---|---|
| `POST` | `/load_voice` | `voice_id`, `reference_wav`, `exaggeration`, `language`, `force` | `{"voice_id", "status"}` |
| `POST` | `/speak` | `voice_id`, `text`, `language_id`, `cfg_weight` | raw WAV bytes |
| `GET` | `/voices` | — | `{"voices": [...]}` |
| `GET` | `/voices/{voice_id}` | — | stored backend / reference clip / exaggeration / language |

### What gets stored per voice

Each registered voice has a conditioning file at `cache/{backend}/{voice_id}.pt`
and an entry in `cache/registry.json` that records its backend, reference clip,
expressiveness and default language. The Voices list reads the registry.
To add a loose reference WAV, choose it in **Forge** and commit the voice,
or run `python cli.py load <name> <reference.wav>` from the installation you use.

---

## Desktop app (gui.py)

Open **Emiya Voice.exe** for the full desktop app, including Speak and Discord. Sending starts off.
Use **Settings → Voice Chat** to choose an output and **Settings → Keys** to assign the optional global
Send and Stop Sending shortcuts. [Portable setup](docs/portable.md) · [Speak and Discord](docs/speak/README.md)

The desktop app is a NiceGUI window (pywebview + WebView2) with six pages: Speak, Voices, Forge, Takes,
Models and Settings. It sits on the same `VoiceEngine` as `cli.py` and `app.py`, and they share
`cache\registry.json` when run from the same installation. The portable copy keeps its own library
beside the executable. Forge adds a reference clip to that library when you commit the voice.

It never loads a model on its own. A model loads only when:
- you click a control that names it: "Switch to Turbo & Generate", a Models card, Retry, "Load Multilingual";
- you confirm the switch popover;
- Warm Up on Launch is set to Last Model, which loads that model once at startup.

Auto-unload After Idle only ever frees VRAM. At most one model is in VRAM; while one program (gui.py, cli.py or
app.py) holds a model, the others get "The voice engine is in use by …" until it frees VRAM.

### Run it

```powershell
# from the install folder (gui.py finds its own folder, so any working folder works)
venv\Scripts\pythonw.exe gui.py      # the app, no console window; logs go to logs\gui.log
venv\Scripts\python.exe  gui.py      # the same app with a console; logs print there
```

For a desktop shortcut, set:
- Target: `<install>\venv\Scripts\pythonw.exe gui.py`
- Start in: the install folder

**What to expect:**
- The window opens on Speak with no model loaded.
- **Closing:**
  - The window's Close button ("Close · frees VRAM") quits and frees VRAM.
  - If a take, forge or model switch is running, the first click arms it ("Click Again to Quit"). A second
    click within 3 s quits.
- **Starting it twice:** a second launch brings the running window to the front and exits (exit code 5).

### Dev flags

| Flag | What it does |
|---|---|
| `--fake[=SCENARIO]` | FakeEngine instead of the real engine: no torch, no model, no GPU.<br>Seeds the scenario's data into `.dev\` (wiped and reseeded at every `--fake` launch) and opens on the scenario's page.<br>Default scenario: `speak`. An unknown name stops with the list of scenarios.<br>Uses its own instance lock (`cache\gui-dev.lock`), so it can run next to the real app. |
| `--data-root=PATH` | Keeps the data (registry, `.pt` cache, clips, takes, load history, settings) in PATH instead of the install folder.<br>Works with the real engine and with `--fake` (for `--fake`, an empty folder). Logs and locks stay in the install folder. |
| `--browser` | Serves the UI in a browser tab instead of the app window, on 127.0.0.1 only (the first free port from 8080; the address is printed).<br>In a tab: picking a clip uploads it, and Export, folder pickers and Minimize need the app window. |
| `--no-show` | With `--browser`: don't open a tab. |
| `--fx-telemetry` | Log the background effects' frame telemetry (logger `voicetool.fx`). |
| `--debug-webview` | Open the WebView2 developer tools. |
| `--smoke[=PLAN]` | Test-window mode for the native smoke harness.<br>The window opens on a free secondary screen without taking the focus, or the run is skipped with exit code 8.<br>`VOICETOOL_TEST_WINDOW=1` does the same, and `VOICETOOL_TEST_SCREEN` (`\\.\DISPLAYn` or a 1-based index) picks the screen. Normal runs never need this. |

```powershell
venv\Scripts\python.exe gui.py --fake                         # the Speak board
venv\Scripts\python.exe gui.py --fake=firstrun                # first-run state
venv\Scripts\python.exe gui.py --fake=generating --browser    # in a browser tab
venv\Scripts\python.exe gui.py --fake --data-root=D:\scratch\vt-fake    # FakeEngine, nothing written to .dev
venv\Scripts\python.exe gui.py --data-root=D:\scratch\vt      # real engine on scratch data
```

Scenarios:
- speak, switch, lowvram, failure
- forge, firstrun, voices, takes, models, settings
- other_backend, loading, generating, queued, forging, freeing, relaunch
- forge_ready, forge_other_backend, forge_low_vram, forge_replace, forge_replace_other_backend,
  forge_taken_other, forge_turbo_short, forged, forge_failed
- voices_missing_files, voices_unconfirmed, cpu

### Where things live

The data root is the install folder. `--data-root` replaces it, and `--fake` uses `.dev\` instead.

| Path (in the data root) | What |
|---|---|
| `cache\registry.json` | the voice registry, shared with cli.py and app.py |
| `cache\<backend>\<voice>.pt` | each voice's baked conditioning |
| `cache\load_history.jsonl` | model loads and unloads (Models › Load History, the load-time estimates) |
| `cache\staging\` | clips picked in Forge before they are forged (swept after 24 h) |
| `voices\reference\` | reference clips |
| `outputs\takes\` | takes as WAV files, and `index.json` with the stars. Settings › Output Folder moves new takes; the index stays here. |
| `ui_settings.json` | the app's settings |

| Path (always in the install folder) | What |
|---|---|
| `logs\gui.log`, `logs\gui-window.log` | logs under pythonw (gui.log rotates at 10 MB) |
| `cache\gui.lock` (`cache\gui-dev.lock` for `--fake`) | one app at a time |
| `cache\engine.lock`, `cache\engine.owner.json` | held by whichever program has a model in VRAM (in the app: Free VRAM releases it) |
| `.nicegui\` (`.nicegui-dev\` for `--fake`) | NiceGUI's storage folder; the app itself writes nothing there |

Offline Mode (Settings › General, on by default) sets `HF_HUB_OFFLINE` before anything loads. Changing it
takes effect after a restart. A damaged `ui_settings.json`, `registry.json`, takes `index.json` or
`load_history.jsonl` never stops the app: the damaged file is moved aside (`<name>.corrupt-<unix>`; the
settings at their first save) or its unreadable lines are skipped (the load history). A damaged registry or
takes list also shows a red toast; the WAVs and `.pt` files are never touched.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | normal |
| 1 | crashed (a box points at `logs\gui.log`) |
| 5 | already running |
| 7 | the theme's `theme.toml` is broken |
| 8 | a test window found no free screen and was skipped |

### Its tests

Run from the development checkout containing `tests/` and `venv/`. A portable user can instead
follow the [Discord and gaming checks](docs/portable.md#test-a-complete-conversation) with the shipped app.

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/pages/test_speak_calls.py  # quick Speak/Discord check with simulated audio
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider            # all default tests, no windows
.\venv\Scripts\python.exe -m pytest -q -m slow                         # offline checks that import model libraries
.\venv\Scripts\python.exe tools/verify_portable_window.py ".\dist\Emiya Voice"  # packaged UI on a private desktop
```

For the extended reliability suites, close games, free the app's model and run each command separately.
The GPU suite needs the models cached for the development checkout and a reference clip you may use at
`voices\reference\sample.wav`. The native harness uses a private desktop and skips when its screen
requirements are unmet. `-rs` prints the reason for skipped tests.

```powershell
$env:VOICETOOL_TEST_BLOCK_PROCESSES = 'destiny2.exe,destiny2launcher.exe'
.\venv\Scripts\python.exe -m pytest -q -rs -m "soak and not gpu and not native and not bench"
.\venv\Scripts\python.exe -m pytest -q -rs -m bench
.\venv\Scripts\python.exe -m pytest -q -rs -m native
.\venv\Scripts\python.exe -m pytest -q -rs -m gpu
```

---

## Generation parameters

| Parameter | Range | Effect |
|---|---|---|
| `exaggeration` | 0.0–1.0+ (default 0.5) | Higher = more expressive/faster speech; only applied when a voice is registered (or re-registered with `--force`), not per-generation |
| `cfg_weight` | 0.0–1.0 (default 0.5) | Lower = slower, more deliberate pacing; useful to offset a high-exaggeration voice |
| `language_id` / `language` | e.g. `"en"`, `"ja"` | Must match the text's language |

Chatterbox also exposes lower-level sampling controls (`temperature`,
`top_p`, `top_k`, `repetition_penalty`) inherited from the underlying
generation model — check `help(model.generate)` on your installed version,
since these shift between releases more than the parameters above.

---

## Status / next steps

- [x] Phase 0 — env validated, GPU confirmed, model loads
- [x] Phase 1 — clean reference clip saved
- [x] Phase 2 — raw generation test — superseded by `cli.py speak`, no
      standalone script needed anymore
- [x] Phase 3 — `.pt` caching working, confirmed as a real cache hit (not
      just a fast-looking timing) by tracing the code path to the
      `Conditionals.load()` branch
- [x] Phase 4 — `VoiceEngine`, backend-agnostic, and hardened:
  - `cache/registry.json` persists voice → backend/config so it survives a
    restart (previously in-memory only — a real bug once a second backend
    exists, fixed now while it was cheap to fix)
  - a per-backend lock prevents concurrent requests for different voices
    from cross-talking (reproduced against the old code: ~70% of concurrent
    calls spoke in the wrong voice; 0% after the fix)
  - `voice_id` is validated against path traversal
  - `force=True` re-bakes a voice's conditionals without deleting the `.pt`
    by hand
- [ ] Phase 5 — front end (voice picker + text box)
- [ ] Phase 6 — polish: Turbo backend, streaming, UI-level error handling

---

## License notes

Chatterbox itself is MIT-licensed. That covers the code and model weights —
it says nothing about the right to use a specific person's likeness, which is
the consent question above, not a licensing one.
