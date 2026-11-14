// Frame cadence of the forge field's FieldRunner and of the take wave's loops, on fake clocks (§8.6).
//
// A small discrete-event browser stands in for WebView2: a vsync at 60, 144 or 280 Hz (animation-frame
// callbacks run at the next vsync, with its timestamp), timers that fire 0.5-1.5 ms late, performance.now
// and Date.now on the same virtual clock (0.3 ms of work inside every callback), and just enough DOM for
// FieldRunner and WavePlayhead. Measured runs are 5 virtual seconds after a 1 s warm-up.
//
// Usage: node ui/themes/emiya/tests/runner_cadence.mjs   -> prints one JSON line; exit code 1 on any failure.
import { FieldRunner } from '../fx/emiya_forge_field.js';
import { FrameClock, PulseDriver, TraceDriver, WavePlayhead, circuit, timeText } from '../fx/emiya_take_wave.js';

const RATES = [60, 144, 280];
const WARMUP_MS = 1000;
const MEASURE_MS = 5000;
const CALLBACK_WORK_MS = 0.3;
// The nine forge-field tokens as the browser reports computed colours (the mockup's values).
const TOKENS = {
  '--emiya-fx-ember-1': 'rgb(158, 32, 40)',
  '--emiya-fx-ember-2': 'rgb(196, 62, 42)',
  '--emiya-fx-ember-3': 'rgb(222, 112, 48)',
  '--emiya-fx-ember-hot': 'rgb(252, 219, 127)',
  '--emiya-fx-trace-shadow': 'rgb(150, 30, 34)',
  '--emiya-fx-trace': 'rgb(172, 40, 44)',
  '--emiya-fx-trace-blade': 'rgb(200, 74, 50)',
  '--emiya-fx-glow': 'rgb(244, 150, 60)',
  '--emiya-fx-spark': 'rgb(252, 219, 127)',
};
const ANCHORS = { switch: [380, 620], gen: [900, 470], forge: [990, 560], 'forge-blade': [990, 660] };

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rect = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top });

// ---- the fake browser --------------------------------------------------------------------------

class FakeBrowser {
  constructor(hz, seed) {
    this.vsync = 1000 / hz;
    this.t = 0;
    this.offset = 0;
    this.timers = new Map();          // id -> { due, fn, every }
    this.frames = new Map();          // id -> callback
    this.nextId = 1;
    this.rafCallbacks = 0;
    this.timeoutsFired = 0;
    this.rand = mulberry32(seed || 1);
    this.docListeners = new Map();    // type -> Set
    this.winListeners = new Map();
    this.queries = [];
    this.reduceMotion = false;
    this.visibilityState = 'visible';
    this.elements = new Map();
    this.audio = null;
    this.field = { getBoundingClientRect: () => rect(176, 64, 1280, 800) };
    this.anchors = Object.entries(ANCHORS).map(([name, [x, y]]) => ({
      classList: ['emiya-anchor', 'emiya-anchor-' + name], getBoundingClientRect: () => rect(x, y, x, y),
    }));
  }

  install() {
    const b = this;
    globalThis.requestAnimationFrame = (fn) => { const id = b.nextId++; b.frames.set(id, fn); return id; };
    globalThis.cancelAnimationFrame = (id) => { b.frames.delete(id); };
    globalThis.setTimeout = (fn, ms) => b.addTimer(fn, ms, 0);
    globalThis.clearTimeout = (id) => { b.timers.delete(id); };
    globalThis.setInterval = (fn, ms) => b.addTimer(fn, ms, Math.max(1, Number(ms) || 0));
    globalThis.clearInterval = (id) => { b.timers.delete(id); };
    Object.defineProperty(globalThis, 'performance', { value: { now: () => b.now() }, configurable: true, writable: true });
    Date.now = () => 1700000000000 + Math.floor(b.now());
    globalThis.window = {
      devicePixelRatio: 1,
      matchMedia: (query) => b.matchMedia(query),
      addEventListener: (type, fn) => b.listen(b.winListeners, type, fn),
      removeEventListener: (type, fn) => b.unlisten(b.winListeners, type, fn),
    };
    globalThis.document = {
      get visibilityState() { return b.visibilityState; },
      querySelector: (sel) => (sel === '.emiya-field' ? b.field : sel === 'audio' ? b.audio : null),
      querySelectorAll: (sel) => (sel.includes('emiya-anchor-') ? b.anchors : []),
      getElementById: (id) => b.elements.get(id) || null,
      addEventListener: (type, fn) => b.listen(b.docListeners, type, fn),
      removeEventListener: (type, fn) => b.unlisten(b.docListeners, type, fn),
      dispatchEvent: event => { for (const fn of b.docListeners.get(event.type) || []) fn(event); },
    };
    globalThis.getComputedStyle = (el) => el.computed();
    globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  }

