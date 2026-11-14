// Emiya theme - the take waveform on Speak's Take card, a NiceGUI custom component.
//
// Two states, both ported from the Emiya Voice mockup (Main.dc.html, the take card, M L328-363):
//   * generating: the circuit trace circuit(692, 56) (M L1434-1456): two red strokes revealed by their
//     dash offset, a gold head with an orange glow (M L330-342); revealed to props.generating.fill, or
//     redrawn every 3 s while the fill is unknown; a static full trace at half opacity under Reduce Motion.
//   * a take: 230 bars, 2 px wide with 1 px gaps, 3 + v * 50 px high (M L344-358, L1488), the played part
//     gold, a 1 px playhead and the 18 px head glow (M L357-358), an invisible range input that scrubs
//     (M L359) and a spark when a new take arrives (M L360-362); new bars rise left to right.
// The playhead follows the shell's one <audio> (the kit Player): a frame loop, capped like the forge
// field, runs only while that audio plays this take, and it also writes the card's time text
// (props.timeTarget), because the Player reports to Python at most four times a second.
// This file holds no colour values: every colour comes from a class of the theme stylesheet.
//
// circuit, pathD and along are also in emiya_bars.js: NiceGUI serves each component file on its own,
// so the two cannot share a module; tests/wave_logic.mjs checks both copies against the mockup.

const BOX_W = 692;              // the waveform box (M L328): 230 bars of 2 px with 1 px gaps
const BOX_H = 56;
const BAR_COUNT = 230;
const RISE_MS = 240;            // a new take's bars rise left to right across this delay (M L1488)
const LOOP_MS = 3000;           // an indeterminate trace redraws every 3 s
const SPARK_MS = 1150;          // how long the take spark stays (M L1393)
const FILL_EASE_S = 0.2;        // a growing fill eases over one core progress tick (0.2 s)
const TIMER_SLACK_MS = 2;       // FrameClock: wake this long before the due time, then wait for its vsync
const MEDIA_EVENTS = ['play', 'playing', 'pause', 'ended', 'seeked', 'timeupdate', 'loadedmetadata', 'durationchange', 'emptied'];

const ROW_STYLE = 'width: ' + BOX_W + 'px; height: ' + BOX_H + 'px; display: flex; align-items: center; gap: 1px';
export const PLAYHEAD_STYLE = 'position: absolute; top: 0; width: 1px; height: ' + BOX_H + 'px; margin-left: -0.5px; pointer-events: none';
export const HEAD_STYLE = 'position: absolute; top: 50%; width: 18px; height: 18px; margin: -9px 0 0 -9px; pointer-events: none';
export const GLOW_STYLE = 'display: block; width: 18px; height: 18px; border-radius: 50%';

const clamp01 = (v) => Math.max(0, Math.min(1, v));

// ---------------------------------------------------------------------------------------------
// Geometry and text, ported from the mockup (pure functions, checked by tests/wave_logic.mjs).
// ---------------------------------------------------------------------------------------------

// circuit(w, h) (M L1434-1456): a gate-shaped trace along the mid line, statement for statement.
export function circuit(w, h) {
  const mid = h / 2;
  const amp = Math.max(6, Math.min(18, h / 2 - 8));
  const pts = [[3, mid]];
  let x = 3;
  let up = true;
  let i = 0;
  while (x + 180 < w) {
    x += 34 + ((i * 37) % 46);
    pts.push([x, mid]);
    const dy = up ? -amp : amp;
    x += amp;
    pts.push([x, mid + dy]);
    x += 22 + ((i * 53) % 36);
    pts.push([x, mid + dy]);
    x += amp;
    pts.push([x, mid]);
    up = !up;
    i += 1;
  }
  pts.push([Math.max(4, w - 3), mid]);
  return pts;
}

// pathD (M L1458-1460)
export function pathD(pts) {
  return 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
}

// along (M L1462-1473): the point at fraction prog of the polyline's length.
export function along(pts, prog) {
  const cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const target = Math.max(0, Math.min(1, prog)) * cum[cum.length - 1];
  for (let i = 1; i < pts.length; i++) {
    if (cum[i] >= target) {
      const f = (target - cum[i - 1]) / Math.max(0.001, cum[i] - cum[i - 1]);
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f];
    }
  }
  return pts[pts.length - 1];
}

