# Voice Tool reliability report

13 November 2026, America/Chicago.

The focus fix and lag investigation are complete. Final functional, slow, soak, GPU, scale and visual checks passed. **Native verification remains red on one acceptance target: idle CPU was 36.65% of one logical CPU, above the 14.7% limit.** The final native run recorded zero focus incidents. A hard kill between conditioning and registry commits also remains a cross-file recovery boundary.

## Final verification

Source was frozen before the last runs. All **232 application, asset, test and configuration hashes remained unchanged**. [Final validation summary](../artifacts/reliability/2026-11-13/validation_summary.json). Tests used offline model files and isolated data roots on Windows, Python 3.11.9, NiceGUI 3.19.0, pywebview 6.2.1, Torch 2.6.0+cu124 and filelock 3.32.3. Native checks used the available 1920 by 1080 display at 144 Hz and 96 DPI, with a 1280 by 800 app client. GPU checks used the NVIDIA GeForce RTX 4070 Ti.

| Suite | Final result | Pytest time | Evidence |
|---|---|---|---|
| Default | 3,131 passed, 1 skipped | 9m 34.73s | [Log](../artifacts/reliability/2026-11-13/verification/default_frozen.txt) |
| Slow | 13 passed | 19.72s | [Log](../artifacts/reliability/2026-11-13/verification/slow_frozen.txt) |
| Non-native soak | 644 passed | 14m 51.37s | [Log and hash audit](../artifacts/reliability/2026-11-13/verification/soak_frozen_status.json) |
| Full-scale bench | 25 passed across five repetitions; every budget and target held | 2m 30.11s | [Bench report](../artifacts/reliability/2026-11-13/bench-frozen/bench.md) |
| Real-engine E2E and GPU soak | 2 passed | 4m 8.95s | [Log](../artifacts/reliability/2026-11-13/gpu-frozen/gpu_final.txt) |
| Native | 18 passed, **1 failed: N5 CPU** | 9m 23.20s | [Results](../artifacts/reliability/2026-11-13/native-frozen/results.txt) |

The skip is the FakeEngine hard-link case when its shared cache and scratch data are on different volumes; plain copies are used. Marker suites have separate scopes and should not be summed as a single test count. Final verification ran concurrently on separate scratch roots; timings describe this run rather than an idle-machine performance baseline.

The default and soak suites include the reliability regressions for corrupt persistence, ordered star/purge edits, capped and keyed lists, fault recovery, queue limits, named model loads, CLI/GUI contention, per-file writer termination, and the permanent second-theme boot probe. The long soak covers all 29 scenarios and six routes, plus the 3,000-action memory/session checks. [Source manifest](../artifacts/reliability/2026-11-13/verification/frozen_source_before.json).

## Focus and native behavior

`focus=False` and a handler that returned focus after activation were insufficient. Test-window mode now installs a **GUI-thread CBT hook before the WinForms constructor**, vetoing activation and gained keyboard focus before Windows applies them. The callback stays alive until close and is released if construction fails. Normal app windows retain their normal focus behavior. The fallback guard checks actual foreground ownership, because WinForms can emit an Activated notification even after Windows activation was vetoed. The monitor closes an activating test window immediately and blocks subsequent launches. [CBT operation](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/ms644977%28v%3Dvs.85%29).

The frozen run completed 25 native window launches and a separate browser-only binding probe, with **zero observed focus incidents and zero guard interventions**. Exploratory failures are retained in the artifact folders and are not counted as passing verification.

- Ten launch/quit cycles passed; Close-to-exit maximum was 1.625s, mean 1.461s.
- Ten minimize/restore cycles passed, with zero activation and zero field frames while minimized; the field resumed every time.
- Three hundred route changes averaged 61.3ms, maximum 134.1ms, with zero JavaScript errors and no steady DOM/heap growth in the samples.
- The five-minute held-generation/effects run passed: 11 samples, constant 601 DOM nodes, heap cycling between 11.63 and 34.37MB, and zero JavaScript errors.
- All native windows exited normally; no owned processes or WebView2 profiles remained. The browser-only socket probe was intentionally terminated after its measurement period.
- Observed TCP endpoints were loopback-only. Second-launch exit 5, busy-close arming, tqdm, and drag-region checks passed.

Native checks also found two startup defects. NiceGUI's asynchronous icon writer could overwrite the correctly sized icons with a scaled 256px frame; sizing now runs after that writer, yielding 0.0 distance for both 32px and 16px frames. Python's first Windows-version query could create a hidden `cmd /c ver` console host; cache priming now runs that query with `CREATE_NO_WINDOW`, then restores the temporary wrapper. The final console count was zero. [Startup evidence](../artifacts/reliability/2026-11-13/desktop_boot_review.md).