  now() { return this.t + this.offset; }

  addTimer(fn, ms, every) {
    const id = this.nextId++;
    this.timers.set(id, { due: this.now() + Math.max(0, Number(ms) || 0) + 0.5 + this.rand(), fn, every });
    return id;
  }

  matchMedia(query) {
    const b = this;
    const mq = {
      listeners: new Set(),
      get matches() { return query.includes('reduced-motion') ? b.reduceMotion : false; },
      addEventListener(type, fn) { this.listeners.add(fn); },
      removeEventListener(type, fn) { this.listeners.delete(fn); },
      reduced: query.includes('reduced-motion'),
    };
    this.queries.push(mq);
    return mq;
  }

  listen(map, type, fn) {
    if (!map.has(type)) map.set(type, new Set());
    map.get(type).add(fn);
  }

  unlisten(map, type, fn) {
    if (map.has(type)) map.get(type).delete(fn);
  }

  listeners(map) {
    let n = 0;
    for (const set of map.values()) n += set.size;
    return n;
  }

  queryListeners() {
    return this.queries.reduce((n, q) => n + q.listeners.size, 0);
  }

  dispatch(type, target) {
    for (const fn of [...(this.docListeners.get(type) || [])]) this.call(() => fn({ type, target }));
  }

  setReduceMotion(on) {
    this.reduceMotion = on;
    for (const q of this.queries) if (q.reduced) for (const fn of [...q.listeners]) this.call(() => fn({ matches: on }));
  }

  setVisibility(state) {
    this.visibilityState = state;
    this.dispatch('visibilitychange', globalThis.document);
  }

  pending() {
    let timeouts = 0;
    let intervals = 0;
    for (const tm of this.timers.values()) if (tm.every) intervals += 1; else timeouts += 1;
    return { timeouts, intervals, frames: this.frames.size };
  }

  call(fn) {
    this.offset = CALLBACK_WORK_MS;
    try { fn(); } finally { this.offset = 0; }
  }

  runFor(ms) {
    const end = this.t + ms;
    for (;;) {
      let next = null;
      for (const [id, tm] of this.timers) if (!next || tm.due < next.tm.due) next = { id, tm };
      const vsync = this.frames.size ? (Math.floor(this.t / this.vsync + 1e-9) + 1) * this.vsync : Infinity;
      const due = next ? next.tm.due : Infinity;
      if (Math.min(due, vsync) > end) {
        this.t = end;
        return;
      }
      if (due <= vsync) {
        this.t = Math.max(this.t, due);
        if (next.tm.every) next.tm.due += next.tm.every;
        else { this.timers.delete(next.id); this.timeoutsFired += 1; }
        this.call(next.tm.fn);
      } else {
        this.t = vsync;
        const batch = [...this.frames.values()];
        this.frames.clear();
        for (const fn of batch) {
          this.rafCallbacks += 1;
          this.call(() => fn(vsync));
        }
      }
    }
  }

  // Callbacks and the counter's growth per second over ms.
  rate(ms, counter) {
    const raf0 = this.rafCallbacks;
    const c0 = counter();
    this.runFor(ms);
    return { raf_per_s: round1(((this.rafCallbacks - raf0) * 1000) / ms), per_s: round1(((counter() - c0) * 1000) / ms) };
  }
}

const round1 = (v) => Math.round(v * 10) / 10;

// ---- the forge field ---------------------------------------------------------------------------

class CountingContext {
  constructor() { this.draws = 0; this.lineCap = 'butt'; this.lineJoin = 'miter'; this.lineWidth = 1; this.fillStyle = null; this.strokeStyle = null; }
  clearRect() { this.draws += 1; }
  setTransform() {}
  beginPath() {}
  moveTo() {}
  lineTo() {}
  arc() {}
  fill() {}
  stroke() {}
  createRadialGradient() { return { addColorStop() {} }; }
}