// The dash offset that reveals fraction prog of a path drawn with pathLength 1000 (M L3014).
export function traceOffset(prog) {
  return Math.round(1000 * (1 - clamp01(prog)));
}

// m:ss.s (M L1424-1428), rounded to tenths first, so 59.96 s reads 1:00.0 and never 0:60.0.
export function clock(t) {
  const tenths = Math.max(0, Math.round((Number.isFinite(t) ? t : 0) * 10));
  const m = Math.floor(tenths / 600);
  const sec = (tenths - m * 600) / 10;
  return m + ':' + (sec < 10 ? '0' : '') + sec.toFixed(1);
}

// "0:01.9 / 0:04.6" (M L3024)
export function timeText(pos, len) {
  return clock(pos) + ' / ' + clock(len);
}

// A bar's height for a peak v (0..1, normalised to the take's own maximum), M L1488.
export function barHeight(v) {
  return Math.max(3, Math.round(3 + v * 50));
}

export function riseDelay(i, n) {
  return Math.round((i / n) * RISE_MS);
}

// The bars of a take: height and rise delay; a flat 3 px row of 230 when the take has no peaks.
export function waveBars(values) {
  const vs = values && values.length ? values : new Array(BAR_COUNT).fill(0);
  return vs.map((v, i) => {
    const h = barHeight(Number(v) || 0);
    const d = riseDelay(i, vs.length);
    return { h: h, d: d, style: 'display: block; flex: none; width: 2px; height: ' + h + 'px; border-radius: 1px; animation-delay: ' + d + 'ms' };
  });
}

// The played part in percent of the box (M L3019).
export function playPct(pos, len) {
  return len > 0 ? Math.max(0, Math.min(100, (pos / len) * 100)) : 0;
}

// Whether two media URLs name the same file (path only: the Player may add a query).
export function samePath(a, b, base) {
  try {
    return new URL(a, base).pathname === new URL(b, base).pathname;
  } catch (e) {
    return false;
  }
}

function audioUnder(root) {
  if (!root) return null;
  if (root.tagName === 'AUDIO') return root;
  return root.querySelector ? root.querySelector('audio') : null;
}

// ---------------------------------------------------------------------------------------------
// Loops (checked by tests/runner_cadence.mjs).
// ---------------------------------------------------------------------------------------------

// A frame loop capped at fps, gated like the forge field's: it sleeps on a timer until just before the
// next due time and then asks for one animation frame, so it wakes about fps times a second on any
// display. fps 0 asks for every frame.
export class FrameClock {
  constructor(fps, tick) {
    this.fps = fps > 0 ? fps : 0;
    this.tick = tick;
    this.running = false;
    this.raf = 0;
    this.wake = 0;
    this.nextDue = 0;
    this.frameFn = (now) => this.frame(now);
    this.wakeFn = () => {
      this.wake = 0;
      if (this.running) this.raf = requestAnimationFrame(this.frameFn);
    };
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.nextDue = 0;
    this.raf = requestAnimationFrame(this.frameFn);
  }

  // Leaves nothing scheduled.
  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    if (this.wake) clearTimeout(this.wake);
    this.raf = 0;
    this.wake = 0;
  }

  frame(now) {
    this.raf = 0;
    if (!this.running) return;
    this.tick(now);
    if (!this.running) return;
    if (!this.fps) {
      this.raf = requestAnimationFrame(this.frameFn);
      return;
    }
    const period = 1000 / this.fps;
    const due = this.nextDue ? this.nextDue + period : 0;
    this.nextDue = due > now ? due : Math.floor(now / period + 1) * period;
    this.wake = setTimeout(this.wakeFn, Math.max(0, this.nextDue - performance.now() - TIMER_SLACK_MS));
  }
}

