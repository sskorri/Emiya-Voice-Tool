// Node test for ui/kit/vt_player.js: the Player reconciles to PlayerTarget against a fake <audio>.
//
// Covers: new src loads and seeks to `at` once the metadata is in; the same src with `at` seeks;
// then play or pause; an applied seq changes nothing; A/B swap-and-seek; clearing; reload after a
// failed load; state reports at most 4 a second (newest state last), stamped with the target's seq;
// play() refusals and media errors; and the Vue component's mount, watch and unmount.
//
// Usage: node tests/kit/vt_player.mjs  -> one JSON line {"ok", "passed", "failed"}; exit code 1 on a failure.
import Component, { PlayerCore, STATE_INTERVAL_MS } from '../../ui/kit/vt_player.js';

const results = { passed: 0, failed: [] };

function check(name, condition, detail = '') {
  if (condition) results.passed += 1;
  else results.failed.push(detail ? `${name}: ${detail}` : name);
}

function equal(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  check(name, a === e, `got ${a}, expected ${e}`);
}

// A media element as far as the Player uses it. Events fire only when a test calls a helper,
// in the order a browser fires them; play() resolves (or rejects with `refusal`) at once.
class FakeAudio {
  constructor() {
    this.listeners = new Map();
    this.calls = [];
    this.attributeSrc = '';
    this.refusal = null;
    this.reset();
  }

  reset() {
    this.paused = true;
    this.ended = false;
    this.currentTime = 0;
    this.duration = NaN;
    this.readyState = 0;
    this.error = null;
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }

  removeEventListener(type, fn) {
    if (this.listeners.has(type)) this.listeners.get(type).delete(fn);
  }

  listenerCount() {
    let count = 0;
    for (const set of this.listeners.values()) count += set.size;
    return count;
  }

  fire(type) {
    for (const fn of this.listeners.get(type) || []) fn({ type, target: this });
  }

  set src(value) {
    this.attributeSrc = value;
    this.calls.push(['src', value]);
    this.reset();
  }

  get src() {
    return this.attributeSrc;
  }

  removeAttribute(name) {
    this.calls.push(['removeAttribute', name]);
    if (name === 'src') this.attributeSrc = '';
  }

  load() {
    this.calls.push(['load']);
    this.reset();
  }

  play() {
    this.calls.push(['play']);
    if (this.refusal) return Promise.reject(this.refusal);
    this.paused = false;
    this.ended = false;
    return Promise.resolve();
  }

  pause() {
    this.calls.push(['pause']);
    this.paused = true;
  }

  // ---- browser-side happenings ----
  metadata(duration) {
    this.duration = duration;
    this.readyState = 1;
    this.fire('loadedmetadata');
  }

  seekTo(seconds) {
    this.currentTime = seconds;
    this.fire('seeked');
  }

  advance(seconds) {
    this.currentTime += seconds;
    this.fire('timeupdate');
  }

  finish() {
    this.currentTime = this.duration;
    this.paused = true;
    this.ended = true;
    this.fire('pause');
    this.fire('ended');
  }

  fail(code, message = '') {
    this.error = { code, message };
    this.fire('error');
  }
}

// A manual clock: setTimeout callbacks run when the test advances the time past them.
class FakeClock {
  constructor() {
    this.time = 1000;
    this.timers = [];
    this.nextId = 1;
  }

  now() {
    return this.time;
  }

  setTimeout(fn, ms) {
    const id = this.nextId++;
    this.timers.push({ id, at: this.time + ms, fn });
    return id;
  }

  clearTimeout(id) {
    this.timers = this.timers.filter((timer) => timer.id !== id);
  }

  advance(ms) {
    const end = this.time + ms;
    for (;;) {
      this.timers.sort((a, b) => a.at - b.at);
      const next = this.timers[0];
      if (!next || next.at > end) break;
      this.timers.shift();
      this.time = next.at;
      next.fn();
    }
    this.time = end;
  }
}

function player() {
  const audio = new FakeAudio();
  const clock = new FakeClock();
  const sent = [];
  const core = new PlayerCore(audio, (state) => sent.push({ ...state, t: clock.now() }), clock);
  return { audio, clock, sent, core };
}

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0));
const TAKE7 = '/media/take/7';
const TAKE5 = '/media/take/5';

{
  const { audio, core } = player();
  core.apply({ seq: 1, src: TAKE7, playing: true, at: 0, muted: true });
  check('a muted outgoing monitor still plays', audio.muted && !audio.paused);
  core.apply({ seq: 2, src: TAKE5, playing: true, at: 0 });
  check('an explicit local preview is audible', !audio.muted && !audio.paused);
}