function fieldState(over) {
  return { seq: 1, heat: 'idle', focus: null, glow: true, speed: 1, reduce: 'system', fps_cap: 60, paused: false, ...over };
}

function mountField(b, state, options) {
  const ctx = new CountingContext();
  const canvas = { width: 0, height: 0, style: { color: '' }, getContext: () => ctx };
  canvas.computed = () => ({ color: canvas.style.color });
  const root = { getBoundingClientRect: () => rect(0, 0, 1280, 800), computed: () => ({ getPropertyValue: (n) => TOKENS[n] || '' }) };
  const emits = [];
  const runner = new FieldRunner(root, canvas, { uid: 'fx', field: '.emiya-field', ...options }, (name) => emits.push(name));
  runner.mount(state);
  return { runner, ctx, emits };
}

function newBrowser(hz, seed) {
  const b = new FakeBrowser(hz, seed);
  b.install();
  Math.random = mulberry32(1000 + hz);
  return b;
}

const failures = [];
const report = { field: { cap60: {}, cap0: {}, working: {} }, clock: { fps60: {}, fps0: {} } };

function check(name, ok, detail) {
  if (!ok) failures.push({ name, detail: detail === undefined ? null : detail });
}

// Cap 60 and cap 0 at every refresh rate.
for (const hz of RATES) {
  for (const [key, over] of [['cap60', {}], ['cap0', { fps_cap: 0 }], ['working', { heat: 'working', focus: 'gen' }]]) {
    const b = newBrowser(hz, hz);
    const f = mountField(b, fieldState(over));
    b.runFor(WARMUP_MS);
    const wakes0 = b.timeoutsFired;
    const r = b.rate(MEASURE_MS, () => f.ctx.draws);
    const row = { raf_per_s: r.raf_per_s, draws_per_s: r.per_s, timer_wakes_per_s: round1(((b.timeoutsFired - wakes0) * 1000) / MEASURE_MS) };
    report.field[key][hz] = row;
    if (key === 'cap0') {
      check('cap 0 draws on every vsync at ' + hz + ' Hz', Math.abs(row.draws_per_s - hz) <= 1, row);
    } else {
      check(key + ': at most 62 callbacks a second at ' + hz + ' Hz', row.raf_per_s <= 62, row);
      check(key + ': 59-61 draws a second at ' + hz + ' Hz', row.draws_per_s >= 59 && row.draws_per_s <= 61, row);
    }
  }
}

// Reduce Motion: one pre-rolled still frame, then nothing scheduled.
{
  const b = newBrowser(144, 7);
  const f = mountField(b, fieldState({ reduce: true }));
  const afterMount = f.ctx.draws;
  const r = b.rate(MEASURE_MS, () => f.ctx.draws);
  const live = newBrowser(280, 8);
  const g = mountField(live, fieldState());
  live.runFor(WARMUP_MS);
  const beforeStill = g.ctx.draws;
  g.runner.receive(fieldState({ seq: 2, reduce: true }));
  const stillDraws = g.ctx.draws - beforeStill;
  const r2 = live.rate(MEASURE_MS, () => g.ctx.draws);
  const sys = newBrowser(144, 9);
  const h = mountField(sys, fieldState({ reduce: 'system' }));
  sys.runFor(WARMUP_MS);
  sys.setReduceMotion(true);
  const r3 = sys.rate(MEASURE_MS, () => h.ctx.draws);
  sys.setReduceMotion(false);
  const r4 = sys.rate(MEASURE_MS, () => h.ctx.draws);
  report.field.reduce = {
    at_mount: { still_draws: afterMount, raf_per_s: r.raf_per_s, draws_per_s: r.per_s, pending: b.pending(), still: f.runner.still },
    while_running: { still_draws: stillDraws, raf_per_s: r2.raf_per_s, draws_per_s: r2.per_s, pending: live.pending(), still: g.runner.still },
    system: { reduced_raf_per_s: r3.raf_per_s, resumed_draws_per_s: r4.per_s },
  };
  check('reduce at mount draws exactly one still frame', afterMount === 1 && f.runner.still === true && f.runner.running === false, report.field.reduce.at_mount);
  check('reduce at mount: no callbacks and no draws afterwards', r.raf_per_s === 0 && r.per_s === 0, r);
  check('reduce at mount: nothing scheduled but the 1 s refresh', b.pending().timeouts === 0 && b.pending().frames === 0 && b.pending().intervals === 1, b.pending());
  check('reduce while running: one still frame, then no callbacks', stillDraws === 1 && r2.raf_per_s === 0 && r2.per_s === 0
    && g.runner.still === true, report.field.reduce.while_running);
  check('reduce while running: no pending timer or frame', live.pending().timeouts === 0 && live.pending().frames === 0, live.pending());
  check('reduce "system" follows the media query', r3.raf_per_s === 0 && r4.per_s >= 59 && r4.per_s <= 61, report.field.reduce.system);
}