// The head is an 18 px CSS radial gradient. Put its opacity on the compositor with one
// step per frame-cap interval, so a paused take needs no per-frame JS, canvas upload or DOM paint.
export function pulseKeyframes(effect, fps) {
  const keys = effect.getKeyframes();
  const from = keys.length > 1 ? Number(keys[0].opacity ?? 1) : 1;
  const to = Number(keys[keys.length - 1].opacity);
  const timing = effect.getTiming();
  const duration = Number(timing.duration);
  const intervals = Math.max(1, Math.ceil(duration * fps / 1000));
  const curve = new KeyframeEffect(null, null, { duration, fill: 'both', easing: keys[0].easing });
  const sampler = new Animation(curve, null);
  // Stationary half-frame bins are centred on the original CSS curve. At the configured 60 Hz
  // cap this bounds the phase error to half a frame in either direction of the alternate pulse.
  const frames = [{ offset: 0, opacity: from, easing: 'steps(1, end)' }];
  for (let i = 1; i <= intervals; i++) {
    sampler.currentTime = duration * i / intervals;
    const position = curve.getComputedTiming().progress;
    frames.push({ offset: (i - 0.5) / intervals, opacity: from + (to - from) * position,
      easing: 'steps(1, end)' });
  }
  frames.push({ offset: 1, opacity: to });
  return frames;
}

export class PulseDriver {
  constructor(fps) {
    this.fps = fps;
    this.source = null;
    this.animation = null;
    this.enabled = false;
    this.onClockChange = () => this.sync();
  }

  attach() {
    document.addEventListener('visibilitychange', this.onClockChange);
    document.addEventListener('emiya-field-clock', this.onClockChange);
  }

  set(element, enabled) {
    const source = element?.getAnimations().find(a => a.animationName === 'vtHead') ?? null;
    if (source !== this.source) {
      this.reset();
      this.source = source;
      if (source) {
        const elapsed = source.currentTime ?? 0;
        source.pause();
        const effect = source.effect;
        this.animation = this.fps > 0
          ? element.animate(pulseKeyframes(effect, this.fps), effect.getTiming())
          : source;
        this.animation.currentTime = elapsed;
      }
    }
    this.enabled = enabled;
    this.sync();
  }

  sync() {
    if (!this.animation) return;
    const field = window.__emiyaFieldClock;
    const paused = !this.enabled || document.visibilityState === 'hidden'
      || (field && (field.paused || field.still || field.hidden));
    if (paused) {
      if (this.animation.playState !== 'paused') this.animation.pause();
    } else if (this.animation.playState !== 'running') {
      this.animation.play();
    }
  }

  reset() {
    if (this.animation && this.animation !== this.source) this.animation.cancel();
    this.animation = null;
    this.source = null;
  }

  detach() {
    this.reset();
    document.removeEventListener('visibilitychange', this.onClockChange);
    document.removeEventListener('emiya-field-clock', this.onClockChange);
  }
}

// The playhead: while the shared <audio> plays this take, every frame moves the played part, the playhead
// line, the head glow and the scrub input and writes the card's time text; any other media event of
// that audio renders once.
export class WavePlayhead {
  constructor(view, options) {
    this.view = view;                       // { played, playhead, head, scrub }: elements, any may be null
    this.audioId = options.audioId || '';   // the Player element (the <audio> or a box around it); '' = any
    this.timeTarget = options.timeTarget || '';
    this.base = options.base;               // document.baseURI
    this.take = null;                       // { id, length_s, src }
    this.audio = null;
    this.scrubbing = false;
    this.clock = new FrameClock(options.fps, () => this.frame());
    this.onMedia = (ev) => this.media(ev);
  }

  // Media events do not bubble; the capture phase on the document sees those of every <audio>.
  attach() {
    for (const type of MEDIA_EVENTS) document.addEventListener(type, this.onMedia, true);
    this.audio = audioUnder(this.audioId ? document.getElementById(this.audioId) : document.querySelector('audio'));
    this.sync();
  }

  detach() {
    for (const type of MEDIA_EVENTS) document.removeEventListener(type, this.onMedia, true);
    this.clock.stop();
  }

  setTake(take) {
    this.take = take || null;
    this.sync();
  }

  setView(view) {
    this.view = view;
    this.sync();
  }

