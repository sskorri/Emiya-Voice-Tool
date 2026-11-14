# Speak: implementation plan

Rewritten 2026-11-13 to follow [decisions.md](decisions.md). Nothing in it is implemented, and no
application code was changed to write it. [how-speak-works.md](how-speak-works.md) describes today's path.
[failure-modes.md](failure-modes.md) holds the audit, findings F1 to F23 (F17 to F23 come from decisions.md,
section 9). Code is named by file and function, not line: another session edited 35 files in `engine/` and
`ui/` on 2026-11-13.

The evidence labels this page uses, as defined in failure-modes.md (which also has **Design review**): **Source** (read in the code), **Measured** (run on the CPU only),
**Earlier run** (the real-model engine check of 2026-11-12), **Reliability run** (another session's
reliability workflow, fake engine), **Research** (web, 2026-11-13), **Pending** (needs the GPU, the window
or you; not run yet).

## Summary

The goal, in the owner's words: use a voice you already have or create (with permission, unless it is your
own), type what you want said, and have the tool speak it in the expression, voice and cadence you tuned
that voice to. Nothing live. The decisions add replaying and reworking takes, and speech into voice calls.

Speak, Forge and Voices already do the core of this on the fake engine (`ui/core/fake.py`, which the
tests use in place of Chatterbox). Phase 0 proves it on the real models; phases 1 to 5 build the rest.
decisions.md holds the product decisions, defaults and new module names (sections 2, 3 and 6). This plan
updates the order of the call work in its section 7, step 11. Manual Send of a saved take is the first
call milestone. Its software checks can run before installation. The silent probe is the first check
against the installed driver. The rest of the decision record still applies.

Terms: a **take** is one generated WAV. **VRAM** is the graphics card's memory. The three models are
**Standard**, **Multilingual** (many languages) and **Turbo** (faster). A voice's **`.pt`** is its saved
conditioning file. **Expressiveness** is `exaggeration` in Chatterbox; **Guidance**, the pacing control, is
`cfg_weight`. A **policy field** is a behaviour with one default that can become a setting later without
touching the code that uses it. An **armed** button is a "Click Again to …" button after its first press. The
**mockup** is the set of design boards you approve before a page changes. A **marker** is the test id a
control carries. A **chip** is a small status label. A **contract intent** is a new action in `ui/contract`
that pages call. **Turbo tags** are `[laugh]`-style cues in a Turbo script. The **parity test** is
`tests/core/test_fake_parity.py`, which checks the fake engine against the real one.

## What is still yours

The decisions are made (decisions.md, section 2), so the old "Decisions needed from you" table is gone.

| What | When | Notes |
|------|------|-------|
| Go-ahead for phase 0 | Build step 1 | Never while a game runs. 0.4 and the drags in 0.5 need you |
| One mockup round | Build step 2 | Below |
| Copy BUILD_SPEC into the repo? | Before phase 1 | The build spec lives only in an earlier session's temporary folder; 1.10 and 1.11 change its settings table and rule W6 |
| Install VB-CABLE and reboot | When ready for the real-device checks in phase 5 | You install the driver (decisions.md, section 4). The manual Send flow can already be exercised with a saved WAV and a fake audio output. |
| First tries with your games | Phase 5 | Push to talk or open mic, windowed or exclusive fullscreen: decides whether 5.7 is enough |
| Listening tests | 0.4, L5, phase 5 | Guidance 0.30 against 0.50; the joins of a long take; the call audio with Hear It off (Hear It plays the sent take on your own speakers too) |

The mockup round (decisions.md, section 7, step 2):

| Page | Visible changes |
|------|-----------------|
| Speak | Save to Voice and the Guidance tick; Stop and "Stopping…"; Script Too Long, the parts label, Generate Anyway (may be cut); Send and Stop Sending; the Hear It toggle; the call switch and its chip; Rework; the "Default" preset renamed "Neutral" |
| Takes | Send and Rework row tools |
| Voices | Default Guidance |
| Settings | The Voice Chat tab; the two call hotkeys on the Keys tab; optional General rows: Stop also drops queued takes, Rework asks before replacing the script |
| Models | "10 min" selected under Auto-unload After Idle |
| Forge | The consent sentence |
| Speak, Forge, Models, the dock | The GPU fault label "Restart to Use the GPU Again" on Generate, Forge, the model cards and the dock |

Also in the round, from decisions.md sections 3 and 5.3 but not in section 7's list: the impersonation note
under "Send speech to calls", "Click Again to Replace Script", and Stop as the summary row's rightmost link.
This plan adds the GPU fault label and its toast, the Stop toasts and the quit-while-busy text. The two
optional General rows are in decisions.md, sections 5.1 and 7. Both behaviours are decided settings
(decisions.md, sections 2 and 3); until their rows are approved they stay policy fields, then become
`stop_drops_queue` and `rework_replace` (both named in decisions.md, section 6).

One question remains for the mockup round. Should the click pause also guard Forge's result row and
Generate after an idle unload?

## What exists, and what does not

| Part of the goal | State today | Planned in |
|------------------|-------------|------------|
| Use or forge a voice (with consent), type, hear it in its Expressiveness | Built | — |
| "Unless it is your own voice" | Missing: "The speaker agreed to having this voice cloned" only | 1.13 |
| In the voice's cadence, saved from Speak | Missing: Guidance is stored nowhere; Speak has Reset, no save | Phase 2 |
| Any length of script | Partly: cut at about 40 s; over the token limit the model breaks (F1) | 1.1, phase 3 |
| Replay a saved take as it is | Built: the engine's exact WAV, no model needed | — |
| Rework a take | Partly: "Reuse A's Settings" copies the delivery, never the text | Phase 4 |
| Takes into a call | Missing: one hidden `<audio>` plays to the Windows default output | Phase 5 |
| Stop the running take | Missing: only queued takes can be cancelled (F6) | 1.4 |
| "Click again" needs a pause | Missing: a double-click confirms (F13) | 1.7, 1.8 |
| Idle unload after 10 minutes | Missing: the default is Never (F2) | 1.10, 1.11 |
| Real models through the window | Not done: only the engine ran on them (Earlier run, 2026-11-12) | Phase 0 |

## What upstream offers