// Paused (minimized): no frames, no still frame; resuming restarts the capped loop.
{
  const b = newBrowser(144, 11);
  const f = mountField(b, fieldState({ paused: true }));
  const r = b.rate(MEASURE_MS, () => f.ctx.draws);
  f.runner.receive(fieldState({ seq: 2 }));
  const resumed = b.rate(MEASURE_MS, () => f.ctx.draws);
  f.runner.receive(fieldState({ seq: 3, paused: true }));
  const pausedAgain = b.rate(MEASURE_MS, () => f.ctx.draws);
  const pendingWhilePaused = b.pending();
  const c = newBrowser(144, 12);
  const g = mountField(c, fieldState({ paused: true, reduce: true }));
  const pausedStill = g.ctx.draws;
  g.runner.receive(fieldState({ seq: 2, reduce: true }));
  const unpausedStill = g.ctx.draws;
  report.field.paused = {
    at_mount: { raf_per_s: r.raf_per_s, draws_per_s: r.per_s, still: f.runner.still },
    resumed_draws_per_s: resumed.per_s,
    paused_again: { raf_per_s: pausedAgain.raf_per_s, draws_per_s: pausedAgain.per_s, pending: pendingWhilePaused },
    reduce: { still_draws_while_paused: pausedStill, still_draws_after: unpausedStill },
  };
  check('paused at mount: 0 callbacks, 0 draws, no still frame', r.raf_per_s === 0 && r.per_s === 0 && f.runner.still === false,
    report.field.paused.at_mount);
  check('unpaused: 59-61 draws a second', resumed.per_s >= 59 && resumed.per_s <= 61, resumed);
  check('paused while running: 0 callbacks, nothing scheduled', pausedAgain.raf_per_s === 0 && pausedAgain.per_s === 0
    && pendingWhilePaused.timeouts === 0 && pendingWhilePaused.frames === 0, report.field.paused.paused_again);
  check('paused with Reduce Motion draws no still frame until resumed', pausedStill === 0 && unpausedStill === 1, report.field.paused.reduce);
}

// A hidden document stops the loop; visible again resumes it.
{
  const b = newBrowser(144, 13);
  const f = mountField(b, fieldState());
  b.runFor(WARMUP_MS);
  b.setVisibility('hidden');
  const hidden = b.rate(MEASURE_MS, () => f.ctx.draws);
  b.setVisibility('visible');
  const visible = b.rate(MEASURE_MS, () => f.ctx.draws);
  report.field.visibility = { hidden_raf_per_s: hidden.raf_per_s, visible_draws_per_s: visible.per_s };
  check('hidden document: no callbacks', hidden.raf_per_s === 0 && hidden.per_s === 0, hidden);
  check('visible again: 59-61 draws a second', visible.per_s >= 59 && visible.per_s <= 61, visible);
}

// stop() and dispose() leave nothing scheduled and no listener.
{
  const b = newBrowser(280, 14);
  const f = mountField(b, fieldState());
  b.runFor(WARMUP_MS);
  f.runner.stop();
  const afterStop = b.pending();
  const r = b.rate(MEASURE_MS, () => f.ctx.draws);
  f.runner.dispose();
  const afterDispose = b.pending();
  const listeners = { document: b.listeners(b.docListeners), window: b.listeners(b.winListeners), queries: b.queryListeners() };
  report.field.stop = { after_stop: afterStop, raf_per_s_after_stop: r.raf_per_s, after_dispose: afterDispose, listeners_after_dispose: listeners };
  check('stop() leaves no pending timer and no frame', afterStop.timeouts === 0 && afterStop.frames === 0 && r.raf_per_s === 0, report.field.stop);
  check('dispose() leaves nothing scheduled', afterDispose.timeouts === 0 && afterDispose.intervals === 0 && afterDispose.frames === 0, afterDispose);
  check('dispose() removes every listener', listeners.document === 0 && listeners.window === 0 && listeners.queries === 0, listeners);
}

