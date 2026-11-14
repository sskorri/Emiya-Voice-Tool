# Speak: failure-mode audit

Written on the morning of 2026-11-13 and re-checked against the code that afternoon. Scope: the path from a
typed script to a played take: does it take too much VRAM, do the releases work, and can spamming or
flooding the window crash it. Also the failure modes of speech into calls (F17 to F23). No application code
was changed for this page.

Decisions and defaults: [decisions.md](decisions.md). Plan items (1.1, phase 3 and so on):
[implementation-plan.md](implementation-plan.md).

## How to read the evidence

| Label | Meaning |
|-------|---------|
| **Source** | Read in the code during this audit or the afternoon re-check. The file or function is named. A fact about this PC (a Windows setting, an installed driver) says so. |
| **Measured** | Run during this audit, on the CPU only. See "Pending" for what needs the GPU. |
| **Earlier run** | The real-model check of the rebuilt engine on 2026-11-12 (all three models, scratch data). Not repeated yet on today's code. |
| **Reliability run** | Interim output of the reliability workflow running in another session during this audit. Fake engine, no GPU. Not re-run here. |
| **Research** | Web research on 2026-11-13 (upstream issues; vendor, PyTorch, NVIDIA and Microsoft pages; forums). Given with its source; not re-read line by line. A source that is neither the vendor nor Microsoft is marked third-party. |
| **Design review** | A reviewer's reasoning in the design run of 2026-11-13. Not tried. |
| **Pending** | Needs the real GPU, the window or the owner. Not run yet. |

Status column: **Still present** (the code still behaves as described), **Changed** (part of the way; the
detail says where), **Fixed**, or **Ahead** (a call failure mode; VB-CABLE, the virtual audio cable for calls,
is not installed and the route is not built). Another session edited 35 files in `engine/` and `ui/` between
10:55 and 11:45 on 2026-11-13, and `app.py` and `cli.py` at 10:57; each finding was read again after that.
That session is still editing: `ui/core/services/takes_store.py` changed again at 12:39, and the take claims
here were read after that. The statuses hold as of that reading. Nothing was run.

## Short answers

**Does it take too much VRAM?** One model is about 3 GiB of weights. The card has 12 GiB and other programs
used about 3.9 GiB (4,005 MiB) with no game running, so one model fits with about 4 GiB to spare. Two models are never loaded
together (Source; Measured in the spam test). The app can hold more than it needs. The model stays loaded
until you free it or quit (F2). Auto-unload After Idle is still off; the decided 10 minutes is not built. A
take's memory stays cached (F3). A fresh forge keeps extra memory (F12). Memory outside PyTorch's counters
stays until quit (F10). The sizes of the last three are pending.

**Do the releases work?** Every release path was read and is sound: switch, Free VRAM, idle unload, a failed
load, a take that runs out of memory, and quit (Source). In the spam test every loaded model was released
again (Measured). The earlier run's GPU numbers are not re-measured on today's code (Pending). A GPU fault
defeats every release; only a restart helps (F1).