Research, 2026-11-13: Chatterbox was retired in August 2026 with no long-text splitting, streaming, speed
control or seed. The copy in `chatterbox/` has no alignment analyzer, so no forced early stop or runaway
guard (Source). Everything is built around it, never inside. Other projects split at `. ! ?` into parts of
120 to 300 characters with 100 to 200 ms pauses; an unmerged proposal splits Japanese at `。！？`, then `、，`.

## Build order

decisions.md, section 7. Each step builds on the ones before it.

| Step | Items | Needs first |
|------|-------|-------------|
| 0 Freeze the inputs | No code | The other session has finished; functions re-checked by name; you approve decisions.md |
| 1 Measure and prove | 0.1 to 0.5 | Your go-ahead, no game running |
| 2 One mockup round | — | Step 0; 0.1 and 0.5 may change the boards |
| 3 Settings foundation, idle | 1.10, then 1.11 | Step 2; the sparse write lands before or with the default |
| 4 Click pause, meaning changes | 1.7, 1.8 | 1.10 |
| 5 Length guard, GPU fault | 1.1, 1.2 | 0.1 (ceilings), 0.2 (error text), step 2 |
| 6 Stop | 1.4 | 1.2, 1.10, step 2 |
| 7 Record rules | 1.12 | Before any new take or request field |
| 8 Slider fix | 1.9 | Only if 0.5 confirms F14 |
| 9 Voice delivery | P1 to P6 | 1.9 if built, 1.10, 1.12 |
| 10 Rework | 4.1 to 4.3 | Phase 2, 1.7, 1.12 |
| 11 Calls | Manual Send, then automatic routing, then game controls (5.1 to 5.8) | 1.4, 1.7, 1.10, 1.12, 4.2. Install VB-CABLE for 5.1 and the receiver checks. The software checks use a fake output. |
| 12 Long scripts | L1 to L5 | 0.1 (speaking rate), 1.1, 1.2, 1.4, 1.12, phase 5 |
| 13 Consent sentence | 1.13 | Its mockup line only |
| When 0.1 says so | 1.3, 1.5, 1.6 | 0.1; not alongside other engine or facade changes |

Each step that adds a contract intent bumps `ui/contract/__init__.py` `CONTRACT_VERSION` one minor version
from today's (1, 1) and updates the pin in `tests/rules/test_contract.py`.

## Phase 0: measure and prove

No application code. With your go-ahead, on scratch data, never while a game runs; windows on the second
screen (the 1080p 144 Hz one) without taking focus.

| Step | What | Result it must give |
|------|------|---------------------|
| 0.1 GPU audit | `gpu_audit.py` (written during the audit; in a temporary scratchpad, not the repo). It refuses to start while a full-screen window covers a screen | Per model: load size, a forge's leftover, take peaks, what stays cached, `empty_cache()` gain and cost, what switch and unload free, load cycles, eight threads, spam. **Add:** seconds per character (English, Japanese), `Job.decode_s`, reload time. Drop its seed check |
| 0.2 Edge mode | `--overlimit`, own process, last | What "Hi.", "OK.", "Yes.", "No way!" and "A" do; what 4,500 characters do, the exact error text, recovery |
| 0.3 Real engine in the window | `gui.py --data-root=…`; forge `sample.wav`; one English line per model, one Japanese on Multilingual | Takes saved, listed, played; dock and VRAM meter follow; one model resident |
| 0.4 You listen | The 0.3 window | Audible, autoplay, the right voice. Guidance 0.30 against 0.50 on Standard and Multilingual: worth saving? Do readings vary more than you like (temperature)? |
| 0.5 Window checks | Fake engine. You do the drags (an agent would take focus) | **F14:** slow drag, hold 2 s, release still, on Forge, Speak (during a take, and idle under a toast) and Voices; read the value server-side (summary "Expr …"). **Held Enter** on a focused Clear: count the clicks |

Exit: failure-modes.md's "Pending measurements" are filled in; 0.3 to 0.5 pass or are written up. The
results set 1.1's ceilings, 1.2's fault text, whether 1.3, 1.5, 1.6 and 1.9 are built, 1.4's extra cancel
points and L1's part sizes.

## Phase 1: guards and foundations

Each item's design names the files and functions it touches.

### 1.1 Script length guard and the interim "Script Too Long"

**Problem.** Standard and Multilingual take at most 2,048 text tokens; one more raises an index error inside
Chatterbox, expected on the GPU to leave CUDA unusable (F1). With the real tokenizers that is about 3,400
English or 1,460 Japanese characters (Measured). Turbo silently drops text past 1,024 tokens (F9). Long
before that a take is cut at about 40 s, and you decided to refuse such scripts until parts exist.

**Design.**
- A new field `BackendInfo.max_text_tokens` in `engine/backends/info.py` (Standard 2,048, Multilingual
  2,048, Turbo 1,024), beside the existing `max_tokens` (speech tokens per take).
- `engine/backends/base.py` `ChatterboxBackend` gets overridable hooks `_text_tokens(text, params)` and
  `_text_limit()`. Each backend counts with its own module's `punc_norm` and tokenizer: Multilingual with
  its language token, Turbo without truncation. The count runs first in `speak()`, before the voice's `.pt`
  loads. Over the limit it raises `ValueError("Script too long · 2,712 tokens, limit 2,048")`, which
  reaches the toast as errors do today. For `cli.py` and `app.py` the model may load first; accepted.
  `app.py` answers 422, not 404.
- **Interim refusal** on fixed ceilings per model and language (English and Japanese from 0.1, others
  conservative). In `ui/behaviours/speak.py`, `_blocked` returns "too long", `_generate_state` gets its own
  branch ("Script Too Long"), `_generate3_state` is disabled, and the stats line gives the reason. The time
  estimate only warns. " · cut at 40 s" stays as the backstop.
- **Generate Anyway (may be cut)**, only if the mockup round approves it: marker `speak.generate_anyway`. It
  skips the interim ceiling for one take, never the engine's token refusal; the take is cut at 40 s as
  today.
- `ui/core/fake.py` and `tests/engine/conftest.py` `FakeBackend` refuse with the same text from stand-in
  ratios (0.60 tokens per Latin character, 1.42 per wide one, measured averages).
- Tests: `tests/engine/test_backends.py` (`faked()` overrides the hooks), a slow CPU tokenizer check, the
  parity test, Speak's "may be cut" tests, the reliability suites' long scripts.