## Lag and real models

The original 700/763/763ms spikes came from the synchronous external VRAM measurement helper: three `nvidia-smi` queries and two 300ms sleeps. One historical spike preceded opening the app. The helper reproduced 739.07ms event-loop lag; its async worker wrapper reduced this to 14.65ms for the same roughly 0.73s query. Permanent regressions verify that the event loop keeps progressing. No application runtime lag change was needed. [Causal measurements](../artifacts/reliability/2026-11-13/lag/summary.json).

On the frozen source, E2E completed three model loads and four takes. The GPU soak completed three rounds across all three models: nine loads and 27 takes. Maximum residency was one model and there were no invariant violations. Python-loop p95 was 13ms in both runs; maxima were 310ms and 341ms, with zero samples above 500ms. Reserved allocator memory after Free VRAM remained **60MiB in every round**, allocated memory after unload stayed at or below 24.68MiB, and the worst generation-time slowdown was 5.46%, within the 50% budget. The engine lock was released and no models remained resident. [Final GPU measurements and hashes](../artifacts/reliability/2026-11-13/gpu-frozen/summary.json).

## CPU finding

CPU accounting now uses kernel plus user time from `GetProcessTimes`, including exited descendants. Converting processor cycles through a calibrated frequency was unreliable; Microsoft explicitly cautions against that conversion. [Process time](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes), [cycle-counter limitation](https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-querythreadcycletime).

The paused waveform's CSS head-glow pulse contributed rendering work at the display refresh rate. It now samples the browser's original curve through the existing 60Hz clock, aligned with the field clock, and stops while hidden/reduced or unmounted. Geometry, keyframes, easing and assets remain unchanged. Cadence, phase and cleanup tests passed at simulated 60/144/280Hz. A more complex direct-opacity candidate provided no further measured benefit and was removed.

**The idle target is still unmet.** The frozen native run measured idle 36.65%, working 17.79%, Effects off 22.24%, Reduce Motion 1.55%, and minimized 5.85%, expressed as percentages of one logical CPU. Field cadence remained about 60Hz. Reduce Motion met its 3% target; idle exceeded 14.7%. The CPU test remains a real failing test, with no raised threshold or expected-failure mark. Further paint/compositor optimization is open. [Investigation](../artifacts/reliability/2026-11-13/performance_investigation.md).

## Visual review

All **22 frozen captures** were nonblank and 1280 by 800. They were reviewed against eight locally rendered original mockup boards and the build spec. No actionable clipping, overlap, missing controls/assets, or unintended layout change was found. Settings General/Effects/Keys/About, empty/error/busy/result states, model-switch warnings and Reduce Motion were included. Real waveforms, dates, history, estimates, approved behavior changes and animation phases explain expected differences; this is not a claim of pixel identity. [Review](../artifacts/reliability/2026-11-13/visual_frozen_review.md), [side-by-side gallery](../artifacts/reliability/2026-11-13/visual_gallery.html).

## Data safety and boundaries

A failed registry save could previously leave newly baked conditioning that a later load silently reused for a different reference clip. The shared real/fake-engine rollback now removes a failed first registration's new `.pt`, or restores a same-directory backup after a failed replacement, then invalidates active conditioning. A bake that created no output performs no unnecessary removal. Real/fake regressions and a genuine Windows ACL-denied scratch-folder test passed. A failed rollback preserves recovery evidence rather than silently deleting the backup. [Audit](../artifacts/reliability/2026-11-13/audit.md).

All four pre-existing persistent project-data files have identical before/after SHA-256 values. Transient engine lock/owner records are excluded from that comparison. Real deletion checks operated on scratch voices/takes and exercised Recycle Bin calls. [Before](../artifacts/reliability/2026-11-13/data_before.json), [after](../artifacts/reliability/2026-11-13/data_after.json).

Per-file atomic writes and recovery from reported errors **do not create a durable transaction across conditioning and registry files**. A hard kill between their commits can still leave an orphan or mismatched metadata; the writer-kill tests establish per-file integrity, not cross-file atomicity. A durable journal is separate work.

Native paint checks used FakeEngine; real-model tests drove the full composition through NiceGUI's headless user fixture. They measure Python-loop lag rather than native paint during real inference. The real GPU long takes were 29.56–30.36s, so near-40s cap coverage and transcript completeness are not established. Physical mouse/keyboard/audio behavior, another display's DPI/refresh behavior, and absolute absence of every future crash or leak are outside these results.
