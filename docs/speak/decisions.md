# Speak: decision record

Written 2026-11-13. This is the foundation the build starts from. No application code was changed to
produce it.

How it was made:

- The open decisions were put to the owner and answered (sections 1 and 2).
- A read-only workflow wrote one design brief per area: 8 briefs, 18 adversarial reviews, then a
  completeness critic.
- The claims that change a design were re-checked against the code at 12:10 that day. Another session
  was editing `engine/` and `ui/` during the work (35 files between 10:55 and 11:45), so this page names
  functions, not line numbers.

Where this page and [implementation-plan.md](implementation-plan.md) disagree, this page wins. The plan
has since been rewritten to follow this record; section 11 lists what this record replaced in the plan's
first version.

## 1. The principle

> "We'll do save for now, but keep it modular and clean. We don't know if we'll add options for these
> aswell so its best to build almost expecting settings to be fully user chosen."
> "We shouldn't hard lock it one way or the other."

Every behaviour below that has more than one sensible answer is built as a choice:

- a stored setting where a control is approved;
- otherwise a policy field with one default, which can become a setting later without touching the code
  that uses it.

Things that are never settings: the one-model-in-VRAM rule, consent before a forge, and the token limits
of the models.

## 2. What the owner decided

| # | Topic | Decision |
|---|-------|----------|
| 1 | How a voice remembers its delivery | An explicit **Save to Voice**, built so that other ways of remembering can become a choice later |
| 2 | Scripts longer than one take | **Speak them in parts, joined into one take.** Until that exists, refuse with a clear label |
| 3 | Stopping the running take | **A Stop.** Whether it also drops queued takes is **a setting; the default drops them** |
| 4 | "Click Again to …" | Counts only after **a pause of about half a second**, on every such button |
| 5 | Replaying and reworking | **Replay saved takes exactly as they are**, so a line need not be typed again, also into a call. **Rework** a take: tweak what its model allows and generate again; the model of a voice is fixed |
| 6 | Consent on Forge | **One switch:** "This is my own voice, or the speaker agreed to having it cloned" |
| 7 | Auto-unload After Idle | **Default 10 minutes** (today: never). **Typing or moving a slider on Speak or Forge** counts as activity; playing or sending saved takes does not |
| 8 | Speech into voice calls | **Wanted** for Discord, in-game voice chat (push to talk, open mic, and Discord instead of game chat) and recordings or streams. The call hears **the tool only**. The owner installs **VB-CABLE** themselves |
| 9 | When a take goes into the call | **Three modes, all choosable:** preview first, a switch on Speak, at once. **Default: preview first.** Calls stay off until "Send speech to calls" is turned on |
| 10 | Hearing the call audio yourself | **On by default; a toggle on the take card**, mirrored in Settings |
| 11 | Playing an older take | **Never reaches the call by itself.** Only Send puts it in, in every mode |
| 12 | Send while another take is still going into the call | **A setting; the default waits its turn** |
| 13 | Games | Plan for push to talk, open mic and Discord. Games may run windowed or exclusive fullscreen |
| 14 | A Python audio library | The built-in Windows route first. **sounddevice only if that fails**, installed by the owner |
| 15 | Where the call settings live | **A new "Voice Chat" tab** in Settings |
| 16 | The settings file | **Write only what the user changed**, so a later default change still reaches them |

## 3. Defaults for the details (the owner may override any of these)

These came out of the briefs and reviews and were not put to the owner. Each has a default that follows
from the decisions above.