**Done when.** Engine level: 5,000 English characters are refused before `generate()`, 3,000 reach it (cut
at 40 s until phase 3), and Turbo over 1,024 tokens is refused. Speak shows Script Too Long above the
ceiling and is unchanged below it. The fake engine refuses the same inputs. If Generate Anyway is approved:
a script above the ceiling and under the token limit generates, and one over the limit is still refused.

### 1.2 GPU fault state

**Problem.** After a hard CUDA error every take and load fails with the same raw text until a restart, and
nothing says so. Stop, the idle back-off and the part retries depend on how this is reported (decisions.md,
section 5.4).

**Design.** `engine/errors.py` gets a failure kind `"gpu_fault"` for takes and loads. `engine/engine.py`
`_call_model` and `_load_failure_kind` classify a non-memory error whose text starts "CUDA error" (0.2 gives
the exact text). The engine reports it once; `ui/core/facade.py` keeps it in `EngineState`. Generate,
Forge, the model cards and the dock read "Restart to Use the GPU Again" and call nothing. `IdleUnloader`
stops trying; parts are never retried after a fault. `ui/core/fake.py` can inject one.

**Tests.** `tests/engine/test_engine.py` (classification), the parity test, the label on each control.

**Done when.** An injected fault gives one toast, one log line and the restart label on every GPU control;
the next Generate calls nothing; no idle unload is tried.

### 1.3 Trim cached VRAM after a burst (only if 0.1 says so)

PyTorch keeps a take's memory cached until the model unloads (F3). When a chain ends with nothing queued,
`ui/core/jobs.py` `_finish` asks the engine for a new `trim()` (`torch.cuda.empty_cache()` under its lock).
Build only if 0.1 shows over about 0.3 GiB left cached and no slowdown. **Tests:** `tests/core/test_jobs.py`
(one trim when a chain ends, none between queued takes), the parity test. **Done when** the cache returns to
0.1's baseline after a long take.

### 1.4 Stop

**Problem.** Only waiting takes can be cancelled (F6). A cancelled job stops at the next sampling step, but
`JobRunner.cancel_running` runs only at shutdown. Gaps (Source): no cancel point while the `.pt` loads, none
in the Standard and Multilingual decode, where a cancelled take returns "done" and is saved.

**Design** (decisions.md, sections 3 and 5.3).
- `ui/core/jobs.py` `JobRunner.stop_take()` acts only on a running Speak take (`job.kind == "generate"`
  with a take number): it cancels the job and, if `StopPolicy.drops_queue` (default on), drops the queue.
  Not during the load before takes, a forge or a test line. The field becomes the key `stop_drops_queue`
  once its General row is approved.
- One rule: Stop is checked once, just before `_save_take` saves. Before it, nothing is saved, the take
  number is not used, the toast reads "Stopped Take 8". After it, the take is normal ("Take 8 was already
  done · kept") and its ready event carries a "no autoplay, no send" flag.
- "Failed" and "refused" keep today's path and 1.2's classification.
- `start_generate` refuses while stopping; `_chain` returns before a forge; `JobState.stopping`.
- `SpeakController.stop()`: the Stop link, "Stopping…" without a timer, never delayed by the click pause;
  `speak.stop` is a required marker. No key for Stop by default (Escape is taken); bindable later. The fake
  engine gets a Standard and Multilingual decode hold that a cancel does not end.
- Only if 0.1's `decode_s` exceeds about a second: decode cancel points by wrapping the model instance's
  flow estimator `forward` and `hift_inference` in `engine/backends/base.py`.

**Tests.** Stop while preparing, sampling and decoding; with 3 queued; refused while stopping; the kept take;
"failed" beats "stopped"; no WAV, no index row. Contract, wiring and parity tests follow.

**Done when.** Stop during sampling or a Standard decode saves nothing, and the next Generate starts at
once. Stop during the save keeps the take unplayed. The queue is dropped by default and kept with the policy
off. A failure at the same moment is reported as the failure.

### 1.5 Engine calls on one thread (only if 0.1 says so)

Engine calls run on NiceGUI's shared pool (up to 28 threads here by the morning audit, not re-checked; it
never shrinks), and PyTorch keeps GPU handles per thread (F10). If 0.1 shows growth, the facade gets one worker thread for model calls.
**Tests:** `tests/core/test_facade.py` (every model call runs on that one thread). **Done when** eight pool
threads no longer add device-wide memory (a GPU run, Pending).

### 1.6 Forge without the leftover (only if 0.1 says so)

A fresh forge keeps Chatterbox's gradient work on the GPU until another voice is used (F12).
`engine/backends/base.py` `_bake` runs inside `torch.inference_mode()`. **Tests:**
`tests/engine/test_backends.py` (`_bake` runs under inference mode). **Done when** the allocation after a
forge equals that after loading the voice's `.pt` (a GPU run, Pending).

### 1.7 Click-again pause

**Problem.** `ui/behaviours/confirm.py` `Arm.press` compares a second press with the first and has no
minimum gap, so a double-click confirms Remove Unstarred, Delete, Replace on Forge, Clear and quit during a
job (F13). This PC's double-click time is 500 ms, its key repeat delay about 500 ms.

**Design** (decisions.md, sections 3 and 5.2).
- `Arm` takes `ConfirmPolicy` (`pause_s` 0.5, `window_s` 3.0) instead of `window_s`; `_Armed` keeps the
  latest press.
- A press counts only if more than 0.5 s has passed since the latest press to the same key and scope.
  Exactly 0.5 s does not count, because it equals this PC's double-click time. The arm still expires 3 s
  after the first press. An early press only moves the latest time, so no burst confirms.
- After an arm acts, the same key is ignored for the pause, any scope (else deleting one voice arms the next).
- `press` returns acted, armed or ignored; `request_quit` toasts only when armed.
- `ui/kit/vt_keys.js` `KeyRouter.handle` drops repeated Enter on every button, so a held Enter clicks once.
- Known limit: clicks are timed on arrival, so an event-loop stall (F5) can stretch a double-click.

