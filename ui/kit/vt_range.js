// The DOM owns the native thumb during a gesture. Vue only watches the authoritative
// server value and the acknowledgement of a release; it never renders an input value.
const rangeKeys = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown']);

export default {
  props: { serverValue: Number, sync: { type: Number, default: 0 }, disabled: Boolean },
  template: `<input type="range" :disabled="disabled" @input="paint" @change.stop="changed"
    @pointerdown="pointerDown" @keydown="keyDown" @keyup="keyUp" @blur="blur">`,
  created() {
    this.pointer = null;
    this.keys = new Set();
    this.sequence = this.sync;
  },
  mounted() { this.applyServer(); },
  beforeUnmount() { this.unwatchPointer(); },
  watch: {
    serverValue: 'applyServer',
    sync: 'applyServer',
    disabled(value) { if (value) this.cancel(); },
  },
  methods: {
    paint() {
      const el = this.$el;
      const percent = 100 * (el.valueAsNumber - Number(el.min)) / (Number(el.max) - Number(el.min));
      el.style.setProperty('--pct', percent.toFixed(2) + '%');
    },
    applyServer() {
      if (this.pointer !== null || this.keys.size || this.sequence > this.sync) return;
      this.$el.value = String(this.serverValue);
      this.lastValue = this.$el.value;
      this.paint();
    },
    pointerDown(event) {
      if (event.button !== 0 || event.isPrimary === false || this.pointer !== null) return;
      this.pointer = event.pointerId;
      window.addEventListener('pointerup', this.pointerUp, true);
      window.addEventListener('pointercancel', this.pointerCancel, true);
    },
    unwatchPointer() {
      this.pointer = null;
      window.removeEventListener('pointerup', this.pointerUp, true);
      window.removeEventListener('pointercancel', this.pointerCancel, true);
    },
    pointerUp(event) {
      if (event.pointerId !== this.pointer) return;
      this.unwatchPointer();
      this.changed();
    },
    pointerCancel(event) {
      if (event.pointerId === this.pointer) this.cancel();
    },
    keyDown(event) {
      if (rangeKeys.has(event.key)) this.keys.add(event.key);
    },
    keyUp(event) {
      if (this.keys.delete(event.key)) this.changed();
    },
    blur() {
      this.keys.clear();
      this.changed();
    },
    cancel() {
      this.unwatchPointer();
      this.keys.clear();
      this.applyServer();
    },
    changed() {
      if (this.pointer !== null || this.keys.size || this.disabled) return;
      const el = this.$el;
      if (el.value === this.lastValue) { this.applyServer(); return; }
      this.lastValue = el.value;
      el.parentElement.dispatchEvent(new CustomEvent('change', {
        bubbles: true, detail: { value: el.valueAsNumber, sync: ++this.sequence },
      }));
    },
  },
};