// Telemetry only with options.telemetry; without it, no statistics pile up.
{
  const b = newBrowser(144, 15);
  const f = mountField(b, fieldState());
  b.runFor(WARMUP_MS + MEASURE_MS);
  f.runner.receive(fieldState({ seq: 2, heat: 'working', focus: 'gen' }));
  const quiet = { emits: [...new Set(f.emits)], window_listeners: b.listeners(b.winListeners), gaps_kept: f.runner.acc.gaps.length };
  const c = newBrowser(144, 16);
  const g = mountField(c, fieldState(), { telemetry: true });
  c.runFor(WARMUP_MS + MEASURE_MS);
  g.runner.receive(fieldState({ seq: 2 }));
  const loud = {
    telemetry: g.emits.filter((n) => n === 'telemetry').length, acks: g.emits.filter((n) => n === 'ack').length,
    window_listeners: c.listeners(c.winListeners),
  };
  g.runner.dispose();
  loud.window_listeners_after_dispose = c.listeners(c.winListeners);
  report.field.telemetry = { off: quiet, on: loud };
  check('telemetry off: only the ready event, no window error listeners, no statistics kept',
    quiet.emits.length === 1 && quiet.emits[0] === 'ready' && quiet.window_listeners === 0 && quiet.gaps_kept === 0, quiet);
  check('telemetry on: a report every second and an ack per state', loud.telemetry >= 5 && loud.telemetry <= 6 && loud.acks === 1
    && loud.window_listeners === 2 && loud.window_listeners_after_dispose === 0, loud);
}

// ---- the take wave's loops --------------------------------------------------------------------

for (const hz of RATES) {
  for (const [key, fps] of [['fps60', 60], ['fps0', 0]]) {
    const b = newBrowser(hz, 20 + hz);
    let ticks = 0;
    const clock = new FrameClock(fps, () => { ticks += 1; });
    clock.start();
    b.runFor(WARMUP_MS);
    const r = b.rate(MEASURE_MS, () => ticks);
    clock.stop();
    const row = { raf_per_s: r.raf_per_s, ticks_per_s: r.per_s, pending_after_stop: b.pending() };
    report.clock[key][hz] = row;
    if (fps) {
      check('FrameClock 60: at most 62 callbacks a second at ' + hz + ' Hz', row.raf_per_s <= 62, row);
      check('FrameClock 60: 59-61 ticks a second at ' + hz + ' Hz', row.ticks_per_s >= 59 && row.ticks_per_s <= 61, row);
    } else {
      check('FrameClock 0: a tick on every vsync at ' + hz + ' Hz', Math.abs(row.ticks_per_s - hz) <= 1, row);
    }
    check('FrameClock stop() leaves nothing scheduled at ' + hz + ' Hz', row.pending_after_stop.timeouts === 0 && row.pending_after_stop.frames === 0, row);
  }
}

// A compositor animation has no requestAnimationFrame callback in JS. This fake advances its
// clock only during play, and verifies the keyframe samples at different monitor rates.
class CompositorAnimation {
  constructor(browser, effect, elapsed = 0) {
    this.b = browser;
    this.effect = effect;
    this.elapsed = elapsed;
    this.origin = browser.now();
    this.playState = 'running';
    this.pauses = 0;
  }
  get currentTime() { return this.playState === 'running' ? this.elapsed + this.b.now() - this.origin : this.elapsed; }
  set currentTime(value) { this.elapsed = value; this.origin = this.b.now(); }
  pause() { this.elapsed = this.currentTime; this.playState = 'paused'; this.pauses++; }
  play() { this.origin = this.b.now(); this.playState = 'running'; }
  cancel() { this.playState = 'idle'; }
}