  media(ev) {
    const el = ev.target;
    if (!el || el.tagName !== 'AUDIO' || !this.owns(el)) return;
    this.audio = el;
    this.sync();
  }

  owns(el) {
    if (!this.audioId) return true;
    const root = document.getElementById(this.audioId);
    return !!root && (root === el || root.contains(el));
  }

  onThisTake() {
    const a = this.audio;
    if (!a || !this.take) return false;
    const src = a.currentSrc || a.src || '';
    return !this.take.src || (!!src && samePath(src, this.take.src, this.base));
  }

  playing() {
    return this.onThisTake() && !this.audio.paused && !this.audio.ended;
  }

  length() {
    const len = this.take ? this.take.length_s : 0;
    if (len > 0) return len;
    const d = this.audio ? this.audio.duration : 0;
    return Number.isFinite(d) && d > 0 ? d : 0;
  }

  position() {
    return this.onThisTake() ? this.audio.currentTime || 0 : 0;
  }

  sync() {
    if (this.playing()) this.clock.start();
    else this.clock.stop();
    this.render(false);
  }

  frame() {
    if (this.playing()) {
      this.render(true);
      return;
    }
    this.clock.stop();
    this.render(false);
  }

  render(live) {
    const v = this.view || {};
    const len = this.length();
    const pos = this.scrubbing && v.scrub ? Number(v.scrub.value) || 0 : this.position();
    const pct = playPct(pos, len) + '%';
    const text = timeText(pos, len);
    if (v.played) v.played.style.width = pct;
    if (v.playhead) v.playhead.style.left = pct;
    if (v.head) v.head.style.left = pct;
    if (v.scrub) {
      if (!this.scrubbing && v.scrub.value !== String(pos)) v.scrub.value = String(pos);
      if (v.scrub.getAttribute('aria-valuetext') !== text) v.scrub.setAttribute('aria-valuetext', text);
    }
    if (live) this.writeTime(text);
  }

  // The card's time element, written only while playing (Python writes it while paused and on seek). The
  // text goes into the element's own text node, so the element's later updates from Python still land.
  writeTime(text) {
    if (!this.timeTarget) return;
    const el = document.getElementById(this.timeTarget);
    const node = el ? Array.from(el.childNodes).find((n) => n.nodeType === 3) : null;
    if (node && node.nodeValue !== text) node.nodeValue = text;
  }

  // The scrub input: dragging moves the playhead (and this take's audio, when it is loaded); the
  // component then asks Python to seek, which loads the take first when needed.
  scrubInput(seconds) {
    this.scrubbing = true;
    if (this.onThisTake()) this.audio.currentTime = seconds;
    this.render(false);
  }

  scrubChange(seconds) {
    this.scrubbing = false;
    if (this.onThisTake()) this.audio.currentTime = seconds;
    this.render(false);
  }
}

// The generating trace: revealed to a fill, eased while it grows; redrawn every 3 s while the fill is
// unknown; a static full trace without its head under Reduce Motion.
export class TraceDriver {
  constructor(pts, fps, view) {
    this.pts = pts;
    this.view = view;                       // { paths: [shadow, line], head }
    this.clock = new FrameClock(fps, (now) => this.loopFrame(now));
    this.loopStart = 0;
    this.shown = -1;
  }

  set(fill, reduced) {
    if (reduced) {
      this.clock.stop();
      this.draw(1, false, false);
    } else if (fill === null || fill === undefined) {
      if (!this.clock.running) {
        this.loopStart = performance.now();
        this.clock.start();
      }
    } else {
      this.clock.stop();
      const prog = clamp01(Number(fill) || 0);
      this.draw(prog, this.shown >= 0 && prog > this.shown, true);
    }
  }

  loopFrame(now) {
    const t = (((now - this.loopStart) % LOOP_MS) + LOOP_MS) % LOOP_MS;
    this.draw(t / LOOP_MS, false, true);
  }