| Area | Detail | Default |
|------|--------|---------|
| Calls | "Switch" mode at launch | The Speak switch starts off at every launch |
| Calls | "At once" mode with Generate 3 Takes | Only the first take is sent |
| Calls | A take flagged as having had a part run long | Falls back to preview, even in "at once" mode |
| Calls | What can reach the call | New takes (per the mode) and explicit Sends. Never reference clips, Forge test lines, staged clips, A/B compare or search jumps |
| Calls | A send fails mid-way (cable removed) | A toast and an error state. Hear It is not switched on behind your back |
| Calls | Stop Sending | Acts on the first press, never delayed. A second Send of the same take within the pause is ignored |
| Calls | Push to talk and fullscreen games | A **global hotkey** for Send and for Stop Sending, unbound until you pick the keys on Settings › Keys. A **"start after" delay** of 0, 1, 2 or 3 s (default 0), so you can get back to the game and hold your talk key. The app never presses game keys for you |
| Calls | Volume in the call | Discord's input volume and the cable levels; no in-app volume for now |
| Calls | A note about impersonation | One line under "Send speech to calls"; it needs mockup approval |
| Calls | Windows default devices | The app never sets them. It warns when a CABLE device holds a default role |
| Stop | During the model load before takes | Stop acts only on a running take |
| Stop | A take that had already finished when Stop arrived | Kept as a normal take, not autoplayed or sent; toast "Take 8 was already done · kept" |
| Stop | Parts of a long take made so far | Discarded |
| Stop | A failure at the same moment | The failure is reported, not "stopped" |
| Stop | A key for Stop | None by default; bindable later. Escape is already taken |
| Click again | How the half second is measured | **From the latest press**, so a burst of clicks never confirms. The 3 s expiry still counts from the first press. Presses within the pause after an action are ignored. A held Enter is ignored. The value is one policy field |
| Click again | Buttons that change meaning in place | The same pause guards a model card's Unload → Load and the dock's Free VRAM → Retry |
| Click again | Forge's "Click Again to Replace" | Disarms when the loaded model changes, so an idle unload cannot turn the second press into a load |
| Delivery | What Save to Voice stores | Expressiveness and Guidance (and later knobs) that differ from the voice. Never the spoken language: that describes the clip |
| Delivery | The draft after the voice changes elsewhere | The draft remembers what it started from. Save to Voice appears only for your own tweaks, and an edit made on Voices moves an untouched draft along |
| Delivery | Re-forge and Replace… of the same name on the same model | Keep the saved delivery. A fresh forge starts from the defaults |
| Delivery | `cli.py load` and `app.py /load_voice` | Stop resetting Expressiveness to 0.50: they keep the stored values when the model and the clip are the same |
| Delivery | `cli.py speak` without `--cfg-weight` | Uses the voice's saved Guidance |
| Delivery | The "Default" preset | Renamed "Neutral" (the model's 0.50 / 0.50), because "Default" would no longer mean the voice's default; copy approved in the mockup |
| Delivery | Re-picking the voice already selected | Keeps the draft |
| Delivery | Default Guidance on the Voices page | 0.05 steps, like the Speak slider |
| Delivery | Saving while that voice is about to be forged | Refused, also during the model load that comes before the forge |
| Steadiness | Temperature | Becomes a delivery knob for all three models (Turbo included), 0.30 to 1.20, default 0.80. Its slider is shown only after you have heard real takes and want it |
| Rework | Its voice was deleted or is now on another model | "Line only": the text is copied, the current voice is kept, and a toast says why. A full rework only when the model matches |
| Rework | The script box already holds other text | A setting; the default asks first ("Click Again to Replace Script") |
| Rework | Link to the original | Recorded. The original and the new take are pinned as A and B only when both pins are empty |
| Rework | "Reuse A's Settings" on Takes | Stays as it is (delivery only) |
| Seed | A seed per take | **Not built.** Replaying saved audio already gives identical playback, and a long take records where its parts join, so one part can be redone and joined back without a seed |
| Long scripts | A script that fits in one part | Takes exactly today's path: no retry, no paragraph split |
| Long scripts | Until parts exist | Refuse on fixed ceilings per model and language (English and Japanese set from the phase 0 measurement, plan item 0.1; others conservative), backed by an exact token count in the backend. The time estimate only warns. A "Generate Anyway (may be cut)" control is proposed for the mockup round |
| Long scripts | Part size | Fixed per model and language, set from the phase 0 speaking-rate measurement. A part that still runs too long is split. Stored as policy fields; no control until one is approved |
| Long scripts | A part fails or runs away | Retry that part once; if it fails again, drop the take. A part that wanders twice is kept and the take is marked |
| Long scripts | Joining | Trim edge silence, short fades, 0.2 s between parts and 0.6 s at paragraphs; drop Standard's last 40 ms at each join |
| Long scripts | Longest script | 5,000 characters |
| Idle | A settings file that already stores "never" | Kept. No settings file exists on this PC yet |
| Idle | An idle unload that fails | Backs off instead of retrying every 15 s |
| Settings | Values without an approved control | Policy fields only, never stored keys, so the settings file never freezes them |
| Consent | Where the sentence lives | In `ui/contract`, so every theme shows the decided wording |
| Consent | Voices added by `cli.py` or `app.py` | Keep reading "Not confirmed · re-forge to confirm" |
| Index | New optional fields on a take | Parsed leniently: a bad value becomes the default and is logged. Today one bad value makes the app set the whole takes list aside |

## 4. VB-CABLE, and what it is for

The owner asked whether this is "the one audio driver … the one you need for W-Okada". Yes, it is the
same kind of driver:

- The w-okada voice changer's own tutorial asks for "a virtual audio device such as Voicemeeter".
  Voicemeeter is VB-Audio's virtual mixer.
- The third-party w-okada guides use **VB-CABLE**, also by VB-Audio. That is the one this tool needs.
  Voicemeeter is not needed, because the call hears the tool only.

What VB-CABLE adds, per the vendor page read 2026-11-13:

- A playback device, **CABLE Input (VB-Audio Virtual Cable)**. The tool plays into it.
- A recording device, **CABLE Output (VB-Audio Virtual Cable)**. Discord, the game or OBS listen to it.
  (These device names come from third-party guides.)
- It is free for personal use (donations welcome).
- Installing it means running the setup as administrator and rebooting. Removing it needs a reboot too.

**Nothing has been installed or downloaded.** A research agent in an earlier run opened a VB-Audio PDF
manual in the built-in browser, and that is what showed a save prompt. That run was stopped, and the
research that followed was not allowed to use a browser or open file links.

### Your setup, later (steps from the research, to be checked when you do it)

1. Download VB-CABLE from vb-audio.com/Cable, extract it, and run the 64-bit setup as administrator.
   Choose Install Driver, then reboot. If Windows refuses the driver, stop there: do not turn off Memory
   Integrity.
2. Open the Windows sound control panel (Settings › System › Sound › More sound settings). On the
   Playback and Recording tabs, check which devices are "Default Device" and "Default Communications
   Device". Neither may be a CABLE device; put your speakers and microphone back if Windows moved them.
   While you are there, give both ends of the cable the same format: open CABLE Input on the Playback
   tab, then CABLE Output on the Recording tab, and on each one's Properties › Advanced tab pick the
   same rate (48000 Hz). On both Advanced tabs, also untick "Allow applications to take exclusive
   control of this device", so that no single app can lock the tool, Discord or OBS out of the cable.
   (This is forum and community advice; check it when you install.)
3. In the tool, turn on Voice Chat › Send speech to calls and choose CABLE Input as the call device.
4. In Discord, first write down your current voice settings. Then, under User Settings › Voice & Video,
   change these:
   - Input Device: CABLE Output (VB-Audio Virtual Cable).
   - Input Profile: Custom.
   - Noise Suppression: none.
   - Echo Cancellation and Automatic Gain Control: off.
   - Input Mode: Voice Activity. The silence between takes keeps it closed.
5. Game chat:
   - A game with its own input picker gets CABLE Output there.
   - Steam's voice chat has its own picker under Steam › Settings › Voice.
   - Push-to-talk chats need the hotkey and the delay from section 3.
6. Recording or streaming: add CABLE Output as an audio input in OBS.
7. Self-test, alone: turn Hear It off, send a take, and watch the level meter of CABLE Output on the
   Recording tab. Then use Discord's mic test.
8. To put everything back: return Discord, Steam and any game to your microphone, and restore the
   settings you wrote down. To uninstall, run the setup as administrator, remove the driver and reboot.

Things that may garble the speech, still to be seen in practice: Discord's noise suppression, echo
cancellation and gain control; Windows' own voice processing on communication devices; and a sample-rate
mismatch between the two ends of the cable (crackle).

## 5. The design per area

The vocabulary in section 6 is used throughout.

### 5.1 Settings foundation

- Each feature reads its choices from one small, frozen policy record: delivery, confirmation, Stop,
  calls, parts, idle. The policy types live in `ui/contract` (standard library only); the builder that
  fills them from the settings lives in `ui/core`.
- Code that makes a decision asks for the policy at that moment. Making a hidden value a user choice
  later means adding a stored key and its row; the code that uses it does not change.
- A stored key is added only together with its visible control. Values without a control stay policy
  defaults.
- The settings file holds only what the user changed (today a fresh file gets every default written into
  it). This lands before or together with the 10-minute idle default. A test checks that the early
  "Offline Mode" read in `ui/entry.py` agrees with the default when the file is missing.
- Placement:
  - A new "Voice Chat" tab in Settings: enable, device, send mode, Hear It, overlap, delay. It shows the
    two call hotkeys read-only, with a link to Settings › Keys.
  - The two call hotkeys are bound on Settings › Keys, with the app's other key bindings, marked as
    working outside the app.
  - Idle stays on Models › Behavior.
  - Later choices about delivery memory or the click pause go in General.
  - General includes "Stop also drops queued takes", on by default. "Rework asks before
    replacing the script" awaits the Rework action.

### 5.2 Click-again pause, and buttons that change meaning

- `Arm.press` in `ui/behaviours/confirm.py` keeps the time of the latest press. A press counts only after
  the pause since the previous one, within 3 s of the first. Presses within the pause after the action
  are ignored.
- `ui/kit/vt_keys.js` ignores key-repeat activations.
- The same pause guards the Models cards and the dock when their button changes meaning.
- Tests that press an armed control twice without moving the test clock need a clock step. The
  random-click invariants count a burst on an armed control as no action.

### 5.3 Stop

- `JobRunner` gets a stop for the running take that cancels its job (`Job.cancel` already stops sampling
  at the next step) and, when the policy says so, drops the queue.
- One rule in `JobRunner._save_take`: Stop is checked once before saving. A take saved after that is
  finished; its ready event carries a "no autoplay, no send" flag.
- The control: a "Stop" link at the right end of the Speak summary row, with Cancel Queue to its left, so
  nothing moves under the pointer. It needs a mockup update.
- On Standard and Multilingual the final decode has no stopping point; it is measured in phase 0.

### 5.4 Script length

- Each backend counts the script's text tokens with its own tokenizer before `generate()`. It refuses
  above 2,048 (Standard, Multilingual) or 1,024 (Turbo) with a short `ValueError`, which reaches the
  toast as it does today.
- Speak's Generate reads "Script Too Long" above the fixed ceilings in section 3.
- The GPU fault state (plan item 1.2) belongs here too. No brief covered it, but Stop, the idle back-off
  and the part retry rule all depend on how a GPU fault is reported.

### 5.5 Voice delivery (Save to Voice)

- **Knob table.** `engine/backends/info.py` gets a table of delivery knobs, each with its range and
  default per model. A model that is not listed for a knob does not use it, so Speak stops deciding by
  the model's name. Adding temperature later is one row.
- **Storage.** A voice's saved knobs go in a nested `delivery` object in `cache/registry.json`; for now it
  holds only `cfg_weight`. Expressiveness stays where it is. The key is absent until something is saved,
  so nothing needs migrating.
- **Registry and engine.**
  - `VoiceRegistry` carries `delivery` the way it carries consent today: on the same model and the same
    clip.
  - Forge's Re-forge and Replace… ask for it to be kept, and the registry resolves that under its own
    lock.
  - `VoiceEngine.update_voice` validates each knob against the table.
  - `VoiceEngine.speak` fills every knob the caller left out from the voice, as it does for
    Expressiveness today. `cli.py` and `app.py` therefore get the voice's cadence too.
- **UI.**
  - `ui/core/delivery.py` is the one place that knows what a voice's delivery is: which knobs, the
    saved values, the draft and what the draft started from.
  - Every write to the draft goes through it: picking a voice, Reset, Reuse, Rework, the launch
    selection.
  - The policy constant there is "save"; an automatic mode would be another value.
- **Pages.**
  - Save to Voice on Speak's Delivery card.
  - A tick on the Guidance slider at the voice's value.
  - A Default Guidance row on Voices.
  - All three need a mockup update.
- Dependency: if the phase 0 window check confirms that a dragged slider can jump back (audit F14), the
  slider fix ships first, because otherwise Save to Voice could store a value other than the one shown.

### 5.6 Replay and rework

- **Replay.** Takes are already saved WAVs, so replaying is identical by nature. What is new is a
  **Send** action on Takes rows, on Speak's session rows and on the take card.
- **Rework.** A "Rework" action on the same rows and on the take card copies the take's text, voice and
  delivery into the Speak draft, with the fallback rules in section 3.
- The take record gets an optional "reworked from" field.
- Row text cannot be selected today (the shell turns text selection off), which is another reason Rework
  must copy the line.

### 5.7 Calls inside the app

- **The route.**
  - The server process plays the take into the call device through Windows' built-in audio API (winmm,
    called with the standard library's ctypes). It sits in `ui/kit/sound_out.py`, behind an
    `AudioOutPort` in `ui/contract/ports.py`.
  - Takes are 32-bit float WAVs at 24 kHz, which the standard library's `wave` module does not read, so
    the kit reads the header itself and converts to 16-bit if the device asks for it.
  - If the winmm check after the install fails, sounddevice replaces it behind the same port.
  - No WebView2 flag that grants device access is added.
- **Two outputs.**
  - The window's existing audio player stays the monitor; it is muted when Hear It is off.
  - The call output is separate and process-wide (`ui/core/call.py`), so a preview can never cut off a
    send.
  - A send ends only on Stop Sending, its own end, or a new send under the overlap setting.
- **Deciding.** Whether a new take goes into the call is decided once per take, from an intent recorded
  when Generate was pressed, by one pure routing function. All three modes are rows of that function.
- **The device.**
  - Stored as the Windows endpoint id plus a display name.
  - Matched by id, then by exact name, never by a guessed prefix.
  - When it is missing, the app says so and keeps the take local.
- **The hotkey.** A global hotkey (Windows `RegisterHotKey`, on its own thread in the server process)
  triggers Send and Stop Sending, with the optional delay. Both keys are bound on Settings › Keys and
  stored in the existing `keys` setting as `call_send` and `call_stop`, unbound by default.

### 5.8 Long scripts in parts

- **Splitter and driver.** `engine/parts.py` holds both, standard library only: sentence ends `. ! ?` and
  `。！？`, then commas, never inside a number or a Turbo tag. The engine and the FakeEngine both call it,
  so they cannot drift apart.
- **The loop.** `VoiceEngine._take` speaks the parts under one hold of the engine lock, with a cancel
  check between parts, and joins them into one WAV.
- **Policy.** The part policy is snapshotted into the request at Generate, so a queued take never changes
  its split.
- **The record.** A take records its part spans (text offsets) and part ends (sample offsets). The index
  stays at version 1, with lenient parsing.

### 5.9 Consent and idle

- **Consent.** The sentence moves to `ui/contract` and reads as decided. The probe theme's copy is
  updated with it. Nothing new is stored.
- **Idle.**
  - Default 10 minutes.
  - `JobRunner` gets a small "note activity" call, which Speak and Forge make on edits while their
    voice's model is loaded.
  - A failed idle unload backs off.
  - The spec's rule W6 is amended to "idle auto-unload on by default at 10 minutes; Never turns it off".

## 6. Vocabulary and new modules

One set of names, fixed before any code:

| Name | Meaning |
|------|---------|
| `call_enabled` | "Send speech to calls" (default off) |
| `call_device` | The call device: endpoint id plus display name ("" = none chosen) |
| `call_send` | `"preview"` (default), `"switch"`, `"at_once"` |
| `call_hear` | Hear It (default on) |
| `call_overlap` | `"wait"` (default), `"cut"` |
| `call_delay_s` | 0, 1, 2 or 3 (default 0) |
| `Session.call_switch` | The Speak switch in "switch" mode (off at launch) |
| `keys.call_send`, `keys.call_stop` | The global hotkeys for Send and Stop Sending (unbound by default) |
| `stop_drops_queue` | Stop also drops queued takes (Settings › General, default on) |
| `rework_replace` | `"ask"` (default) or `"at_once"`: what Rework does when the script box holds other text (a policy field until its row is approved) |
| `delivery` (registry) | A voice's saved knobs other than Expressiveness |

| New module | Holds |
|------------|-------|
| `ui/contract/policies.py` | The frozen policy records |
| `ui/core/policies.py` | The builder that fills them from the settings |
| `ui/core/delivery.py` | Knobs, saved values, the draft and its starting point |
| `ui/core/call.py` | The call output and the routing function |
| `ui/contract/ports.py` (extended) | `AudioOutPort` |
| `ui/kit/sound_out.py` | The winmm sink |
| `engine/parts.py` | The splitter and the part driver |

A layer rule is added so that only `ui/launch.py` may pick a real audio sink. Tests get a fake one.

## 7. Build order

Each step builds on the ones before it.

0. **Freeze the inputs.** Wait until the other session has finished editing. Re-check the functions
   named here, and approve this record. No code.
1. **Phase 0 measurements.** The parked GPU audit, plus speaking rate, decode time, reload time, the
   edge-mode run (one-word and over-limit scripts), the real engine in the window, your listening, the
   slider jump (F14) and a held Enter in the window (plan items 0.1 to 0.5). Only with your go-ahead,
   never while a game runs.
2. **One mockup round** for every visible change. On Speak:
   - Save to Voice and the Guidance tick
   - Stop and its "Stopping…" state
   - Script Too Long, the parts label and Generate Anyway
   - Send and Stop Sending
   - the Hear It toggle
   - the call switch and its chip
   - Rework

   Elsewhere: Send and Rework on Takes rows; Default Guidance on Voices; the Voice Chat tab; the two
   call hotkeys on Settings › Keys; "10 min" on Models; the consent sentence and the "Neutral" preset.
   Optional: General rows for "Stop also drops queued takes" and "Rework asks before replacing the
   script".
3. **The settings foundation**, with only-what-you-changed saving and the 10-minute idle default.
4. **Click-again pause** and the guards on buttons that change meaning.
5. **Script length guard** and the interim refusal; the GPU fault state.
6. **Stop.**
7. **Record rules:** lenient parsing for new take fields, frozen new request fields.
8. **The slider fix**, if phase 0 confirmed F14.
9. **Voice delivery** (Save to Voice).
10. **Rework.**
11. **Calls**, after you install VB-CABLE: first a check that only lists the devices and asks whether
    they accept the take's format, without playing anything. Then the build, with Send, Hear It, the
    modes, the hotkey and the delay.
12. **Long scripts in parts.**
13. **The consent sentence**, any time after its mockup line is approved.

## 8. Still to measure or try

| What | Why it matters | How |
|------|----------------|-----|
| A dragged slider jumping back during a page update (F14) | Whether Save to Voice can be trusted before the slider fix | A window run on the fake engine, second screen, no focus |
| A held Enter on a focused button | Whether the key-repeat rule is enough | The same window run |
| Speaking rate per model and language | Part sizes and the interim ceilings | The GPU run in phase 0 |
| Decode time per model | How long Stop can take | The GPU run |
| Reload time after an idle unload | Whether 10 minutes feels right | The GPU run |
| winmm on CABLE Input: names, format, the endpoint id | The no-install call route | A device-listing check after you install VB-CABLE; plays nothing |
| A muted take in the window keeps time and reports its end | The take wave and the monitor while Hear It is off | A window run on the fake engine |
| Push-to-talk behaviour of your games | Whether the delay is enough or a hotkey is needed | Your first tries |
| Windows voice processing on the cable | Whether the speech arrives clean | Your self-test, with Hear It off |
| Guidance 0.30 vs 0.50 by ear | How much saving Guidance is worth | You listen in phase 0 |

## 9. New failure modes from the call work

These join the audit in [failure-modes.md](failure-modes.md):

| ID | Failure | Handling |
|----|---------|----------|
| F17 | CABLE Input becomes the default speaker: you hear nothing and the call hears everything | The app warns; setup step 2 |
| F18 | Different sample rates at the two ends of the cable cause crackle | Setup step 2 |
| F19 | Discord's noise suppression, echo cancellation or gain control garble the speech | Setup step 4 |
| F20 | Exclusive mode on the cable blocks a second reader | Setup step 2 |
| F21 | Discord is left on CABLE Output after a session, so friends hear silence | A reminder |
| F22 | The cable is removed or disabled mid-send | Toast and state; no hang |
| F23 | Windows voice processing alters the speech on a communication device | Your self-test |

## 10. Not decided here

- The look of every new control. That is the mockup round.
- Whether to copy BUILD_SPEC into the repo. It lives only in a temporary folder of an earlier session
  that may be cleaned, and the settings table and rule W6 come from it.
- Anything live: microphone input, real-time voice changing, conversation. Out of scope, as decided
  earlier.

## 11. What this replaced in the first version of the plan

The first version of [implementation-plan.md](implementation-plan.md) was written before these decisions.
It has since been rewritten to follow this record. Its "Decisions needed from you" table numbered its own
questions; those numbers are not the decisions in section 2.

- **The old plan's question 4, a seed per take.** Replaced: no seed (section 3).
- **The old plan's question 5, the consent choice.** Replaced: one switch with the decided sentence (plan
  item 1.13).
- **The old plan's question 7 and its "Out of scope", calls.** Replaced: calls are in scope (sections 2, 4
  and 5.7; plan phase 5).
- **Phase 2, a top-level `cfg_weight`.** Replaced by the nested `delivery` object (5.5).
- **Item 1.7, the click-again pause.** Its claim about the random-click tests is corrected, and the
  pause is measured from the latest press (5.2).
- **Item 1.8, the Unload → Load guard.** Replaced by the same pause gate as 1.7, which also covers the
  dock (5.2).
- **Phase 3, part size and pauses.** Adjusted: fixed per-model budgets, a one-part script is unchanged,
  and the pauses are 0.2 s and 0.6 s (5.8).