report.pulse = {};
for (const hz of RATES) {
  const b = newBrowser(hz, 40 + hz);
  let sampler;
  const timing = {duration:900, direction:'alternate', iterations:Infinity, easing:'linear'};
  const progress = t => (1 - Math.cos(Math.PI * t / 900)) / 2; // stand-in for browser ease-in-out
  globalThis.KeyframeEffect = class {
    constructor(target, frames, options) { this.options = options; }
    getComputedTiming() { return {progress:progress(sampler.currentTime)}; }
  };
  globalThis.Animation = class { constructor(effect) { this.effect=effect; sampler=this; } };
  const effect = {getTiming:()=>timing, getKeyframes:()=>[{opacity:'.6',easing:'ease-in-out'}]};
  const source = new CompositorAnimation(b, effect, 137);
  source.animationName='vtHead';
  let sampled, sampledOptions;
  const element = {getAnimations:()=>[source], animate(frames, options) {
    sampled=frames; sampledOptions=options;
    return new CompositorAnimation(b, {});
  }};
  const pulse = new PulseDriver(60);
  pulse.attach();
  pulse.set(element,true);
  const animation = pulse.animation;
  const initial=animation.currentTime;
  const alphaAt=t=>sampled.filter(k=>k.offset<=t/900+1e-9).at(-1).opacity;
  const points=Array.from({length:55},(_,i)=>i*900/54);
  const maxError=Math.max(...points.map(t=>Math.abs(alphaAt(t)-(1-.4*progress(t)))));
  const stepsPerSecond=(sampled.length-2)*1000/timing.duration;
  const visible=b.rate(1000,()=>0);
  pulse.set(element,true);
  const same=animation===pulse.animation;
  b.setVisibility('hidden');
  const held=animation.currentTime;
  const hidden=b.rate(1000,()=>0);
  const unchanged=animation.currentTime===held;
  b.setVisibility('visible');
  b.runFor(1000);
  const advance=animation.currentTime-held;
  const field=mountField(b,fieldState());
  field.runner.receive(fieldState({seq:2, paused:true}));
  const paused=animation.playState==='paused';
  field.runner.receive(fieldState({seq:3}));
  field.runner.dispose();
  const offPlays=animation.playState==='running';
  pulse.set(element,false);
  const reduced=animation.playState==='paused';
  pulse.detach();
  const row=report.pulse[hz]={steps_per_s:stepsPerSecond,visible_raf:visible.raf_per_s,hidden_raf:hidden.raf_per_s,
    keyframe_error:maxError,initial_phase:initial,held:unchanged,resumed_ms:advance,same,paused,off_plays:offPlays,
    reduced,canceled:animation.playState==='idle',listeners:b.listeners(b.docListeners),pending:b.pending()};
  check('pulse: compositor caps visible steps with exact sampled CSS curve at ' + hz + ' Hz',
    stepsPerSecond===60 && maxError<1e-6 && visible.raf_per_s===0 && sampledOptions.direction==='alternate'
    && sampler.effect.options.easing==='ease-in-out' && sampled.every((k,i)=>i===sampled.length-1 || k.easing==='steps(1, end)'),row);
  check('pulse: held phase, native minimize, effects-off, reduce and disposal at ' + hz + ' Hz',
    initial===137 && same && unchanged && advance>=999 && advance<=1001 && paused && offPlays && reduced
    && row.canceled && row.listeners===0 && row.pending.frames===0 && row.pending.timeouts===0,row);
}

report.clock.phase = {};
for (const hz of RATES) {
  const b = newBrowser(hz, 80 + hz);
  const field = mountField(b, fieldState());
  b.runFor(37);
  const clock = new FrameClock(60, () => {});
  clock.start();
  b.runFor(WARMUP_MS);
  const period = 1000 / 60;
  const errors = [field.runner.nextDue, clock.nextDue].map((due) => Math.abs(due / period - Math.round(due / period)));
  report.clock.phase[hz] = { field_due: field.runner.nextDue, wave_due: clock.nextDue, grid_error: Math.max(...errors) };
  check('field and wave share the absolute 60 Hz timer phase at ' + hz + ' Hz', Math.max(...errors) < 1e-8,
    report.clock.phase[hz]);
  field.runner.dispose();
  clock.stop();
}

