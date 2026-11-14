// Player (ui/kit/audio.py): the shell's one hidden <audio>, reconciled to Python's PlayerTarget.
//
// The target is state, not commands (BUILD_SPEC 5.4): a new src loads and seeks to `at`; the same
// src with `at` set seeks; then the audio plays or pauses. A seq that was already applied changes
// nothing, so re-renders that carry the same target are free, and a src that failed to load loads
// again when a newer target names it. The browser's state goes back as `state` events, at most 4 a
// second, each stamped with the seq of the target in force:
//   {seq, playing, position, duration, ended, error}

export const STATE_INTERVAL_MS = 250;
const HAVE_METADATA = 1;
const MEDIA_EVENTS = [
  'play', 'playing', 'pause', 'ended', 'seeked', 'timeupdate', 'loadedmetadata', 'durationchange', 'emptied',
  'error',
];
const MEDIA_ERRORS = {
  1: 'Playback was aborted',
  2: 'A network error stopped the audio',
  3: 'The audio could not be decoded',
  4: 'The audio format or file is not supported',
};

const systemClock = {
  now: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle),
};

function finiteOr(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

function resolveSrc(src) {
  // NiceGUI's own audio element prefixes root-relative URLs the same way (elements/audio.js).
  const prefix = typeof window !== 'undefined' && window.path_prefix ? window.path_prefix : '';
  return src.startsWith('/') ? prefix + src : src;
}

export class PlayerCore {
  constructor(audio, send, clock = systemClock) {
    this.audio = audio;
    this.send = send;
    this.clock = clock;
    this.applied = null; // seq of the target in force
    this.src = ''; // that target's src, as Python sent it
    this.pendingSeek = null; // seconds to seek to once the metadata is in
    this.playError = ''; // why the last play() was refused
    this.lastSentAt = -Infinity;
    this.lastSent = '';
    this.timer = null;
    this.onMediaEvent = (event) => this.mediaEvent(event);
    for (const type of MEDIA_EVENTS) audio.addEventListener(type, this.onMediaEvent);
  }

  // Reconcile to a target {seq, src, playing, at}; false when its seq is already in force.
  apply(target) {
    if (!target || target.seq === this.applied) return false;
    this.applied = target.seq;
    this.audio.muted = target.muted === true;
    const src = typeof target.src === 'string' ? target.src : '';
    const at = typeof target.at === 'number' && Number.isFinite(target.at) ? Math.max(0, target.at) : null;
    if (src !== this.src || (src && this.audio.error)) this.load(src, at);
    else if (at !== null) this.seek(at);
    if (!src) return true;
    if (target.playing) this.play();
    else this.audio.pause();
    return true;
  }

  load(src, at) {
    const retry = src === this.src;
    this.src = src;
    this.playError = '';
    this.pendingSeek = src && at ? at : null;
    if (!src) {
      this.audio.removeAttribute('src');
      this.audio.load();
    } else if (retry) {
      this.audio.load();
    } else {
      this.audio.src = resolveSrc(src);
    }
  }

  seek(at) {
    const audio = this.audio;
    if (audio.readyState >= HAVE_METADATA) {
      this.pendingSeek = null;
      audio.currentTime = Math.min(at, finiteOr(audio.duration, Infinity));
    } else {
      this.pendingSeek = at;
    }
  }

  play() {
    this.playError = '';
    const promise = this.audio.play();
    if (promise && typeof promise.catch === 'function') {
      promise.catch((error) => {
        if (error && error.name === 'AbortError') return; // a newer load or a pause came first
        const name = (error && error.name) || 'Error';
        this.playError = `${name}: ${(error && error.message) || 'playback was refused'}`;
        this.report();
      });
    }
  }

  mediaEvent(event) {
    if (event.type === 'loadedmetadata' && this.pendingSeek !== null) this.seek(this.pendingSeek);
    this.report();
  }

  snapshot() {
    const audio = this.audio;
    return {
      seq: this.applied,
      playing: !audio.paused,
      position: round3(Math.max(0, finiteOr(audio.currentTime, 0))),
      duration: round3(Math.max(0, finiteOr(audio.duration, 0))),
      ended: Boolean(audio.ended),
      error: this.errorText(),
    };
  }

  errorText() {
    const failure = this.audio.error;
    if (!failure) return this.playError;
    return failure.message ? String(failure.message) : MEDIA_ERRORS[failure.code] || 'The audio could not be played';
  }

  // Send the state now, or once STATE_INTERVAL_MS has passed since the last send (then the newest state).
  report() {
    if (this.timer !== null) return;
    const wait = this.lastSentAt + STATE_INTERVAL_MS - this.clock.now();
    if (wait <= 0) {
      this.flush();
      return;
    }
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      this.flush();
    }, wait);
  }

  flush() {
    const state = this.snapshot();
    const key = JSON.stringify(state);
    if (key === this.lastSent) return;
    this.lastSent = key;
    this.lastSentAt = this.clock.now();
    this.send(state);
  }

  destroy() {
    for (const type of MEDIA_EVENTS) this.audio.removeEventListener(type, this.onMediaEvent);
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
    this.audio.pause(); // a detached <audio> would keep playing
  }
}

export default {
  template: '<audio preload="auto"></audio>',
  props: {
    target: Object,
  },
  mounted() {
    this.core = new PlayerCore(this.$el, (state) => this.$emit('state', state));
    // One tick later, like NiceGUI's audio element: window.path_prefix is set in the app's mounted().
    this.startTimer = setTimeout(() => this.core.apply(this.target), 0);
  },
  watch: {
    target(value) {
      if (this.core) this.core.apply(value);
    },
  },
  unmounted() {
    clearTimeout(this.startTimer);
    if (this.core) this.core.destroy();
  },
};
