# Speak: decisions, plan and audit

Updated 2026-11-15. Speak and Discord share the normal Emiya Voice window. The November 13 plan and audit follow for reference.

## Try Speak with Discord

Open **Emiya Voice.exe** in the portable folder. From the development checkout, use
`venv\Scripts\pythonw.exe gui.py`. Each copy reads its own settings and voice library.

1. Install VB-CABLE on the PC that will send speech, then reboot. Leave your headphones and microphone as your normal Windows defaults. In **Settings → Voice Chat**, refresh the devices, choose **CABLE Input** and turn on **Send speech to calls**. In Discord's **Voice & Video** settings, choose **CABLE Output** as the input.
2. On **Speak**, select a voice, type a short line and click **Generate**. **Preview first** is the default. Click **Send** on the finished take when you want the call to hear it. **Hear It** controls your local monitor.
3. On **Takes**, click the row to listen privately or **Send** to put that saved WAV into the call. Use **Stop Sending** to end the outgoing stream and clear waiting sends. Optional global Send and Stop Sending shortcuts start unbound in **Settings → Keys**. Push-to-talk uses your own game or Discord key.
4. For an 8 GB RTX 3070 Ti, start by preparing a few lines before launching the game. Choose **Free VRAM**, then try sending them in a private Discord call. Compare the same stretch of gameplay with the app closed and with saved lines sending. Then try one newly typed line and compare frame times and how long your friend waits to hear it.

The software checks use a recording endpoint and verify WAV replay with no resident model. The offline folder
includes all three models. Discord reception, cable levels, and gameplay with a model resident still need a trial on
the target PC. A new install frees an idle model after 10 minutes. An explicitly saved **Never** setting is preserved.
Speak now counts the actual model's input tokens before synthesis and refuses inputs over its limit. A take that
reaches the model's speech-duration cap stays local for preview. Long-script splitting, Rework and Save to Voice
are later items in the plan.