class FakeAudio {
  constructor(b, src) {
    this.b = b;
    this.tagName = 'AUDIO';
    this.src = src;
    this.currentSrc = src;
    this.duration = 4.6;
    this.paused = true;
    this.ended = false;
    this.base = 0;
    this.since = 0;
  }
  get currentTime() {
    return this.paused ? this.base : Math.min(this.duration, this.base + (this.b.now() - this.since) / 1000);
  }
  set currentTime(v) {
    this.base = v;
    this.since = this.b.now();
  }
  play() {
    this.base = this.currentTime;
    this.since = this.b.now();
    this.paused = false;
    this.ended = false;
    this.b.dispatch('play', this);
  }
  pause() {
    this.base = this.currentTime;
    this.paused = true;
    this.b.dispatch('pause', this);
  }
  end() {
    this.base = this.duration;
    this.paused = true;
    this.ended = true;
    this.b.dispatch('ended', this);
  }
  contains(el) { return el === this; }
}

function scrubInput() {
  const attrs = {};
  return { value: '0', getAttribute: (k) => (k in attrs ? attrs[k] : null), setAttribute: (k, v) => { attrs[k] = String(v); } };
}

const parseClock = (text) => {
  const [m, s] = text.split(' / ')[0].split(':');
  return Number(m) * 60 + Number(s);
};

{
  const b = newBrowser(280, 30);
  const audio = new FakeAudio(b, '/media/take/7');
  b.audio = audio;
  const node = { nodeType: 3, nodeValue: '0:00.0 / 0:04.6' };
  b.elements.set('time', { childNodes: [{ nodeType: 1 }, node] });
  const view = { played: { style: {} }, playhead: { style: {} }, head: { style: {} }, scrub: scrubInput() };
  const ph = new WavePlayhead(view, { audioId: '', timeTarget: 'time', fps: 60, base: 'file:///app/' });
  ph.take = { id: 7, length_s: 4.6, src: '/media/take/7' };
  ph.attach();
  const idle = b.rate(WARMUP_MS, () => 0);
  const idleText = node.nodeValue;
  const idleWidth = view.played.style.width;
  audio.play();
  const playing = b.rate(2000, () => 0);
  const pos = audio.currentTime;
  const shown = parseClock(node.nodeValue);
  const width = parseFloat(view.played.style.width);
  audio.pause();
  node.nodeValue = 'set by Python';
  const paused = b.rate(WARMUP_MS, () => 0);
  const pausedText = node.nodeValue;
  audio.play();
  b.runFor(5000);
  const endedWidth = view.played.style.width;
  audio.end();
  const afterEnd = b.rate(WARMUP_MS, () => 0);
  audio.currentSrc = '/media/take/8';
  audio.src = '/media/take/8';
  audio.currentTime = 1;
  audio.play();
  const other = b.rate(WARMUP_MS, () => 0);
  const otherWidth = view.played.style.width;
  audio.pause();
  audio.currentSrc = '/media/take/7';
  audio.src = '/media/take/7';
  ph.scrubChange(2.3);
  const scrubbed = { audio: audio.currentTime, width: parseFloat(view.played.style.width), valuetext: view.scrub.getAttribute('aria-valuetext') };
  ph.detach();
  const detached = { listeners: b.listeners(b.docListeners), pending: b.pending() };
  report.playhead = {
    idle: { raf_per_s: idle.raf_per_s, text: idleText, width: idleWidth },
    playing: { raf_per_s: playing.raf_per_s, audio_s: round1(pos), shown_s: shown, width_pct: round1(width) },
    paused: { raf_per_s: paused.raf_per_s, text: pausedText },
    ended: { width: endedWidth, raf_per_s: afterEnd.raf_per_s },
    other_take: { raf_per_s: other.raf_per_s, width: otherWidth },
    scrubbed, detached,
  };
  check('playhead: no frames while the audio is paused', idle.raf_per_s === 0 && idleText === '0:00.0 / 0:04.6'
    && idleWidth === '0%', report.playhead.idle);
  check('playhead: capped frames while playing', playing.raf_per_s >= 55 && playing.raf_per_s <= 62, report.playhead.playing);
  check('playhead: the frame loop writes the time text while playing', Math.abs(shown - pos) <= 0.12
    && node !== null && Math.abs(width - (pos / 4.6) * 100) <= 1, report.playhead.playing);
  check('playhead: paused, it stops and leaves the text to Python', paused.raf_per_s === 0 && pausedText === 'set by Python', report.playhead.paused);
  check('playhead: at the end the played part is full and the loop stops', endedWidth === '100%' && afterEnd.raf_per_s === 0, report.playhead.ended);
  check('playhead: audio of another take moves nothing', other.raf_per_s === 0 && otherWidth === '0%', report.playhead.other_take);
  check('playhead: scrubbing seeks this take and updates the box', Math.abs(scrubbed.audio - 2.3) < 1e-9
    && Math.abs(scrubbed.width - (2.3 / 4.6) * 100) < 1e-6 && scrubbed.valuetext === timeText(2.3, 4.6), scrubbed);
  check('playhead: detach removes its listeners and loop', detached.listeners === 0 && detached.pending.timeouts === 0 && detached.pending.frames === 0, detached);
}

