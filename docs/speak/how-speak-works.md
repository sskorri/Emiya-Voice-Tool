# How Speak works today

This page describes the code as of the afternoon of 2026-11-13, checked after another session's edits
to `engine/` and `ui/` that morning. Nothing was changed to write it. Code is named by file and
function, not line number, because it is still changing. Decisions are in [decisions.md](decisions.md),
the steps in [implementation-plan.md](implementation-plan.md), the risks in
[failure-modes.md](failure-modes.md).

## What "speak" means

The definition this documentation works from (the owner's words, 2026-11-13):

> Use the voices you already have or create, and speak out what you typed. Nothing live. If the voice
> does not exist yet, add it (with permission, unless it is your own voice). Then type what you want
> said, and the tool speaks it in the expression, voice and cadence you tuned that voice to.

Later that day the owner added (decisions.md, section 2):

- **Replay saved takes exactly as they are**, so a line need not be typed again, also into a call.
- **Rework a take**: change what its model allows and generate again. A voice's model stays fixed.
- **Speech into voice calls** (Discord, game chat, recordings), through VB-CABLE, a virtual audio cable
  the owner installs. The call hears the tool only.
- **One consent switch**: "This is my own voice, or the speaker agreed to having it cloned".

Most of the original definition is built and tested against the fake engine (`ui/core/fake.py`). It has
**not** yet been run end to end on the real models in the window (plan item 0.3).

## The path of one take

Two terms first. **Expressiveness** is Chatterbox's `exaggeration`: how much emotion goes into a line.
**Guidance** is `cfg_weight`, the pacing control: lower values give slower, more deliberate delivery.

| # | Step | Where | What happens |
|---|------|-------|--------------|
| 1 | Pick a voice | `SpeakController.pick_voice` in `ui/behaviours/speak.py` | Copies the voice's Expressiveness (and a Multilingual voice's language) into the draft and sets Guidance to 0.50. Loads nothing. "Speak with ‹voice›" (Voices, Forge, search) calls it too. At launch, `Runtime._launch_selection` in `ui/core/background.py` does the same for the newest take's voice. |
| 2 | Shape the delivery | `set_delivery`, `apply_preset`, `reset_delivery` (same file) | Expressiveness 0.25 to 2.00 and Guidance 0.00 to 1.00, on a 0.05 grid. Presets: Default (0.50 / 0.50), Slower (0.50 / 0.30), Dramatic (0.70 / 0.30). Turbo ignores both and reads inline tags. |
| 3 | Type the script | `set_text` (same file) | The stats line shows characters and sentences, plus a time estimate once three comparable takes exist. |
| 4 | Generate | `SpeakController.generate` | One frozen `GenerateRequest` per take: voice, model, text, language, Expressiveness and Guidance (both empty for Turbo). A queued take never reads the live draft. |
| 5 | Get the job slot | `JobRunner.start_generate` in `ui/core/jobs.py` | One job at a time; up to 3 takes wait behind it. While a take or a model switch (loading another model) runs, a take for another model is refused. |
| 6 | Load the model if needed | `TokenStore` in `ui/core/tokens.py`, `EngineFacade.load` in `ui/core/facade.py`, `VoiceEngine.make_resident` in `engine/engine.py` | Only with the single-use token the Generate button carries. Low VRAM turns the click into a question. The resident model is unloaded before the new one is built. |
| 7 | Speak | `EngineFacade.speak`, `VoiceEngine.speak`, the backend's `speak` | In a worker thread with `require_resident=True`, so a take never loads a model. The engine fills a missing language and Expressiveness from the voice, never Guidance. Output: a 32-bit float WAV, mono, 24 kHz. |
| 8 | Save | `JobRunner._save_take`, `TakesStore.add` in `ui/core/services/takes_store.py` | Writes `‹Output Folder›/‹voice›_take‹N›.wav`, then the index, so a crash leaves at most an unlisted WAV. Then sets `Session.take_id` and emits `take_ready`. |
| 9 | Play | `PlaybackController._on_take_ready` in `ui/behaviours/playback.py` | Always loads the new take into the client's one hidden `<audio>` (`ui/kit/audio.py`); plays it when **Autoplay New Takes** is on (the default). |