// ---- new src: load, seek to `at` after the metadata, then play -------------------------------------
{
  const { audio, core } = player();
  check('a new target is applied', core.apply({ seq: 1, src: TAKE7, playing: true, at: null }));
  equal('new src loads and plays', audio.calls, [['src', TAKE7], ['play']]);
  audio.metadata(4.6);
  equal('no `at` keeps the start', audio.currentTime, 0);

  audio.calls = [];
  core.apply({ seq: 2, src: TAKE5, playing: true, at: 1.5 });
  equal('new src with at: load and play first', audio.calls, [['src', TAKE5], ['play']]);
  equal('the seek waits for the metadata', audio.currentTime, 0);
  audio.metadata(3.2);
  equal('new src seeks to at once the metadata is in', audio.currentTime, 1.5);

  core.apply({ seq: 3, src: TAKE7, playing: false, at: 9 });
  audio.metadata(4.6);
  equal('at beyond the end is clamped to the duration', audio.currentTime, 4.6);
  equal('playing false pauses', audio.calls.at(-1), ['pause']);
}

// ---- same src: seek when `at` is set; then play or pause; a seq in force changes nothing ------------
{
  const { audio, core } = player();
  core.apply({ seq: 1, src: TAKE7, playing: true, at: null });
  audio.metadata(4.6);
  audio.calls = [];
  core.apply({ seq: 2, src: TAKE7, playing: true, at: 2.25 });
  equal('same src with at seeks at once (no reload)', [audio.calls, audio.currentTime], [[['play']], 2.25]);
  audio.calls = [];
  core.apply({ seq: 3, src: TAKE7, playing: false, at: null });
  equal('same src, at null: only pause', [audio.calls, audio.currentTime], [[['pause']], 2.25]);
  audio.calls = [];
  check('an applied seq is refused', !core.apply({ seq: 3, src: TAKE5, playing: true, at: 0 }));
  equal('an applied seq changes nothing', audio.calls, []);
  check('a missing target is refused', !core.apply(null));
  audio.calls = [];
  core.apply({ seq: 4, src: TAKE7, playing: true, at: 0 });
  equal('restart from 0 at the end: seek 0, play', [audio.calls, audio.currentTime], [[['play']], 0]);
}

// ---- A/B swap-and-seek ----------------------------------------------------------------------------------
{
  const { audio, core } = player();
  core.apply({ seq: 1, src: TAKE5, playing: true, at: null });
  audio.metadata(3.2);
  audio.advance(1.9);
  audio.calls = [];
  core.apply({ seq: 2, src: TAKE7, playing: true, at: 1.9 });
  audio.metadata(4.6);
  equal('A/B: the other take loads, keeps the position and plays',
    [audio.calls, audio.currentTime, audio.paused], [[['src', TAKE7], ['play']], 1.9, false]);
  audio.calls = [];
  core.apply({ seq: 3, src: TAKE5, playing: false, at: 1.9 });
  audio.metadata(3.2);
  equal('A/B while paused stays paused', [audio.calls, audio.currentTime, audio.paused],
    [[['src', TAKE5], ['pause']], 1.9, true]);
}

// ---- clearing the source, and loading again after a failure ---------------------------------------------
{
  const { audio, core } = player();
  core.apply({ seq: 1, src: TAKE7, playing: true, at: null });
  audio.calls = [];
  core.apply({ seq: 2, src: '', playing: true, at: 3 });
  equal('an empty src unloads and never plays', audio.calls, [['removeAttribute', 'src'], ['load']]);
  equal('an empty src drops a pending seek', core.pendingSeek, null);

  core.apply({ seq: 3, src: TAKE7, playing: true, at: null });
  audio.fail(4);
  audio.calls = [];
  core.apply({ seq: 4, src: TAKE7, playing: true, at: 1 });
  equal('a failed src loads again when a newer target names it', audio.calls, [['load'], ['play']]);
  audio.metadata(4.6);
  equal('and seeks once the metadata is in', audio.currentTime, 1);
}

// ---- state reports: at most 4 a second, newest state last, stamped with the seq -------------------------
{
  const { audio, clock, sent, core } = player();
  core.apply({ seq: 5, src: TAKE7, playing: true, at: null });
  audio.fire('play');
  equal('the first report goes at once', sent.length, 1);
  audio.metadata(10);
  for (let i = 0; i < 40; i += 1) {
    clock.advance(25);
    audio.advance(0.025);
  }
  clock.advance(STATE_INTERVAL_MS);
  const times = sent.map((state) => state.t);
  const gaps = times.slice(1).map((t, i) => t - times[i]);
  check('reports are at least 250 ms apart', gaps.every((gap) => gap >= STATE_INTERVAL_MS), JSON.stringify(gaps));
  check('at most 4 reports in any second', times.every((t) => times.filter((u) => u >= t && u < t + 1000).length <= 4));
  check('a second of playback still reports', sent.length >= 4, `only ${sent.length}`);
  const last = sent.at(-1);
  equal('the trailing report has the newest position', last.position, 1);
  equal('reports carry the seq in force', [...new Set(sent.map((state) => state.seq))], [5]);
  equal('report fields', Object.keys(last).filter((key) => key !== 't'),
    ['seq', 'playing', 'position', 'duration', 'ended', 'error']);
  equal('a playing report', [last.playing, last.duration, last.ended, last.error], [true, 10, false, '']);

  const count = sent.length;
  clock.advance(1000);
  audio.fire('durationchange');
  clock.advance(1000);
  equal('an unchanged state is not sent again', sent.length, count);

  audio.finish();
  clock.advance(STATE_INTERVAL_MS);
  equal('the end is reported once', sent.length, count + 1);
  equal('an ended report', [sent.at(-1).playing, sent.at(-1).ended, sent.at(-1).position], [false, true, 10]);
}