**Tests.** `tests/behaviours/test_confirm.py`, `tests/kit/vt_keys.mjs`. Tests that confirm without moving the
manual clock step it past the pause (bench, soak, scale, flows, wiring, `test_forge.py`, the five buttons'
page tests). The starred-before-purge test in `tests/pages/test_rel_monkey.py` must also assert the purge
ran. `Invariants._why` (`tests/pages/_rel_actions.py`) gains: a burst on an armed control counts as no
action (decisions.md, section 5.2); as an extra check, a burst on an unarmed control never acts.

**Done when.** A burst at 0, 0.15, 0.3, 0.45 and 0.6 s never acts; a press 0.6 s after the latest acts; the
arm expires 3 s after the first press; a press just after an action does nothing; a held Enter clicks once.

### 1.8 The same pause on buttons that change meaning

**Problem.** A model card's "Unload" becomes "Load This Model" in place, and the reverse after a long load
(F15); the dock's Free VRAM becomes Retry after a failed load. The first click after the job acts on the new
meaning. An idle unload can turn Forge's armed Replace into an unnamed load.

**Design.** A control that changed meaning ignores clicks until a quiet pause longer than
`ConfirmPolicy.pause_s` since the later of the change and its last click (one helper beside `Arm`, no look
change). Models cards decide on the server through a `card_action(backend)` intent fed by
`ui/behaviours/models.py` `_card_action`. The dock's Free VRAM and Retry, which call `free_vram` and
`request_switch(origin="retry")` on the same controller, get the same gate.
`ui/behaviours/forge.py` disarms Replace when the loaded model changes (`engine.rev`).

**Tests.** Models and Forge behaviour and page tests; the `models.switch` and `models.unload` wiring rows
move to `card_action`.

**Done when.** After an unload, clicks closer than the pause never load and one after a longer pause acts;
the same for Load to Unload and Retry. Arm Replace, let an idle unload happen, press again: nothing loads.

### 1.9 Slider fix (only if 0.5 confirms F14)

Every update re-renders the sliders and Vue writes each value back, so a held thumb may jump back (F14,
Source). If confirmed: a kit range component next to `ui/kit/elements.py`, used by
`ui/themes/emiya/widgets.py` `GoldSlider`, that does not bind its value, applies server values only while no
pointer or key holds it, and sends on release; a `sync` counter keeps a refused Voices edit snapping back.
**Tests:** `tests/kit/test_kit.py` (a server value waits while the thumb is held; release sends once),
`tests/pages/test_pages_voices.py` (a refused edit snaps back). **Done when** a slow drag applies the
released value on Speak, Forge and Voices (read from the server side), checked once by you in the window.

### 1.10 Settings foundation

**Problem.** Behaviours with more than one sensible answer must be choices (decisions.md, section 1), but
features read plain settings keys. And `ui/core/services/settings.py` `SettingsStore._write` writes every
setting into a missing, empty or corrupt file at the first save, so a later default change never reaches you.

**Design** (decisions.md, section 5.1).
- `ui/contract/policies.py` (standard library; services may not import `ui/core`): frozen `ConfirmPolicy`,
  `StopPolicy`, `DeliveryPolicy`, `CallPolicy`, `PartPolicy` and `IdlePolicy`, the six of decisions.md.
  A seventh, `ReworkPolicy`, is proposed here, not in decisions.md.
- `ui/core/policies.py` builds them with a new `SettingsStore.get_or(section, key, default)`: a field reads
  its key if the schema has one, else its single default (clamped: pause below window). `build_core` adds
  `Core.policies`, which deciding code asks each time. Controllers with policy-dependent controls subscribe
  to `settings.changed` (Takes and Voices do not today).
- `_write` stores only the keys you set, also in a fresh file; version stays 1. `ui/entry.py`
  `read_offline` keeps its own Offline Mode default; a test pins it to the schema's.

**Tests.** New `tests/core/test_policies.py`; the fresh-file test in `tests/core/test_settings_store.py`
inverts.

**Done when.** The first save on a missing file writes one key; a changed default reaches a file that never
set it; a policy change applies at the next decision.

### 1.11 Idle default 10 minutes

**Problem.** Auto-unload After Idle defaults to Never, so a model sits in VRAM while you game (F2).

**Design** (decisions.md, sections 3 and 5.9).
- `SCHEMA` `idle_unload_min` and `ModelsVM.idle_min` default to 10; the choices stay.
- `JobRunner.note_activity(source)`, called by Speak and Forge on edits while their model is resident;
  `IdlePolicy` names the counted sources. Playback, sends and Voices edits do not count.
- `IdleUnloader` backs off after a failed unload (its own time, not `last_activity`): one toast per back-off.
- Rule W6 becomes "on by default at 10 minutes; Never turns it off". With Warm Up on Launch, an unused
  warmed model is freed 10 minutes later.