  draw(prog, ease, head) {
    const v = this.view || {};
    const offset = String(traceOffset(prog));
    for (const p of v.paths || []) {
      p.style.transition = ease ? 'stroke-dashoffset ' + FILL_EASE_S + 's linear' : 'none';
      p.style.strokeDashoffset = offset;
    }
    if (v.head) {
      const xy = along(this.pts, prog);
      v.head.style.display = head ? '' : 'none';
      v.head.style.transition = ease ? 'transform ' + FILL_EASE_S + 's linear' : 'none';
      v.head.style.transform = 'translate(' + xy[0].toFixed(1) + 'px, ' + xy[1].toFixed(1) + 'px)';
    }
    this.shown = prog;
  }

  reset() {
    this.clock.stop();
    this.shown = -1;
  }
}

// ---------------------------------------------------------------------------------------------
// The Vue component (props from take_wave.py).
// ---------------------------------------------------------------------------------------------
const WAVE_PTS = circuit(BOX_W, BOX_H);
const WAVE_D = pathD(WAVE_PTS);
let instances = 0;

const TEMPLATE = `
<div class="vt-wavebox emiya-take-wave" style="position: relative; width: ${BOX_W}px; height: ${BOX_H}px">
  <svg v-if="generatingNow" width="${BOX_W}" height="${BOX_H}" viewBox="0 0 ${BOX_W} ${BOX_H}" role="img"
       aria-label="Generating the take" class="emiya-trace" :class="{ 'is-still': reduced }"
       :style="{ display: 'block', overflow: 'visible', opacity: reduced ? 0.5 : 1 }">
    <defs>
      <radialGradient :id="gradId">
        <stop offset="0%" class="emiya-trace-glow-in"></stop>
        <stop offset="100%" class="emiya-trace-glow-out"></stop>
      </radialGradient>
    </defs>
    <line x1="0" y1="${BOX_H / 2}" x2="${BOX_W}" y2="${BOX_H / 2}" class="emiya-trace-mid" stroke-width="1"></line>
    <path ref="traceShadow" :d="traceD" pathLength="1000" class="emiya-trace-shadow" fill="none" stroke-width="3.2"
          stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="1000" stroke-dashoffset="1000"></path>
    <path ref="traceLine" :d="traceD" pathLength="1000" class="emiya-trace-line" fill="none" stroke-width="1"
          stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="1000" stroke-dashoffset="1000"></path>
    <g ref="traceHead">
      <circle cx="0" cy="0" r="9" :fill="'url(#' + gradId + ')'"></circle>
      <circle cx="0" cy="0" r="1.6" class="emiya-trace-dot"></circle>
    </g>
  </svg>
  <template v-else-if="take">
    <div :key="'bars-' + newSeq" aria-hidden="true" style="position: absolute; left: 0; top: 0; ${ROW_STYLE}">
      <span v-for="(b, i) in bars" :key="i" :class="['emiya-wave-bar', { 'vt-rise': !reduced }]" :style="b.style"></span>
    </div>
    <div ref="played" aria-hidden="true" class="emiya-wave-played"
         style="position: absolute; left: 0; top: 0; height: ${BOX_H}px; overflow: hidden">
      <div :key="'played-' + newSeq" style="${ROW_STYLE}">
        <span v-for="(b, i) in bars" :key="i" :class="['emiya-wave-bar', 'is-played', { 'vt-rise': !reduced }]" :style="b.style"></span>
      </div>
    </div>
    <span ref="playhead" aria-hidden="true" class="emiya-wave-playhead" style="${PLAYHEAD_STYLE}"></span>
    <span ref="head" aria-hidden="true" style="${HEAD_STYLE}"><span ref="glow" :class="['emiya-wave-glow', { 'vt-head-glow': !reduced }]" style="${GLOW_STYLE}"></span></span>
    <input ref="scrub" type="range" class="vt-scrub" aria-label="Playback position" min="0" :max="scrubMax" step="0.1"
           style="position: absolute; left: 0; top: 0; width: 100%; height: ${BOX_H}px; margin: 0; opacity: 0"
           @input="onScrubInput" @change="onScrubChange">
    <span v-if="sparkOn" :key="'spark-' + sparkSeq" aria-hidden="true" class="vt-spark" style="left: 99%; top: 50%"></span>
  </template>
</div>`;