Playback runs in the window's WebView2 page (the Edge browser engine that draws the window), a separate
process from the server (NiceGUI's native mode spawns it), and always goes to the Windows default
output. Nothing can choose a device today: no code calls `setSinkId`, and the app's Python environment
(the venv) has no audio playback library.

In the app, a voice is added through Forge, which requires the switch "The speaker agreed to having this
voice cloned" (`JobRunner.start_forge` and `EngineFacade.bake` both refuse without it).
`VoiceEngine.load_voice` bakes the conditioning into `cache/‹model›/‹voice›.pt` and writes the registry
entry with the consent record. `cli.py load` and `app.py /load_voice` also add voices, with no consent
record; the app shows those as "Not confirmed · re-forge to confirm" (decisions.md, section 3).

## What is stored where

Paths are relative to the data root: the install folder, `.dev` inside it for a `--fake` run, or the
folder given with `--data-root` (`Paths.for_run` in `ui/core/paths.py`).

| Thing | File | What it holds |
|-------|------|---------------|
| A voice | `cache/registry.json` (`engine/registry.py`; shared with `cli.py` and `app.py` in a normal run) | `backend`, `reference_wav`, `exaggeration`, `language`, `created_at`, `forged_at`, and `meta` (`consent`, `consent_at`, `clip_seconds`, `clip_source`). A re-register builds a fresh entry and keeps `meta` only for the same model and clip (`_registered_entry`). |
| Its conditioning | `cache/‹model›/‹voice›.pt` | What the model needs to speak in that voice, baked once from the clip |
| The Speak draft | memory only (`Session` in `ui/contract/state.py`) | voice, text, language, Expressiveness, Guidance, current take, A/B pins (the two takes held for comparison). Survives a page reload, not a restart. |
| A take | the WAV in the Output Folder, plus `outputs/takes/index.json` (fixed place, version 1) | id, voice, model, language, text, Expressiveness, Guidance, length, generation time, tokens, "hit the limit" flag, created, session, star, WAV path, sample rate, waveform peaks. Unknown keys are ignored on read. One bad value in a known field of any take sets the whole index aside (`_parse_index`, `_read_index`). |
| Settings | `ui_settings.json` (`ui/core/services/settings.py`) | Output Folder (`outputs/takes`), Autoplay New Takes (on), Warm Up on Launch (off), Auto-unload After Idle (0 = never), Run Models On (cuda), and others. If the file is missing, empty or corrupt, the first save writes every setting (`SettingsStore._write`). |

Neither `ui_settings.json` nor `outputs/takes/index.json` exists on this PC yet (checked 2026-11-13).

Note what is **not** stored with a voice: Guidance. Expressiveness and language come back when you pick
the voice; Guidance lives only in the draft and in each take's record, and resets to 0.50 on every pick,
on Reset and at launch. `cli.py speak` and `app.py` send 0.50 unless told otherwise. Re-loading a voice
with `cli.py load` or `app.py /load_voice` resets its Expressiveness to the value passed: 0.50 unless
`--exaggeration` (or the request field) says otherwise.

## The three models

Sizes are the weight files in the local Hugging Face cache, read on 2026-11-13. The Chatterbox code is
the copy in `chatterbox/`: it reports version 0.1.7 but is a newer upstream snapshot (files dated
2026-09-15).

| Model | Weights on disk | Speaks | Delivery controls | Clip rule | Limits per take |
|-------|-----------------|--------|-------------------|-----------|-----------------|
| Standard | 3.19 GB (2.97 GiB) | English | Expressiveness, Guidance | none | 1,000 speech tokens, about 40 s; 2,048 text tokens |
| Multilingual | 3.21 GB (2.99 GiB) | 23 languages | Expressiveness, Guidance, language | none | the same |
| Turbo | 2.99 GB (2.78 GiB) | English | inline tags only (`[laugh]`, `[chuckle]`, `[cough]`, `[sigh]`, `[gasp]`) | longer than 5 s | 1,000 speech tokens; text cut at 1,024 tokens |

The limits come from the Chatterbox copy (`max_new_tokens=1000`, Turbo's `max_gen_len=1000`,
`max_text_tokens = 2048`) and Turbo's tokenizer (`model_max_length` 1,024, truncation on).

- **Expressiveness**: higher values add emotion and tend to speed the delivery up.
- **Guidance**: the closest thing Chatterbox has to a cadence control; there is no direct speed setting.
  Standard raises Guidance below 0.001 to 0.001, because 0 crashes that model (`MIN_CFG_WEIGHT` in
  `engine/backends/standard.py`).
- Everything else (`temperature`, `top_p`, `min_p`, `repetition_penalty`) runs at Chatterbox's
  defaults. The backends take only language, Expressiveness and Guidance, and drop other keywords.
  Temperature becomes a delivery knob later, Turbo included (decisions.md, section 3, Steadiness).

## The one-model rule

At most one model is in VRAM. Three independent layers enforce it:

1. **The engine.** `config.py` sets `MAX_RESIDENT_BACKENDS = 1`. `VoiceEngine` unloads the resident
   model before it builds the next, under one hold of its lock. The lock file `cache/engine.lock` keeps
   the GUI, `cli.py` and `app.py` from each holding a model.
2. **The job slot** (`JobRunner`): one job at a time (a switch, an unload, takes, a forge or a test
   line). Every start call answers at once: started, queued or refused.
3. **Switch tokens** (`TokenStore`): a button that would load a model carries a token for exactly that
   load. It works once, and only while nothing about the models, the job or the voice library changed.

## Where VRAM is given back

- On a switch: the old model is unloaded before the new one is built.
- **Free VRAM** on the dock (the status bar at the bottom of the window) and the Models page, a model
  card's Unload, and the Free VRAM entry in the search box.
- **Auto-unload After Idle** (`IdleUnloader` in `ui/core/background.py`, checked every 15 s), when set to
  10, 30 or 60 minutes. In today's code the default is 0, which means never. The idle count restarts at
  launch, when the setting changes, and when a job other than an unload ends. Typing does not count. A
  failed idle unload is retried at the next check.
- When a load fails, and when a take runs out of memory.
- On quit: `Runtime.stop` makes no engine call, NiceGUI ends the process hard (`os._exit`), and the
  driver returns everything.

## Limits a take has today

- A take is at most about 40 s of audio. A longer script is cut there and the take is marked "cut at
  40 s". The warning before generating appears only once three comparable takes exist.
- Nothing checks the script's length before it reaches the model; only an empty script is refused.
- The running take cannot be stopped. Cancel Queue drops only the takes waiting behind it.
  `JobRunner.cancel_running` exists, but only shutdown calls it.
- Every take is sampled afresh, with no seed. No seed will be built (decisions.md, section 3).

## Gaps, and where each is decided

Each row is a gap between today's code and the owner's goal, with the sections of
[decisions.md](decisions.md) and the items of [implementation-plan.md](implementation-plan.md).

| Gap in today's code | decisions.md | Plan |
|---------------------|--------------|------|
| Guidance is not saved per voice: it resets to 0.50 on pick, Reset and launch, and the engine never fills it. `cli.py load` and `app.py /load_voice` reset Expressiveness; `cli.py speak` and `app.py` send Guidance 0.50. | Sections 2 (decision 1), 3 (Delivery), 5.5 | 0.4 (you listen, 0.30 vs 0.50), then phase 2 (P1 to P6); 1.9 first if 0.5 confirms the slider jump (F14) |
| No Save to Voice. Only Voices edits a voice, and only its language and Expressiveness. | Sections 2 (decision 1), 5.5 | Phase 2 |
| Long scripts are cut at about 40 s. Nothing checks the length. Over 2,048 text tokens Standard and Multilingual fail inside the model (F1), which on the GPU is expected to need a restart, and nothing reports that state. Turbo drops text past 1,024 tokens without saying so. | Sections 2 (decision 2), 3 (Long scripts), 5.4, 5.8 | 0.1 (speaking rate) and 0.2, then 1.1 (guard, "Script Too Long") and 1.2 (GPU fault state); later phase 3 (L1 to L5) |
| One consent wording, with no "this is my own voice". | Sections 2 (decision 6), 5.9 | 1.13 |
| No Stop for the running take. | Sections 2 (decision 3), 3 (Stop), 5.3 | 1.4 |
| A double-click confirms "Click Again to …": `Arm.press` (`ui/behaviours/confirm.py`) times the second press from the first, with no minimum gap (double-click time here: 500 ms). A button that changes meaning (Unload → Load on a model card, Free VRAM → Retry on the dock) can take the second click as the new action. Forge's "Click Again to Replace" stays armed when the loaded model changes. Whether a held Enter confirms is not yet checked. | Sections 2 (decision 4), 3 (Click again), 5.2 | 0.5 (held Enter check), 1.7, 1.8 |
| Auto-unload After Idle defaults to never. Typing and moving sliders do not count as activity. A failed idle unload is retried every 15 s. | Sections 2 (decision 7), 3 (Idle), 5.9 | 1.11 |
| One bad value in any take sets the whole takes list aside, so new optional take fields would be risky. | Sections 3 (Index), 5.8 | 1.12 |
| The first settings save writes every default, so a later default change would not reach you. | Sections 2 (decision 16), 5.1 | 1.10 |
| No way to play to a device other than the Windows default, so nothing reaches a call. | Sections 2 (decisions 8 to 15), 4, 5.7 | Phase 5: 5.1 (device check after you install VB-CABLE), then 5.2 onward |
| No Rework: nothing copies a take's text back into the script, and row text cannot be selected (`user-select: none` in `ui/themes/emiya/static/emiya.css`). "Reuse A's Settings" copies delivery only and stays that way (decisions.md, section 3). | Sections 2 (decision 5), 3 (Rework), 5.6 | Phase 4 (4.1 onward) |

Not a gap: replaying a saved take in the app already works (`PlaybackController.play_take`).