**Can spamming or flooding crash it?** No crash path was found. A click gets the single job slot, joins a
queue of at most 3, or is refused, decided in one step on the event loop (Source); 12,800 engine calls from up
to 16 threads gave no unexpected error and no hang (Measured). Fast clicks can still do what you did not mean (F13,
F15; F14 unconfirmed). A running take cannot be stopped (F6), and the pages did more work as takes piled up
(F5; Speak's list is now capped at 200; the other fixes are not confirmed). Only script length can take the
engine down (F1).

**Can speech into a call go wrong?** It is not built yet. Most risks sit outside the app: a Windows default
device, the cable driver, Discord's voice processing (F17 to F23). The app must warn, report a failed send,
and never hang.

## Findings

Severity: **High** can leave the app unusable until restart; **Medium** costs VRAM or time without losing
work; **Low** is an edge case or a missing convenience. For F17 to F23, **High** means the call can hear what
you did not mean to send; **Medium**, the speech arrives damaged or not at all.

| ID | Severity | Finding | Evidence | Status | Plan item |
|----|----------|---------|----------|--------|-----------|
| F1 | High | A script over 2,048 text tokens reaches the model unchecked and fails inside it; on the GPU that is expected to break CUDA until restart. | Source, Measured (CPU), Research, Pending (GPU) | Still present | 1.1, 1.2, phase 3 |
| F2 | Medium | A loaded model stays in VRAM until Free VRAM or quit; Auto-unload After Idle defaults to never. | Source | Still present | 1.11 (with 1.10) |
| F3 | Medium | The VRAM a take needed stays cached until the model is unloaded. | Source; size Pending | Still present | 1.3 |
| F4 | Medium | Free VRAM is checked before a load, not before a take; a game started later is not noticed, and the driver may slow the take instead of failing it. | Source, Research | Still present | none; an owner option (see detail) |
| F5 | Medium | The pages did more work per take as takes piled up; at hundreds in one session each new take stalled the window. | Reliability run | Changed | none; the reliability session's scale fixes (R3-1 to R3-4 in `tests/pages/test_rel_scale.py`) |
| F6 | Low | The running take cannot be stopped; only waiting takes can be cancelled. | Source | Still present | 1.4 |
| F7 | Low | A take whose sampling never stops runs to the 40 s limit; known on very short scripts, frequency here unknown. | Source, Research | Still present | 0.2, phase 3 |
| F8 | Low | The "may be cut at 40 s" warning needs three comparable earlier takes, so a fresh install gets none. | Source | Still present | 1.1 |
| F9 | Low | Turbo cuts a script at 1,024 text tokens without saying so. | Source | Still present | 1.1 |
| F10 | Low | GPU memory outside PyTorch's counters (CUDA context, per-thread handles, FFT plans) stays until quit, not shown as the app's. | Source, Research; size Pending | Still present | 1.5 |
| F11 | Low | `cli.py` and `app.py` differ from the GUI: `speak` may load a model, a model-side failure in `app.py` is a bare HTTP 500, and both pass Guidance (the pacing control, `cfg_weight`) as 0.5. | Source | Still present | phase 2, P2 |
| F12 | Low | A fresh forge leaves extra GPU memory until another voice is used or the model is unloaded. | Source, Research; size Pending | Still present | 1.6 |
| F13 | Medium | A double-click satisfies every "click again" confirmation (Remove Unstarred, Delete voice, Forge replace, Clear, quit during a job). | Source | Still present | 1.7 |
| F14 | Medium, unconfirmed | A dragged slider may jump back on each page update (five a second during a job), so the release applies the old value. | Source (mechanism) | Still present (mechanism) | 0.5, 1.9 |
| F15 | Low | A button that changes meaning in place takes the second click as the new action: a card's Unload becomes Load, the dock's Free VRAM becomes Retry. | Source | Still present | 1.8 |
| F16 | Low | Smaller effects of fast or unusual input, under "Clicking fast". | Source | Changed in part | as noted there |
| F17 | High | CABLE Input becomes the Windows default speaker: you hear nothing and the call hears everything. | Research (third-party) | Ahead | 5.4 (the warning); setup step 2 |
| F18 | Medium | Different sample rates at the two ends of the cable cause crackle. | Research (third-party) | Ahead | setup step 2 (same rate on both ends); your self-test |
| F19 | Medium | Discord's noise suppression, echo cancellation or gain control garble the speech. | Research (third-party) | Ahead | setup step 4 |
| F20 | Medium | Exclusive mode on the cable blocks a second reader, or the app's own send. | Research | Ahead | setup step 2 (exclusive control unticked on both ends); 5.2, 5.3 (the failed-send toast) |
| F21 | Medium; High for the open-mic case | Discord is left on CABLE Output after a session, so friends hear silence; your real microphone may be left on open mic. | Research (third-party), Design review | Ahead | 5.4 (the reminder); setup step 8 |
| F22 | Medium | The cable is removed or disabled while a take is being sent. | Research; Pending | Ahead | 5.2, 5.3 |
| F23 | Medium | Windows' own voice processing alters the speech on a communication device. | Research; Pending | Ahead | your self-test (setup step 7) |

Setup steps are those of decisions.md, section 4. Bare numbers such as 5.4 are plan items.

Plan notes. F7: phase 3 retries a part that runs away, but a one-part script keeps today's path with no retry
(decisions.md, section 3), so a runaway one-word take has no fix planned; 0.2 shows how often it happens.
F11: phase 2 covers Guidance and a re-load reset: `cli.py load` (default `--exaggeration 0.5`) and `app.py`
`/load_voice` (`LoadVoiceRequest.exaggeration` 0.5) reset a voice's Expressiveness (the expressiveness
control, `exaggeration` in Chatterbox), because `VoiceEngine.load_voice` passes it to
`VoiceRegistry.register` (Source). The HTTP 500 and the self-loading `speak` have no plan item.