export default {
  template: TEMPLATE,
  props: {
    take: Object,                 // { id, bars: 230 values 0..1, length_s, src } or null
    generating: Object,           // { active, fill: 0..1 or null }
    audioId: String,
    timeTarget: String,
    reduce: [Boolean, String],    // true (Reduce Motion "Always") or "system" (follow Windows)
    newSeq: Number,
    sparkSeq: Number,
    fpsCap: Number,
  },
  emits: ['seek'],
  data() {
    instances += 1;
    return { reduced: false, sparkOn: false, gradId: 'emiya-take-head-' + instances, traceD: WAVE_D };
  },
  computed: {
    generatingNow() {
      return !!(this.generating && this.generating.active);
    },
    bars() {
      return waveBars(this.take ? this.take.bars : []);
    },
    scrubMax() {
      return this.take && this.take.length_s > 0 ? this.take.length_s : 1;
    },
  },
  mounted() {
    this.motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.onMotion = () => this.syncReduce();
    this.motionQuery.addEventListener('change', this.onMotion);
    this.reduced = this.isReduced();
    const fps = typeof this.fpsCap === 'number' ? this.fpsCap : 60;
    this.playhead = new WavePlayhead(this.playheadView(), { audioId: this.audioId, timeTarget: this.timeTarget, fps: fps, base: document.baseURI });
    this.playhead.take = this.takeInfo();
    this.trace = new TraceDriver(WAVE_PTS, fps, this.traceView());
    this.pulse = new PulseDriver(fps);
    this.pulse.attach();
    this.playhead.attach();
    this.syncTrace();
    this.syncPulse();
  },
  updated() {
    // Vue patched or re-created parts of the box: hand the loops the current elements.
    this.playhead.setView(this.playheadView());
    this.trace.view = this.traceView();
    this.syncTrace();
    this.syncPulse();
  },
  unmounted() {
    this.motionQuery.removeEventListener('change', this.onMotion);
    this.playhead.detach();
    this.trace.reset();
    this.pulse.detach();
    clearTimeout(this.sparkTimer);
  },
  watch: {
    take() {
      this.playhead.setTake(this.takeInfo());
    },
    generating(next, prev) {
      if (!(prev && prev.active) || !(next && next.active)) this.trace.reset();
      this.playhead.setTake(this.takeInfo());
      this.$nextTick(() => this.syncTrace());
    },
    reduce() {
      this.syncReduce();
    },
    sparkSeq(next, prev) {
      if (!next || next === prev || this.reduced) return;
      this.sparkOn = true;
      clearTimeout(this.sparkTimer);
      this.sparkTimer = setTimeout(() => { this.sparkOn = false; }, SPARK_MS);
    },
  },
  methods: {
    isReduced() {
      return this.reduce === true || (this.reduce === 'system' && !!this.motionQuery && this.motionQuery.matches);
    },
    syncReduce() {
      this.reduced = this.isReduced();
      if (this.trace) this.$nextTick(() => this.syncTrace());
    },
    syncTrace() {
      if (!this.generatingNow) {
        this.trace.reset();
        return;
      }
      this.trace.set(this.generating.fill, this.reduced);
    },
    syncPulse() {
      this.pulse.set(this.$refs.glow || null, !!this.take && !this.generatingNow && !this.reduced);
    },
    // The take the playhead follows; none while the box shows the generating trace (no time is shown then).
    takeInfo() {
      const t = this.take;
      return t && !this.generatingNow ? { id: t.id, length_s: Number(t.length_s) || 0, src: t.src || '' } : null;
    },
    playheadView() {
      const r = this.$refs;
      return { played: r.played || null, playhead: r.playhead || null, head: r.head || null, scrub: r.scrub || null };
    },
    traceView() {
      const r = this.$refs;
      return { paths: [r.traceShadow, r.traceLine].filter(Boolean), head: r.traceHead || null };
    },
    onScrubInput(ev) {
      this.playhead.scrubInput(Number(ev.target.value) || 0);
    },
    onScrubChange(ev) {
      const seconds = Number(ev.target.value) || 0;
      this.playhead.scrubChange(seconds);
      this.$emit('seek', seconds);
    },
  },
};