// ---- errors: media errors and refused play() --------------------------------------------------------------
{
  const { audio, clock, sent, core } = player();
  core.apply({ seq: 1, src: TAKE7, playing: true, at: null });
  audio.fail(4);
  equal('a media error is reported by code', sent.at(-1).error, 'The audio format or file is not supported');
  clock.advance(1000);
  audio.error = { code: 3, message: 'DEMUXER_ERROR_COULD_NOT_OPEN' };
  audio.fire('error');
  equal('a media error message wins', sent.at(-1).error, 'DEMUXER_ERROR_COULD_NOT_OPEN');

  const second = player();
  second.audio.refusal = { name: 'NotAllowedError', message: 'play() needs a user gesture' };
  second.core.apply({ seq: 1, src: TAKE5, playing: true, at: null });
  await flushPromises();
  equal('a refused play() is reported', second.sent.at(-1).error, 'NotAllowedError: play() needs a user gesture');
  second.clock.advance(1000);
  second.audio.refusal = null;
  second.core.apply({ seq: 2, src: TAKE5, playing: true, at: null });
  second.audio.fire('play');
  equal('a later play clears the refusal', second.sent.at(-1).error, '');

  const third = player();
  third.audio.refusal = { name: 'AbortError', message: 'interrupted by a new load' };
  third.core.apply({ seq: 1, src: TAKE5, playing: true, at: null });
  await flushPromises();
  equal('an AbortError (a newer load or pause) is not an error', third.sent.length, 0);
}

// ---- teardown ------------------------------------------------------------------------------------------------
{
  const { audio, clock, sent, core } = player();
  core.apply({ seq: 1, src: TAKE7, playing: true, at: null });
  audio.fire('play');
  audio.advance(0.1);
  const before = sent.length;
  core.destroy();
  clock.advance(1000);
  equal('destroy cancels the pending report', sent.length, before);
  equal('destroy removes every listener', audio.listenerCount(), 0);
  check('destroy pauses (a detached <audio> would keep playing)', audio.paused);
}

// ---- the Vue component ---------------------------------------------------------------------------------------
{
  const audio = new FakeAudio();
  const emitted = [];
  const vm = {
    $el: audio,
    target: { seq: 1, src: TAKE7, playing: true, at: null },
    $emit: (name, payload) => emitted.push([name, payload]),
  };
  globalThis.window = { path_prefix: '/prefix' };
  Component.mounted.call(vm);
  equal('nothing is applied before the first tick', audio.calls, []);
  await flushPromises();
  equal('the first tick applies the target with the path prefix', audio.calls, [['src', `/prefix${TAKE7}`], ['play']]);
  audio.fire('play');
  equal('the component emits state', emitted.map(([name]) => name), ['state']);
  equal('the emitted state is stamped with the seq', emitted[0][1].seq, 1);
  Component.watch.target.call(vm, { seq: 2, src: TAKE7, playing: false, at: null });
  equal('the watch applies a new target', audio.calls.at(-1), ['pause']);
  Component.watch.target.call(vm, { seq: 2, src: TAKE7, playing: true, at: null });
  equal('a re-render with the same seq changes nothing', audio.calls.at(-1), ['pause']);
  Component.unmounted.call(vm);
  equal('unmounting removes the listeners', audio.listenerCount(), 0);
  check('the template is one hidden <audio> (no controls)', /^<audio preload="auto"><\/audio>$/.test(Component.template));
  delete globalThis.window;

  const early = new FakeAudio();
  const quick = { $el: early, target: { seq: 1, src: TAKE5, playing: true, at: null }, $emit: () => {} };
  Component.mounted.call(quick);
  Component.unmounted.call(quick);
  await flushPromises();
  equal('unmounting before the first tick applies nothing', early.calls, [['pause']]);
}

const ok = results.failed.length === 0;
console.log(JSON.stringify({ ok, passed: results.passed, failed: results.failed }));
process.exit(ok ? 0 : 1);