[Original and updated Emiya windows](ui-review.html) · [Portable Windows build](../portable.md) · [Windows and receiver setup](decisions.md#4-vb-cable-and-what-it-is-for)

## Where to find the controls

| Location | What you can change |
| --- | --- |
| Settings → Voice Chat | Enable sending, choose or refresh the output device, select the send mode, hear the line locally, wait or cut in when another send is playing, and choose a 0–3 second start delay. |
| Settings → Keys | Set the global Send and Stop Sending shortcuts. Both start unbound. |
| Settings → General | Choose whether stopping the current generation also drops queued takes. The default clears the queue. |
| Models → Behavior | Set warm-up, confirmation for model switches and how long a model stays loaded when idle. |
| Settings → Effects | Turn Background Effects on or off, adjust their speed, toggle Work Glow and set Reduce Motion. Work Glow intensifies the background embers during generation, forging and model loads. |

Save to Voice for delivery, Rework and its script-replacement preference, and automatic long-script splitting
are still planned. The temperature control is pending a listening comparison. [Decisions and build order](decisions.md)

## What the Speak checks cover

The windowless suite exercises the page controls, routing and recovery with a recording output. It checks
an indexed WAV after a restart with its source voice removed, explicit sends from both take lists, delayed
and interrupted sends, and exactly one audible monitor with two clients. All three bundled models also
completed offline CPU generation through the packaged engine. The build was relocated and launched from
outside the checkout. [Portable verification](../../artifacts/packaging/verification.json)

The private Windows test used the actual Speak call service and a recording output. Outgoing audio advanced
while the window was covered or minimized. WebView2 paused the muted local monitor while minimized and
resynchronized within 69 ms on restore in that run. On the same private desktop, holding Enter activated Clear
once, and all four slider drags kept their selected value through release. [Native input and audio results](../../artifacts/speak/native-input-audio.json)

## What remains to measure

The earlier 4070 Ti runs measured about 2.65–3.04 GiB of PyTorch **reserved** memory on model load, plus
memory used by the driver, Windows and other programs. They also saw variation in background generation time.
Those figures cannot predict the frame-time cost on an 8 GB 3070 Ti while its game is using the GPU. Saved-line
replay with the model freed is the first target-PC check. Test live generation separately using the same game
settings and a repeatable section of play. [GPU timing investigation](../../artifacts/reliability/followup/gpu-diagnostic/findings.md)

The remaining Phase 0 measurements include the dedicated GPU memory audit, short-line coverage across all
models, and listening comparisons for the voice and Guidance settings. The synthetic output tests cannot measure
Discord's speech processing or the audible cutoff at the receiving end. [Portable test evidence](../../artifacts/packaging/verification.json)

| Page | What it answers |
|------|-----------------|
| [decisions.md](decisions.md) | **The authority:** the 16 decisions, defaults, design and build order |
| [how-speak-works.md](how-speak-works.md) | How a script becomes a played take today, what is stored where, a take's limits |
| [implementation-plan.md](implementation-plan.md) | The work by phase (0 to 5) and item (1.4, P1, L1, 5.1 …) |
| [failure-modes.md](failure-modes.md) | The audit: VRAM, releases, spam and fast clicks (F1 to F16), and the call failures (F17 to F23) |

New here? Read the pages in this order.

## November 13 baseline

Speak turns a typed script into a take in a voice you picked or forged. Nothing is live: no
microphone input. A **take** is one generated clip, saved as a WAV. **Guidance**, the pacing control,
is `cfg_weight` in Chatterbox; **Expressiveness** is `exaggeration`. The **fake engine** is a CPU
stand-in for Chatterbox that the tests use. **The window** is the app's desktop window.

**Built:** picking or forging a voice, typing, generating, autoplaying and replaying takes. Tested
on the fake engine only, not yet with the real models in the window (0.3, 0.4). Replay is exact, so
no seed is planned.

**Decided, not built**, in build order ([decisions.md](decisions.md), section 7). Items 1.3, 1.5
and 1.6 are built only if the GPU audit (0.1) shows they matter.

| Change | Plan |
|--------|------|
| Settings save only what you changed; idle unload after 10 idle minutes (today: never) | 1.10, 1.11 |
| "Click again" counts only after a pause of about half a second, also on buttons that change meaning in place | 1.7, 1.8 |
| Refuse a script that is too long; report a GPU fault | 1.1, 1.2 |
| Stop the running take | 1.4 |
| Record rules: new take fields read leniently, new request fields frozen | 1.12 |
| Slider fix, only if 0.5 confirms F14 | 1.9 |
| Save to Voice: the voice keeps its tuned Expressiveness and Guidance, also in `cli.py` and `app.py` | phase 2 |
| Rework a take | phase 4 |
| Speech into calls: off until you turn it on; by default you hear a take before you send it | phase 5 |
| Long scripts spoken in parts, joined into one take | phase 3 |
| One consent switch: "This is my own voice, or the speaker agreed to having it cloned" | 1.13 |

**VB-CABLE**, a virtual audio cable free for personal use, carries the speech into a call. You
install it yourself, as administrator, then reboot (decisions.md, section 4).

- **One finding can break a session.** A script over 2,048 text tokens (the units the model reads:
  about 3,400 English or 1,460 Japanese characters) fails inside the model. On the GPU it is expected
  to need a restart; not yet measured (0.2). The guard is 1.1; phase 3 removes the cause.
- **VRAM.** A model's weights are 2.78 to 2.99 GiB on a 12 GiB card; other programs used 4,005 MiB
  with no game running. Two models are never loaded together. The app may hold VRAM it does not
  need; 0.1 measures it.
- **Releases.** Every release path is sound, except after a GPU fault (F1, item 1.2). A spam test
  of the real engine code, with stand-in models instead of Chatterbox (CPU), started 3,373 loads
  with 277 injected failures; every loaded model was released, never two alive. A real-model run on
  2026-11-12 freed about 3.5 GiB per unload.
- **Spam.** The same test made 12,800 random calls from up to 16 threads: no unexpected error, no
  hang. It ran before today's engine edit and has not been run again. Fast clicks
  can misfire: a double-click passes every "click again" (F13, fix 1.7), a model card's Unload
  becomes Load in place (F15, fix 1.8), a slider may lose a drag (F14, untried; 1.9).
- **Upgrading Chatterbox gives nothing.** Upstream was retired in August 2026.

## Evidence recorded November 13

| Kind | State |
|------|-------|
| Source review: engine, job slot, release and quit paths, Speak flow | Done |
| Token-limit measurement and the spam test (both CPU) | Done; the spam test passed, before today's engine edit; not re-run |
| Research (Chatterbox, PyTorch memory, NVIDIA driver, VB-CABLE) and the decision workflow | Done |
| Decision record, key code claims re-checked at 12:10 | Written; approving it is build step 0 |
| GPU audit: VRAM, releases, spam | Script written; not run (0.1) |
| Speaking rate, decode and reload time | Not done (0.1) |
| One-word and over-limit scripts | Not done (0.2) |
| Real engine in the window, an audible take, Guidance 0.30 vs 0.50 by ear | Not done (0.3, 0.4) |
| Window checks: slider jump (F14), a held Enter on a focused button | Not done (0.5) |
| Mockup round for every visible change | Not done (build step 2) |
| VB-CABLE install, then the device check | Not done; you install it, then 5.1 |