## F1 in detail: the script-length limit

- Standard and Multilingual build their text position table with 2,050 rows: 2,048 text tokens plus a
  start and a stop token (`chatterbox/src/chatterbox/models/t3/modules/t3_config.py`
  `T3Config.max_text_tokens`, used in `models/t3/t3.py`). (Source)
- One token more raises `IndexError: index out of range in self` in Chatterbox's
  `LearnedPositionEmbeddings`, checked with the installed module on the CPU. (Measured)
- With the real tokenizer files, English averages 0.60 tokens per character and crosses the limit at about
  3,400 characters; Japanese averages 1.42 and crosses it at about 1,460. (Measured)
- Nothing between the script box and the model limits the length. `SpeakController._blocked`,
  `EngineFacade.speak` and `VoiceEngine.speak` refuse only an empty script. `SpeakController.set_text` and
  `GenerateRequest` check nothing. The backends pass the text straight to `generate()`. (Source, re-checked)
- On the GPU the same lookup fails as `CUDA error: device-side assert triggered`, and CUDA calls keep failing
  until a restart: takes and loads fail, and unloading frees nothing. This is normal PyTorch behaviour; plan item 0.2 confirms it here.
  (Pending)
- Upstream reports of device-side asserts were traced to long input and cured by splitting the text
  ([#148](https://github.com/resemble-ai/chatterbox/issues/148),
  [#175](https://github.com/resemble-ai/chatterbox/issues/175)). (Research)
- Long before that limit a script is cut: one take holds at most 1,000 speech tokens, about 40 seconds
  (`engine/backends/info.py`, `max_tokens`). (Source)

So the reachable sequence is: paste a long document, press Generate. Plan item 1.1 refuses before the model:
each backend counts tokens with its own tokenizer and refuses above 2,048 (Standard, Multilingual) or 1,024
(Turbo), and until parts exist Generate reads "Script Too Long" above fixed ceilings (decisions.md, sections 3
and 5.4). Plan item 1.2 adds a GPU fault state: the app reports a broken GPU and says a restart is needed.
Stop, the idle back-off and the part retry rule rely on it. Phase 3 speaks scripts of up to 5,000
characters in parts.

## VRAM

### Budget

| Item | Size | Evidence |
|------|------|----------|
| The card | 12,282 MiB (11.99 GiB), RTX 4070 Ti | Measured (`nvidia-smi`) |
| Other programs, no game running, at the start of the audit | 4,005 MiB | Measured (`nvidia-smi`, 09:40) |
| Model weights on disk | Standard 2.97 GiB, Multilingual 2.99 GiB, Turbo 2.78 GiB | Measured (file sizes) |
| Returned by unloading a model after short takes | about 3.5 GiB | Earlier run |
| Left reserved after the last unload | 0.02 GiB | Earlier run |
| A game (Overwatch) | about 4.9 GB | noted on 2026-11-13 |
| The CUDA context, the peak of a long take, what stays cached afterwards | not known yet | Pending |

Without a game there is room for one model and its working memory. With a game the size of Overwatch, free
VRAM falls to about 3 GiB, less than a model plus the app's 0.5 GiB margin, so a load becomes a question
("Load Anyway") instead of happening silently. (Source: `Planner.low_vram`, `TokenStore.redeem`)

Why two models are never loaded: the engine holds one and unloads it before building the next
(`VoiceEngine._load`, `_evict`); a lock file stops a second program (`engine/lock.py`); the job slot runs one
job at a time (`JobRunner`); a load needs a one-time token (`TokenStore`); and takes never load
(`require_resident=True` in `EngineFacade.speak`). (Source)

### What can make the app hold more than it needs

- **F2, the model stays.** Nothing unloads a model on its own unless Auto-unload After Idle is set;
  `idle_unload_min` still defaults to 0. `IdleUnloader.check` counts as activity only the end of a job (not
  an unload), the app start and a change of the setting, and has no back-off. (Source) Decided: 10 minutes, edits on Speak and Forge
  count, and a failed idle unload backs off (decisions.md, sections 2 and 3; plan item 1.11). The settings
  foundation (1.10) comes first or with it: `SettingsStore._write` writes every setting into a fresh file,
  which would freeze the old default. No settings file exists on this PC yet. (Source, this PC)
- **F3, the take's working memory stays cached.** The engine empties PyTorch's cache on an unload, a failed
  load and a take that runs out of memory, not after an ordinary take, so the app holds the high-water mark
  of the longest take since the load. (Source: `VoiceEngine._unload`, `_call_model`) A PyTorch
  developer advises emptying it once when going idle, not routinely, because refilling costs time.
  (Research) Plan item 1.3 only if 0.1 shows the size matters.
- **F4, takes are not budgeted against free VRAM.** The low-VRAM rule looks at free memory only when a model
  is about to load, so a game started afterwards is not noticed. (Source) Since driver 536.40, NVIDIA on
  Windows lets CUDA spill into shared system memory. A take then slows down (about threefold in one report)
  instead of failing, and PyTorch does not see it. Since driver 546.01, a per-program setting brings the
  out-of-memory error back: "CUDA - Sysmem Fallback Policy" set to "Prefer No Sysmem Fallback"
  ([NVIDIA KB 5490](https://nvidia.custhelp.com/app/answers/detail/a_id/5490)). (Research) That error is
  handled cleanly: the take fails with "CUDA out of memory" and the model stays loaded. (Source) Setting the
  policy for the app's `pythonw.exe` is an option for the owner. Neither case loses work.
- **F12, a fresh forge holds extra memory.** Chatterbox computes a voice's conditioning with gradient
  tracking on (no `no_grad` in `prepare_conditionals` or `embed_ref`), so its working data stays on the GPU
  while that voice is active. A voice read from its `.pt` has none, so the extra goes when another voice is
  used. (Source) A community
  server measured about 200 MB on an RTX 4070 and fixed it by baking in inference mode. (Research,
  third-party) Plan item 1.6 only if 0.1 shows it matters.
- **F10, memory PyTorch does not count.** The CUDA context lives from the first GPU reading until quit;
  about 700 MB is quoted on the PyTorch forum. (Research, third-party) PyTorch keeps some handles per
  thread, and engine calls still run on a shared pool of threads that never exit (`EngineFacade._call`,
  `run.io_bound`). (Source) The pool holds up to 28 threads on this PC; that figure is from the morning
  audit, not re-checked. The vocoder's GPU FFTs cache a plan per distinct size, up to 4,096 by default
  (`torch.backends.cuda.cufft_plan_cache`). (Source, Research) The VRAM meter shows all of this as "other".
  Plan item 1.5 only if 0.1 shows it matters.

## Releases

| Trigger | Path | What is freed | Evidence |
|---------|------|---------------|----------|
| Switching models | `_load` → `_evict` → `_release` → `_unload` | The old model, before the new one is built | Source, Measured; Earlier run: 3.5 GiB |
| Free VRAM | `JobRunner.start_free` → `EngineFacade.unload` → `unload_backend` | The model; the lock file | Source, Measured |
| Idle | `IdleUnloader.check` every 15 s → `start_free(reason="idle")` | The same; never during a job or with takes queued | Source; off by default |
| A load fails | `_fail_load` | The half-built model; the lock file if nothing else is resident | Source, Measured |
| A take runs out of memory | `_call_model` | The take's working memory; the model stays | Source |
| Quit | NiceGUI runs the shutdown handlers (30 s limit) and exits with `os._exit(0)` | Everything, by the driver | Source (`ui/kit/window.py`) |
| The window process dies | The same monitor thread ends the server | Everything | Source |
| The app is killed | The operating system | Everything; the lock file is released with the process | Source; Reliability run (crash tests) |

`_unload` drops the model, runs the garbage collector and empties PyTorch's cache. Nothing else keeps a model
alive: errors are re-raised with only their first line, so no stored traceback holds the model, and the pages
get only text and numbers (`VoiceEngine._call_model`, `EngineFacade._call`). Kept until quit: the CUDA
context and about 0.02 GiB of small tensors; neither grows with use. (Source; Earlier run)

## Spamming and flooding

| Action | What happens | Where |
|--------|--------------|-------|
| Generate many times, model loaded | The first starts, 3 queue, the rest get "Queue full · 3 takes waiting" | `JobRunner.start_generate`, `_enqueue` |
| Generate for another model while one works | Refused, naming both models | `start_generate` |
| A model button many times | The first click spends its token; later clicks find the slot busy or the token void | `start_switch`, `TokenStore.redeem` |
| Free VRAM during a job | Refused: "Busy · wait for the current job" | `start_free` |
| Forge many times | The first takes the slot; the rest are refused | `start_forge` |
| Delete a voice during a job | Refused | `EngineFacade.delete_voice` |
| Typing fast | Change events within one loop tick are merged | `SpeakController.set_text`, `CoreEvents.notify` |
| Many refusals in a row | Each toast replaces the last | `Toast` (`ui/contract/plans.py`) |
| Hours of use | The log keeps 60 lines, the load history 500 events | `AppLog` (`ui/core/log.py`, `MAX_LINES`), `load_history.KEEP` |

Every start call claims the slot before it returns, on the event loop. Below the pages, the engine has one
lock around all model work, one per model around "point at a voice, then generate", and the lock file
between processes. (Source)

### Clicking fast does something you did not mean

From a line-by-line read of the page code, re-checked in the source; none was run in the window.

**F13, a double-click confirms.** The first click on a destructive button changes its label to "Click Again
to …", and a second click within 3 seconds acts. `Arm.press` in `ui/behaviours/confirm.py` compares the second
press with the time of the first, with no lower limit, and the button does not move. Still present.

| Button | A double-click does this | Recoverable? |
|--------|--------------------------|--------------|
| Remove Unstarred (Takes) | Sends every unstarred take of every session to the Recycle Bin, not only the rows shown (`TakesController._totals`, `purge_unstarred`) | From the Recycle Bin; on a drive without one the label says "Delete" and it is permanent |
| Delete (Voices) | Removes the voice and its `.pt`; the reference clip stays | The `.pt` goes to the Recycle Bin; re-forge from the clip |
| Forge over an existing name | Replaces that voice | No |
| Clear (Speak) | Empties the script | No |
| Close during a job | Quits | The running take is lost |

Decided (plan item 1.7; decisions.md, sections 2 and 3): a second press counts only after a pause of about
half a second, measured from the latest press. The 3 s expiry still counts from the first press, presses
within the pause after the action are ignored, and a held Enter is ignored. Half a second matches this PC's
double-click time (DoubleClickSpeed 500 ms; Source, this PC).

Why from the latest press: take presses at 0, 0.2, 0.4 and 0.6 s. Measured from the first, the one at 0.6 s
confirms. Measured from the latest, each is 0.2 s after the one before, so none counts.

A held Enter needs its own rule. Windows sends the first key repeat about 500 ms after the press here
(KeyboardDelay 1; Source, this PC). That is the same length as the pause, so the pause alone may let it
through. Today `KeyRouter.handle` in `ui/kit/vt_keys.js` ignores key repeats only for the app's shortcuts
(Source). So plan item 1.7 also ignores key-repeat presses in `vt_keys.js`. Plan item 0.5 shows whether
WebView2 presses the focused button again.

**F14, sliders and page updates (unconfirmed).** The server sends a delivery slider's value as a property
(`GoldSlider._sync`, `ui/themes/emiya/widgets.py`), and on every update Vue writes an input's `value` back
when it differs from the screen (`vue.esm-browser.js` `patchProps`). During any job the header changes five
times a second, so a dragged thumb would be put back each time. Released without further movement, the old
value applies. The number still shows the dragged one. Forge's `_ExprSlider.preview` now moves only the fill,
but that still updates the same input. Still present as a mechanism; plan item 0.5 decides. If confirmed, the
slider fix (1.9) ships before Save to Voice (decisions.md, section 5.5).

**F15, a button that changes meaning.** `ModelsController._card_action` gives the resident model's card
"unload" and the others "load" or "switch", acting at once. So the first click after an unload starts a load
of 10 seconds or more that cannot be cancelled. When a load fails, the dock's
Free VRAM is hidden and Retry appears in its place (`ui/themes/emiya/shell.py`). Still present. Plan item 1.8
puts the F13 pause on both and disarms Forge's "Click Again to Replace" when the loaded model changes
(decisions.md, section 3); today only a draft change or leaving the page voids that arm
(`ForgeController._changed`, `StatusController.on_route`). (Source)

**F16, smaller ones.**

| Item | Status |
|------|--------|
| A double-click on a Takes row plays and pauses the take (`TakesController.row_click`). | Still present |
| Each "show in Explorer" or "Show Folder" click opens another Explorer window (`ui/kit/files.py`). | Still present |
| File checks on the event loop freeze the window while a sleeping drive or a lost share answers. | Changed in part: `SettingsController._pick_output_folder` uses a worker now; `PlaybackController._wav_missing`, `TakesController.show_folder` and two folder pickers do not. |
| The rail search scans every voice and take per keystroke, without the Takes search's 150 ms delay. | Still present (`SearchController.results`) |
| NiceGUI's client drops a message over about 1 MB, so Generate would speak the last text that arrived. | Not re-checked (installed NiceGUI) |

The reliability run also pinned two star races and four scale bugs (F5). The star races are fixed: every star,
Keep A / Keep B and Remove Unstarred now go through one ordered writer, `TakeEdits` in `ui/core/edits.py`.
(Source)

### Spam test of the real engine code (Measured)

The engine's own code ran with stand-in models instead of Chatterbox, without a GPU: 12,800 random calls from
up to 16 threads in 38 s, with injected out-of-memory failures, failed bakes and cancels. 3,373 loads were
started: 3,096 finished and each was unloaded again; 277 failed as injected and left nothing. At most one model was resident at any moment; only documented
errors; no thread hung; nothing resident after `close()`. Not run again since the engine edit of 10:55.

### What the reliability run had measured by the time of this audit

All on the fake engine, reported by the other session, interim.

| Run | Result |
|-----|--------|
| Random actions | 562 tests passed in 429 s; no test saw an unintended load |
| Long session | 3,019 actions, 0 faults; live memory 5.48 MiB at the start, 5.45 MiB at the end; no growth in handlers, timers, tasks or threads |
| Event-loop lag during engine work | median about 6 ms, 99th percentile about 22 ms, worst 25 ms; opening pages meanwhile, worst about 100 ms |
| Double-clicking a star | 0 of 450 double clicks left the star flipped |
| Scale: 300 voices, 3,000 takes, 1,000 of them in this session | All budgets held except the Speak session list and the Takes page (F5, below) |

**F5 in detail.** With 1,000 takes in the current session, one Generate blocked the event loop for 1.4 s
(median) to 3.9 s (worst), and Generate 3 Takes for 4.6 to 5.6 s. The cost grew with the session's takes:
about 0.07 s at 100, 0.43 s at 300, 1.4 s at 1,000 and 7.3 s at 3,000. The Takes page went over its budget
at several sizes from 1,200 rows up. Those two failed budgets come from four underlying bugs: the Speak
list unbounded, and redrawn per take; "Show more" on Takes rebuilding every row; a voice pick rebuilding the
Voices list.

Status: changed. `SpeakController._session_list` now lists the newest 200 takes (`SESSION_ROWS`) and rebuilds
rows only when the takes, the pins, the current take or a flash change. (Source) `tests/pages/test_rel_scale.py`
calls all four (R3-1 to R3-4) fixed; those tests were not run, the code behind R3-2 to R3-4 was not read, and
the times were not measured again.

## The Chatterbox copy in this project

- `chatterbox/` reports 0.1.7 but is newer than PyPI's 0.1.7: its files are dated 2026-09-15 and it holds
  code found only on upstream's unreleased main. Three problems reported against PyPI's 0.1.7 concern code
  that is gone here. (Source, Research)
- Upstream retired the project in August 2026, with no long-text splitting, streaming, speed control or seed
  (Research), so this plan builds around Chatterbox.

## Speech into calls (F17 to F23)

Today each window plays takes through one hidden `<audio>` element to the Windows default output
(`ui/kit/audio.py`; Source). The venv has no playback library and VB-CABLE is not installed (Source, this PC).
Decided route (decisions.md, section 5.7): the server plays the take into CABLE Input through winmm, Windows'
built-in audio API; the window's player stays your monitor (Hear It); Discord, the game or OBS listen to CABLE
Output.

**F17, the cable becomes the default speaker.** All system sound then goes into the cable: you hear nothing,
and the call hears everything the PC plays. It can happen right after the install: users report it, and a
search summary of the vendor's manual says Windows 10 and later may make the newest audio driver the default.
(Research; the manual itself not opened) The second role, Default Communications
Device, shows only in the Sound control panel (mmsys.cpl); user posts say Discord's "Default" input follows
it. (Research, third-party: a Discord community post, found 2026-11-13)
Handling: the app never sets defaults and warns when a CABLE device holds either role (decisions.md, section
3; plan item 5.4); setup step 2.

**F18, crackle from different sample rates.** Forum reports blame a rate mismatch between the cable's ends
(Research, third-party); the vendor names that need only for its HiFi Cable (Research, read 2026-11-13).
Handling: decisions.md, section 4, setup step 2 sets both ends to 48 kHz. Takes are 32-bit float, mono,
24 kHz (Source); the sink converts to 16-bit if
the device refuses that (section 5.7). Your self-test is the check.

**F19, Discord's voice processing.** Noise suppression (Krisp removes sound that is not a human voice), echo
cancellation and automatic gain control are built for a microphone and can garble synthetic speech. They sit
under Input Profile › Custom, gain control behind Show Advanced Voice Settings. (Research, third-party:
Soundpad's Discord FAQ, read 2026-11-13; Discord's pages returned 403) Steam and many games have their own
processing (Research, third-party).
Handling: setup step 4. Whether Voice Activity clips a take's first syllable is unknown (Pending).

**F20, exclusive mode.** An app holding a cable end exclusively locks out the others, so the send or a second
reader cannot open it. Riot's support advises unticking "Allow applications to take exclusive control"
(Research, third-party, read 2026-11-13), and Microsoft
documents that a shared open then fails as "device in use" (Research: Microsoft's
`IAudioClient::Initialize` page, read 2026-11-13). Handling: decisions.md, section 4, setup step 2 unticks
exclusive control on both ends. A send that cannot open the device fails with a toast (F22; plan items 5.2,
5.3).

**F21, Discord left on the cable.** Friends then hear silence. Worse, from the review: Discord's Input Mode
applies to the whole client, so switching only the device back after using Voice Activity for the cable
leaves your real microphone on open mic. (Design review) That case is High: the call hears what you did not
mean to send. Per-app input devices in the Windows Volume mixer also stay set. (Research, third-party guides
found 2026-11-13) Handling: a reminder when you turn calls off (decisions.md, section 9; plan item 5.4);
setup step 8 restores what you wrote down in step 4.

**F22, the cable disappears mid-send.** Removed, disabled or taken by another app while a take goes out.
Decided: a toast and an error state, no hang, and Hear It is not switched on behind your back (decisions.md,
sections 3 and 9; plan items 5.2, 5.3). The device is stored as endpoint id plus display name and looked up at each send (section 5.7). Two winmm risks
remain. What it reports when a device vanishes mid-play is not known. Freeing an audio buffer before Windows
is done with it would crash the server (Design review). Plan item 5.1 plays nothing, so a real send is the
first test.

**F23, Windows' own voice processing.** Windows 11 24H2 and later apply Voice Clarity (echo cancellation,
noise suppression, de-reverb) by default to capture streams opened in Communications mode, when the device has
no such processing of its own. (Research: the Windows Insider blog post for build 26040, 2024-01-26, read
2026-11-13; no off switch described) This PC runs build 26200 (Source, this PC). Whether it applies to
CABLE Output is unknown; Discord has a "Bypass System Audio Input Processing" toggle (the F19 source).
Handling: your self-test (setup step 7), Hear It off: Windows' microphone test on CABLE Output (Settings ›
System › Sound › the device › Start test; Research: Microsoft's microphone set-up page, read 2026-11-13),
then Discord's.

## Pending measurements

The GPU rows (plan items 0.1 and 0.2) use a script written during the audit that drives the engine as the app
does, on scratch data, under the real engine lock. GPU work waits for the owner's go-ahead and never runs
while a game is open (decisions.md, section 7).

| Question | How it is measured | Plan item | Result |
|----------|--------------------|------|--------|
| What does the CUDA context cost? | `nvidia-smi` before and after the first GPU call | 0.1 | pending |
| How large is each model once loaded? | Reserved and device-wide memory after `make_resident` | 0.1 | pending |
| What does a fresh forge hold beyond the voice itself? | Allocation after a bake, and after reading the same voice's `.pt` | 0.1 | pending |
| What is the peak of a short, medium and long take? | Peak reserved per take | 0.1 | pending |
| What stays cached after a long take? | Reserved minus allocated while idle | 0.1 | pending |
| What does emptying the cache return, and does it slow the next take? | Time and memory around `empty_cache()` | 0.1 | pending |
| What do a switch and the last unload free? | The engine's events, and device-wide memory | 0.1 | pending |
| Does anything grow over load-and-unload cycles? | Four cycles; reserved, device-wide and process memory | 0.1 | pending |
| Does each worker thread that runs a take hold GPU memory? | One take on each of eight live pool threads | 0.1 | pending |
| Does spam at the engine hold on the GPU? | Six threads calling `speak()` while another switches and unloads | 0.1 | pending |
| How fast does each model speak, decode (how long Stop can take) and reload? | Per model; speech per character in English and Japanese | 0.1 | pending |
| Does the same seed reproduce a take? | — | — | dropped: no seed (decisions.md, section 3) |
| Do one-word scripts work? | "Hi.", "OK.", "Yes.", "No way!" and "A", three tries each on every model: length, or the error | 0.2 | pending |
| What does an over-limit script do on the GPU, and does the process recover? | A separate run with a 4,500-character script, last | 0.2 | pending |
| Is saved Guidance worth it (phase 2)? | You listen to Guidance 0.30 against 0.50 | 0.4 | pending |
| Does a dragged slider jump back (F14)? | A fake-engine window run, second screen, no focus taken | 0.5 | pending |
| Does a held Enter press a focused button again (F13)? | The same window run | 0.5 | pending |
| What does winmm see on CABLE Input? | Names, endpoint id, whether the take format is accepted; plays nothing | 5.1 | pending |
| Does a muted `<audio>` keep time while the window is covered? | A window check, your go-ahead, second screen | 5.5 | pending |
| Does the speech arrive clean (F18, F19, F23)? | Your self-test, Hear It off (setup step 7) | self-test | pending |
| Is the start delay enough for push to talk in your games, or is a hotkey needed? | Your first tries | phase 5 | pending |

## Not checked

- The native window on the real engine: audible playback, autoplay, focus, dragging (plan item 0.3).
- A GPU fault from outside the app (a driver reset or update, sleep and resume); it would look like F1.
- A real load or take while a game uses the GPU, and sessions of hours on the real engine.
- Fast input in WebView2 beyond plan item 0.5: a physical double-click against a button being disabled, and the
  redraw cost of long lists.
- Audio output devices: nothing lists, chooses or opens one yet. The sender would show in the Volume mixer as
  Python, and a muted entry there would silence sends with no error (Design review).
- The re-check covered the functions each finding names, not every edited file, nor the other session's
  test results.