{
  const b = newBrowser(144, 40);
  const mine = { tagName: 'AUDIO', paused: false, ended: false, currentTime: 1, duration: 4.6, src: '/media/take/7', currentSrc: '/media/take/7', contains(el) { return el === this; } };
  const stranger = { ...mine, contains() { return false; } };
  b.elements.set('player', mine);
  const ph = new WavePlayhead({ played: { style: {} } }, { audioId: 'player', timeTarget: '', fps: 60, base: 'file:///app/' });
  ph.take = { id: 7, length_s: 4.6, src: '/media/take/7' };
  ph.attach();
  ph.clock.stop();
  ph.audio = null;
  b.dispatch('play', stranger);
  const ignored = ph.audio === null;
  b.dispatch('play', mine);
  const adopted = ph.audio === mine && ph.clock.running;
  ph.detach();
  check('playhead: with an audio id, only that Player counts', ignored && adopted, { ignored, adopted });
}

{
  const b = newBrowser(280, 50);
  const view = { paths: [{ style: {} }, { style: {} }], head: { style: {} } };
  const tr = new TraceDriver(circuit(692, 56), 60, view);
  tr.set(null, false);
  const r = b.rate(3000, () => 0);
  const offsets = [];
  for (let k = 0; k < 200; k++) {
    b.runFor(1000 / 60);
    offsets.push(Number(view.paths[0].style.strokeDashoffset));
  }
  tr.reset();
  tr.set(0.2, false);
  const first = view.paths[0].style.transition;
  tr.set(0.5, false);
  const grow = { offset: view.paths[0].style.strokeDashoffset, transition: view.paths[0].style.transition };
  tr.set(0.3, false);
  const shrink = view.paths[0].style.transition;
  const determinate = b.rate(1000, () => 0);
  tr.set(0.5, true);
  const reduced = { offset: view.paths[1].style.strokeDashoffset, head: view.head.style.display };
  const reducedRate = b.rate(1000, () => 0);
  tr.reset();
  report.trace = {
    loop: { raf_per_s: r.raf_per_s, min_offset: Math.min(...offsets), max_offset: Math.max(...offsets) },
    fill: { first, grow, shrink, raf_per_s: determinate.raf_per_s },
    reduced: { ...reduced, raf_per_s: reducedRate.raf_per_s }, pending_after_reset: b.pending(),
  };
  check('trace: the 3 s loop is capped', r.raf_per_s <= 62 && r.raf_per_s >= 55, report.trace.loop);
  check('trace: the loop sweeps the whole path', Math.min(...offsets) < 50 && Math.max(...offsets) > 950, report.trace.loop);
  check('trace: a fill reveals the path, eased only while it grows', first === 'none' && grow.offset === '500'
    && grow.transition.startsWith('stroke-dashoffset 0.2s') && shrink === 'none' && determinate.raf_per_s === 0, report.trace.fill);
  check('trace: Reduce Motion is a static full trace without its head', reduced.offset === '0' && reduced.head === 'none'
    && reducedRate.raf_per_s === 0, report.trace.reduced);
  check('trace: reset leaves nothing scheduled', b.pending().timeouts === 0 && b.pending().frames === 0, b.pending());
}

report.ok = failures.length === 0;
report.failures = failures;
console.log(JSON.stringify(report));
process.exit(report.ok ? 0 : 1);