- On landing, check that `E:\Voice Tool\ui_settings.json` and `E:\Voice Tool\.dev\ui_settings.json` (the
  fake engine's data root) still do not exist; neither does on 2026-11-13. A file that stores 0 keeps it.
  `ui/core/fake.py` `Scenario` gets a settings seed, so a board under review keeps its model.

**Tests.** "Idle off" tests (`test_background.py`, `test_rel_monkey.py`) and "applies live" tests
(`test_pages_models.py`, `test_one_model.py`) seed 0 first.

**Done when.** On the manual clock a fresh install frees the model after 10 idle minutes, typing postpones
it, replaying does not, a failing unload toasts once per back-off, and Never never unloads.

### 1.12 Record rules

**Problem.** `ui/core/services/takes_store.py` `_parse_index` raises on one bad value of a known field, and
`_read_index` then sets the whole index aside; a new index version would do the same in older builds.

**Design.** New optional take fields parse leniently in `_take_from_json` (a bad value becomes the default,
logged once) and are checked in `_check_new`. `INDEX_VERSION` stays 1; `_take_from_json` already ignores
unknown keys. New `GenerateRequest` fields are frozen and hashable, defaulting to today's behaviour.

**Tests and Done when.** In `tests/core/test_takes_store.py` a malformed new field still loads every take
and version 2 is still quarantined; `tests/rules/test_contract.py` hashes the new request fields.

### 1.13 The consent sentence

**Problem.** The switch leaves out your own voice. Its sentence is a theme constant
(`ui/themes/emiya/pages/forge.py` `CONSENT_TEXT`), and `tests/rules/test_probe_theme.py` has its own copy.

**Design.** The sentence moves to `ui/contract` and reads "This is my own voice, or the speaker agreed to
having it cloned". `ui/themes/emiya/pages/forge.py` and the test probe theme in
`tests/rules/test_probe_theme.py` both import it. Nothing new is stored; "Confirm Consent First" and the
Voices labels stay. Built after its mockup line is approved.

**Tests.** `tests/pages/test_pages_forge.py`, `tests/rules/test_probe_theme.py`.

**Done when.** Forge and the probe theme show it from one constant, and Forge still refuses without the
switch.

## Phase 2: the voice remembers its delivery

decisions.md, sections 3 and 5.5. A voice's **delivery** is its knobs: Expressiveness, Guidance, and later
temperature (a steadiness control). **Problem:** Guidance is stored nowhere, `VoiceEngine.speak` never fills
it, `cli.py load` resets Expressiveness on a re-load, and Speak cannot save a tweak. If 0.5 confirms F14,
1.9 ships first. P6 lists the tests for P1 to P5; each package names the phase's "Done when" bullets it
must pass.

### P1 Engine

- **Knob table** (`engine/backends/info.py` `KNOBS`): each row maps a backend to (low, high, default); a
  backend missing from a row does not use it. Expressiveness and Guidance: Standard, Multilingual. A hidden
  temperature row: all three, 0.30 to 1.20, default 0.80; nothing fills, sends or shows it until you want
  its slider. `knobs_of(backend)` replaces checks of the model name; defaults come from `config`.
- **Storage:** a nested `delivery` object in `cache/registry.json`; Expressiveness stays top-level; absent
  until saved, so nothing migrates.
- **Registry** (`engine/registry.py`). Today `_registered_entry` builds a fresh entry and carries `meta`
  only on the same backend and clip. `register(delivery=…)`: `KEEP` takes the previous delivery under the
  registry lock, same backend only (Re-forge, Replace…); `None` carries it like `meta` (`cli.py`,
  `app.py`); `{}` clears it.
- **Engine** (`engine/engine.py`). `update_voice(delivery=…)` checks each knob with a new `_edited_knob`
  (`None` forgets; unknown, unused and out-of-range values are refused). `load_voice` with `exaggeration`,
  `language` or `delivery` left `None` keeps the stored value on the same backend and clip, else the
  default; Turbo's Expressiveness stays 0.5. `speak` fills every shown knob the model uses from the voice,
  else the default (today only Expressiveness), so Turbo never gets a Guidance.
- `engine/types.py` `VoiceDetails.delivery` is a tuple of pairs, default `()` (a `MappingProxyType` default
  fails on this venv's Python 3.11).
- Done when: bullets 2 and 3.

### P2 Other callers

`cli.py` `speak --cfg-weight`, `load --exaggeration`, `load --language`, and `app.py`
`SpeakRequest.cfg_weight`, `LoadVoiceRequest.exaggeration` and `.language` default to "not given" (today
0.5, 0.5, "en"), so a re-load no longer resets Expressiveness or language. Done when: bullet 2.

### P3 Core and contract

- `VoiceRow.delivery`, copied by `ui/core/services/voices.py` `_row`.
- `ui/core/delivery.py` (new): the knobs, the saved values, the draft and its **base** (voice and saved
  values at pick, Reset or Save). `put(session, backend, values)` is the only draft writer: pick, Reset,
  "Reuse A's Settings", Rework, the launch selection. It writes defaults for unused knobs, so a Turbo pick
  still leaves 0.50 and 0.50. When a refresh changes the saved values and the draft equals its base, both
  move. `DeliveryPolicy.memory = "save"`.
- `ui/core/facade.py`: `_speak_params` loops the knobs; `bake` passes `KEEP` when
  `ForgeRequest.keep_delivery`. `update_voice` refuses today only while `job.kind == "forge"` for that
  voice; it also refuses the voice the running chain will forge, during its load (`JobRunner` exposes it).
- `ui/core/jobs.py` `_test_request` uses the saved delivery, not 0.5. `ui/core/catalog.py`: "Default"
  becomes "Neutral". One ordered registry writer per Core replaces the per-client `_edit_lock` in
  `ui/behaviours/voices.py`. `ui/core/fake.py` mirrors it all through the engine's helpers.
- Done when: bullets 1 and 4.

### P4 Behaviours

- `ui/behaviours/speak.py` `pick_voice` and `reset_delivery`, `ui/core/background.py` `_launch_selection`
  (all three set `catalog.DEFAULT_CFG_WEIGHT` today) and `ui/behaviours/takes.py` `reuse_settings` use
  `put`. Re-picking the selected voice keeps the draft. Delivery uses of `_turbo` become knob checks.
- `save_delivery()` writes only knobs that differ from the voice's saved values (never the language),
  shows "Saving…" until the refresh lands, works during a take, is refused for a voice being forged; the
  log keeps old → new. Save to Voice shows only while the draft differs from its base. Voices gets Default
  Guidance in 0.05 steps.
- `ui/behaviours/forge.py`: `prefill` in reforge mode records the origin; `set_name`, `set_backend`,
  `forge_another` and a new clip clear it; `_request` sets `keep_delivery` only while it holds. Copy mode
  never keeps a delivery.
- Done when: bullets 1, 3 and 4.

### P5 Pages (after the mockup)

Save to Voice beside Reset (`speak.save`), the Guidance tick, Default Guidance on Voices
(`voices.edit.guid`), "Neutral". In `ui/contract/viewmodels.py`, `SpeakVM` gains `guid_tick`, `show_save`
and `save`; `SpeakController` gains `save_delivery()`; `VoicesController.edit` gains `cfg_weight=` (today
it takes `language` and `exaggeration`). Done when: bullet 1 on the pages, and the page and wiring tests.

### P6 Tests

Engine: registry, engine, `test_info.py` (each row against each `generate()`), `test_compat.py` (a re-load
without flags keeps a Japanese voice's language and Guidance). The parity test. Core voices, jobs and
background. Speak (queued takes keep their values), Voices and Forge behaviours and pages; wiring; contract.
A Save loads no model.

**Done when** (phase 2; the packages cite these numbers).
1. Guidance 0.30, Save, pick another voice and back: the slider reads 0.30 and the next take sends 0.30;
   the same after a restart, once the voice is picked again.
2. `cli.py speak` without `--cfg-weight` uses 0.30; re-running `cli.py load` on the same clip keeps both
   knobs; `--force` with a new clip resets both.
3. Re-forge and Replace… keep the delivery; a fresh forge and a copy onto Turbo do not.
4. A Voices edit moves an untouched Speak draft and leaves a tweaked one; Save is refused while that voice's
  forge chain loads; a voice with nothing saved behaves as today.

## Phase 3: long scripts in parts

decisions.md, sections 3 and 5.8. **Problem:** every take is one model call, cut at 1,000 speech tokens
(about 40 s). A script of up to 5,000 characters becomes one take, spoken in **parts** (pieces short enough
for one call) and joined. A script that fits one part takes exactly today's path: no retry, no paragraph
split. Each package names its tests and the phase's "Done when" bullets it must pass.

### L1 Splitter

`engine/parts.py` (new, standard library only):
- Ends: `. ! ?` or `。！？` plus closing quotes, then a space or the end; not in a number, after an
  abbreviation or initial, or in a run of periods. In a long sentence: the last `, ; :`, `、`, `，` or dash
  (never between digits), then a space, then a hard cut, never inside a tag. Turbo tags (`[…]`) are atomic.
- Blank lines are preferred boundaries, used only when several parts are needed. Parts under about 2 s merge.
  A part cut without punctuation gets `,` (`、` in Chinese and Japanese): a plan default, checked in L5.
- **Budget:** fixed characters per model and language from 0.1's speaking rate, well under 40 s (the briefs
  suggest 20 s); English and Japanese set from 0.1, others conservative. `PartPolicy` fields, no control.
- Tests: English, Japanese, dialogue, quotes, numbers with separators, tags, one huge sentence, no spaces.
- Done when: those cases split as written, and a script that fits comes back as one part (bullet 3).

### L2 Part driver and join

- The driver in `engine/parts.py` takes the parts and two callables (speak one part, join), so
  `VoiceEngine._take` and `FakeEngine._take` share one loop: cancel checks between parts, retries, progress.
- **When a part goes wrong** (decisions.md, section 3):

  | Case | What happens |
  |------|--------------|
  | Fails (out of memory, an error) | Retried once; a second failure drops the take |
  | Runs away (reaches the 1,000-token limit) | A part still too long for its budget is split. Otherwise it is retried once; if it wanders again, it is kept and the take is marked. Where split ends and retry begins is set with L1's budgets after 0.1 (Pending) |
  | GPU fault, refusal, Stop | Never retried |

- Parts run under the lock `speak` holds; without a caller job (`cli.py`, `app.py`) the engine binds its own.
  The 5,000 cap is checked in `speak` on both engines. `engine/progress.py` `Job` adds part and parts.
- `engine/audio.py` join, imported lazily: trim edge silence, short fades, 0.2 s between parts, 0.6 s at
  paragraphs, Standard's last 40 ms dropped per join; a 32-bit float mono 24 kHz WAV. The fake joins with
  the standard library.
- Tests: new `tests/engine/test_parts.py` (a fake speak callable that fails, runs away and is stopped),
  `tests/engine/test_audio.py` (the join), the parity test. Done when: bullets 1 to 3.

### L3 Core and contract

`GenerateRequest.parts` is a frozen `PartPolicy` snapshot from `SpeakController._request`, so a queued take
never changes its split. `Take` and `NewTake` gain `part_spans` (text offsets) and `part_ends` (sample
offsets) under 1.12; a kept runaway sets `hit_token_limit`. `JobState` gains part and parts (`_tick`
copies them; `_take_fill` counts across parts). `JobRunner._take` builds "Take 8 failed at part 3 of 5:
‹reason›". `ui/core/services/estimates.py` drops the 40 s ceiling and compares by model and language.
Tests: `tests/core/test_takes_store.py`, `test_jobs.py`, `test_estimates.py`. Done when: bullet 4.

### L4 Speak

The stats line shows " · 3 parts" (memoized: `refresh` runs every 0.2 s in a job). Generate reads
"Generating · part 2 of 5". "Script Too Long" stays only for the cap. Copy goes through the mockup. Tests:
`tests/behaviours/test_speak.py`, `tests/pages/test_pages_speak.py`. Done when: the label and progress show,
and Script Too Long appears only above 5,000 characters.

### L5 Real check and listening

A `gpu` test: 2,000 English characters give one take; peak VRAM stays near 0.1's medium take; memory outside
PyTorch's counters (F10) is recorded. You listen to the pauses, Standard's tail, the added comma, and capitals
mid-sentence. 5,000 Japanese characters would be about 15 minutes at an assumed 0.18 s per character
(Pending: 0.1 measures the rate).

**Done when.**
1. 2,000 English and 600 Japanese characters each give one take ending with the last sentence.
2. Stop between or inside parts discards every part.
3. A one-part script sends byte-for-byte today's request and returns the backend's bytes.
4. Three queued takes split alike.
5. The parity test passes with `speak(parts=)`.

## Phase 4: replay and rework

Replaying a saved take as it is already works: the engine's exact WAV plays with no model, even after its
voice is deleted; the media tests only gain a "no engine call" check. Seeds are dropped (decisions.md,
section 3). Phase 4 builds **Rework**.

### 4.1 Rework rules and the draft

**Problem.** "Reuse A's Settings" never copies the text, and the shell sets `user-select: none`, so a line
must be typed again.

**Design.** A pure function beside `put` in `ui/core/delivery.py`. Full rework (text, voice, language,
delivery) only if the take's voice exists on the take's model, which is enabled. Otherwise "line only": the
text is copied, the voice kept, and a toast says why. Turbo: text and voice only; values snap to the 0.05
grid. `SpeakController.rework(take_id)` applies it through `put`, never writes the voice, loads nothing.
"Reuse A's Settings" stays. Tests: `tests/behaviours/test_speak.py`; `tests/behaviours/test_takes.py`
`test_no_intent_loads_a_model`. Done when: phase bullet 1.

### 4.2 Rework controls and the replace arm

Rework on the take card, Takes rows (`ui/behaviours/takes.py`) and Speak's session rows. If the script box
holds other text, `ReworkPolicy.replace = "ask"` arms "Click Again to Replace Script" through `Arm` (1.7
applies); the other value replaces at once. It stays a policy field until its General row is approved, then
becomes a stored key. Rework disarms Clear. Tests: page tests, wiring, arm cases. Done when: phase bullet 2.

### 4.3 The link to the original

`Take.reworked_from` (lenient), from `GenerateRequest.reworked_from` on the first Generate after Rework,
cleared once the text no longer matches; first take of a batch only. Caption " · from Take 5". Original and
new take are pinned as A and B only when both pins are empty. Tests: `tests/core/test_takes_store.py`
(lenient field), `tests/behaviours/test_speak.py` (cleared on a changed text, first of a batch),
`tests/behaviours/test_takes.py` (pins). Done when: phase bullets 3 and 4, and pins already in use are kept.

**Done when** (phase 4).
1. One Rework puts a take's line, voice and delivery in Speak, or the line alone with a reason.
2. A typed script is replaced only on the second click.
3. The new take reads "from Take 5".
4. The original and its WAV are untouched, also when the rework is stopped.

## Phase 5: speech into calls

**First milestone — manually send a saved take.** With the model unloaded and two windows open, choose a
take in the library and press Send once. The receiver should hear one copy of the saved line with Hear It
off. Stop Sending should cut it on the first press. Repeat from the library after a restart and after
the take's voice has been deleted, using its indexed WAV.

| Delivery order | What the user can do | Work packages |
|---|---|---|
| 1. Manual Send | Enable calls, choose a device, and send or stop an existing take from the take card, Takes, or Speak's session rows. Hear It controls local monitoring. | Manual parts of 5.2–5.6. Exercise them before installation with a fake output. Use 5.1 before the first real send. |
| 2. Automatic sending | Choose Preview first (default), the Speak switch, or At once. All three use the same call service as manual Send. | Automatic parts of 5.3–5.5, including the Stop and save boundary from 1.4. |
| 3. Game controls | Bind global Send and Stop Sending and optionally delay the start so the user can hold their talk key. | 5.7. |

### What can be tested before installation

- **Manual Send, end to end in software.** Start with an indexed WAV in a scratch library. Inject a
  recording fake `AudioOutPort` that lists scripted endpoints and records starts, stops and failures.
  With two clients connected, assert one output stream, the chosen take's samples, zero model loads or
  synthesis calls, and an untouched WAV. Double-click Send, preview another take during the send, and
  delete a waiting take's WAV before its turn. Check the error state and that the next valid send works.
- **Windows adapter logic.** Feed the stream writer both a mono 24 kHz IEEE-float WAV (format tag 3,
  32-bit, as in `outputs/verify_roundtrip.wav`) and PCM16. Test float playback, conversion when the
  selected format requires PCM16, PCM16 playback, and Stop within one submitted block using a simulated
  driver. Exercise reordered devices, duplicate names, open failure and removal mid-stream.
- **Routing and timing.** Use the fake output and manual clock to exercise the three modes, first-in-batch
  selection across a model switch or queue wait, Stop during save, wait/cut overlap, master disable,
  a delayed start and Stop during the delay. A/B, ordinary Play, reference and staged clips, Forge test
  lines and search navigation must produce zero sends. Monitor and device failures stay independent.
- **Desktop checks without a cable.** With a fake output, check that a muted take's waveform advances
  and reports its end in a covered window. On Windows a bound global key can also be exercised while
  another app has focus. These are window checks with the usual go-ahead.

The existing `FakeEngine` in `ui/core/fake.py` simulates speech generation and produces PCM16 WAVs.
Plain `gui.py` selects the real `VoiceEngine`. The `--fake` launch path, page scenarios and engine
parity tests use the stand-in. Keep it for those checks. Manual Send tests can start directly from a
saved file. Their fake audio output stands in for the Windows playback device.

### What requires the installed VB-CABLE

1. After you install the driver and reboot, run the silent 5.1 probe. Record the actual playback
   endpoint ID, reported name, accepted formats, and Windows playback/recording default roles.
   If the cable took over a default role, restore the intended speakers or mic before the isolation check.
   The app warns about defaults and leaves changes to you.
2. Select **CABLE Input** as the tool's call device. In a private recording or Discord mic test, select
   **CABLE Output** as the input. With two tool windows open, the model unloaded and Hear It off, preview
   a take locally and confirm the receiver stays silent. Then Send the take once and listen for one
   complete, clean line. Repeat with a longer take and use Stop Sending mid-line. Measure the audible
   cutoff at the receiver separately from the adapter's one-block stop target.
3. Disable the selected endpoint during a send. Check for a visible error, prompt cleanup, and no audio
   on another device. Restore the cable and explicitly send again. Check that the interrupted line
   does not resume on its own. For the last milestone, verify the bound hotkeys and delay in the actual
   game or voice app with its push-to-talk or open-mic setting. Restore its previous input and mode
   afterward (decisions.md, section 4).

### 5.1 Silent device probe (first real-device action)

After installation, write and run a small standard-library probe in the scratchpad with your go-ahead.
List winmm playback endpoints and their Windows endpoint IDs via `waveOutMessage`. Query mono 24 kHz
float and PCM16 on the specific endpoint using `WAVE_FORMAT_QUERY`. Record its name as reported and
the default roles. This query plays nothing. Its result selects the adapter format or leads to 5.8.

### 5.2 AudioOutPort and the Windows adapter

Define `AudioOutPort` in `ui/contract/ports.py`. Select the real adapter only in `ui/launch.py`.
Keep winmm and `ctypes` inside `ui/kit/sound_out.py`. Parse RIFF chunks with `struct` and convert the
outgoing samples only when needed. Stream on a worker in blocks of about 100 ms. On Stop or device
loss, release buffers only after Windows returns them and close the stream. Test the adapter in
`tests/kit/test_sound_out.py` with a simulated driver, and enforce the import boundary in the layer test.

### 5.3 One call service for manual and automatic sends

- **Manual first.** One process-wide service in `ui/core/call.py` accepts a take ID. When its turn
  arrives, resolve the WAV through `TakesStore.path_of` and recheck the selected endpoint. Keep the
  service outside the model job slot and idle timer. The default overlap policy waits, with up to
  three sends waiting (plan default). Cut replaces the active send. Track each start, stop, finish
  and failure so a stale completion cannot end the next send. Report a device failure with a toast
  and error state. Keep the take available and preserve the Hear It preference.
- **Automatic second.** In `SpeakController.generate`, freeze the send intent and batch position in each
  `GenerateRequest` so both survive a load confirmation. In `JobRunner._save_take`, apply one pure
  routing function after the WAV and index are committed, just before the per-client `take_ready`
  broadcast. Enqueue and return immediately so a slow or failed send cannot hold up generation.
- **Routing rules.** Preview first requires explicit Send. Switch mode uses the switch value at
  Generate. `Session.call_switch` starts off each launch. At once sends the first request of a batch,
  regardless of its eventual take number. A run-long take falls back to preview in every mode. A failed save yields no
  send. Item 1.4 checks Stop just before saving. A Stop before that check discards the take. If Stop
  arrives during a successful save, keep the take and suppress autoplay and automatic sending.
  Older takes reach the call only through explicit Send.
- **Master disable.** Turning off Send speech to calls stops active output, cancels waiting and delayed
  sends, and invalidates unsent intents captured while calls were enabled. Turning it back on starts
  no old sends. The per-take Speak switch affects subsequent Generate presses. App shutdown releases
  the worker, pending sends and hotkeys.

Cover both paths in `tests/core/test_call.py`, including two clients, failed saves, a late Stop, an
endpoint lost mid-send, and a disable/re-enable cycle while a take is still generating.

### 5.4 Voice Chat settings

Add validated keys with their corresponding controls. For manual Send, add `call_enabled` (off),
`call_device` (endpoint ID plus display name), `call_hear` (on) and `call_overlap` (wait or cut).
Match the stored ID first, then a unique exact reported name. If neither identifies one endpoint,
keep the choice visible, show “Not found” for a missing device, and block Send until one endpoint is
identified. The app must never substitute the default speaker. Include the short impersonation note,
the Windows default-role warning (F17) and the reminder
to restore the receiver's input and mode (F21). Add `call_send` with automatic modes and
`call_delay_s` with 5.7. Update the contract's `set_tab`, the theme's `SETTINGS_TABS`, the settings
controller and `Scenario.settings_tab`. Cover availability, persistence and warnings in
`tests/behaviours/test_settings.py` and `tests/pages/test_pages_settings.py`.

### 5.5 Take card and monitor

Add Send, Stop Sending and Hear It to the take card in the first milestone. Hear It writes the same
setting as Voice Chat. Ignore a repeated Send of the same take within the click pause. Stop Sending
acts on the first press. Drive the monitor from the actual send start, including after a queue wait
or delay. Its `PlayerTarget.muted` lets the waveform advance with Hear It off. Local previews use the
existing per-window player and can be played or sought without interrupting the outgoing stream.
Add the switch and status chip with automatic sending. Cover both milestones in the Speak, playback
and page tests, plus the covered-window check above.

### 5.6 Manual Send on Takes and Speak rows

Route both row actions through the same send-by-ID operation as the card. Have `TakesController`
subscribe to `settings.changed` for the call keys. Exercise the row wiring, device availability
changes and sending an older take after restart in the Takes behaviour and page tests.

### 5.7 Global hotkeys and start delay

Bind `keys.call_send` and `keys.call_stop` on Settings › Keys, unbound by default. Show the bindings
read-only on Voice Chat with a link to Keys. Use Windows `RegisterHotKey` on a server thread behind
a port (proposed `ui/kit/hotkeys.py`). Dispatch to the existing call service. Offer start delays of
0, 1, 2 or 3 seconds, default 0. The user presses the game talk key. Test dispatch with a fake
hotkey source, delayed Send and immediate Stop on the manual clock, and zero registrations when unbound.

### 5.8 Fallback if the winmm probe fails

If 5.1 rules out winmm and you install `sounddevice`, add its WASAPI shared-mode adapter behind
`AudioOutPort`, imported lazily. Repeat the adapter contract tests, endpoint/format probe and
private receiver check with this adapter.

**Phase 5 acceptance.** Record the software results separately from the installed-device results.
Manual Send is verified on the device when the receiver captures exactly one saved take with Hear It
off, stays silent during local preview, and cuts the line on Stop. Complete the automatic-routing
and game checks against that same working path.

## Out of scope

Nothing live, decided by the owner on 2026-11-13: no microphone input, no real-time voice changing, no
spoken conversation. Calls are in scope because they play saved takes only.

## Rules every phase keeps

- **One model in VRAM.** `tests/core/test_one_model_rule.py` and `tests/pages/test_one_model.py` stay green.
- **Layers.** Only `ui/core` touches the engine (`tests/rules/test_layers.py`); only `ui/launch.py` picks a
  real audio sink.
- **The fake engine mirrors the real one** in the same step (the parity test).
- **Chatterbox's files stay untouched.** Fixes go around it.
- **Markers and the mockup.** A new control gets a marker and an approved look first.
- **Choices, not locks.** A behaviour with more than one sensible answer is a setting or a policy field
  (decisions.md, section 1). Never settings: one model in VRAM, consent before a forge, the token limits.
- **A stored key comes only with its approved control.** Until then the value is a policy field
  (decisions.md, section 5.1).
- **Settings live in `ui_settings.json`**, never browser storage (WebView2 runs private). Each new key gets
  a check, tests, and a row in the settings table once that table has a home.
- **No new dependency** without your approval. **The app never sets Windows default devices.**
- **Research and checks never use the browser pane or open file links.**
- **GPU and window test runs** need your go-ahead, with the game closed and the test window on the second
  screen without taking focus. Automated call tests use a fake audio output. The audible receiver and
  in-game checks in phase 5 are manual checks after installation. Phases 1 to 5 start after step 0.

## Replaced from the first version of this plan

Replaced by decisions.md, section 11: seeds (none, phase 4); two consent wordings (one sentence, 1.13);
calls out of scope (in scope, phase 5); a top-level `cfg_weight` (the nested `delivery`, P1); 1.7's
random-click claim (corrected; the pause counts from the latest press); 1.8's one-second lockout (the 1.7
gate, dock included); phase 3's part sizes and pauses (fixed per model and language, 0.2 s and 0.6 s).
Also changed: the "Decisions needed from you" table (answered); 1.1's "3,000 characters still reach the
model", now at the engine level; 1.4's "within one step", true only while sampling.
