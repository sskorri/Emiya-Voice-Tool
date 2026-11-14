// KeyDispatcher (ui/kit/keys.py): one document keydown listener that turns bound combos into commands.
//
// ui.keyboard cannot do this: it skips focused inputs, textareas and buttons and never calls
// preventDefault (nicegui/elements/keyboard.js). The rules (BUILD_SPEC 1.5; S03 3.4):
// - nothing fires while `suspended` (a Keys slot listens) or during IME composition;
// - with focus in an input, textarea, select or contenteditable only Ctrl or Alt combos fire, so
//   plain keys type and Ctrl+Enter still generates from the script box;
// - on a button-like control a fresh press uses its normal activation; holding Enter cannot confirm it twice;
// - a match calls preventDefault (Space never scrolls); a held key repeats the preventDefault, not
//   the command.
// Combos are normalised as the Keys capture does: Ctrl, Shift, Alt in that order, then the key
// (" " becomes "Space", one character upper case, "Arrow" dropped); the Windows key is ignored.

const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'OS']);
const TEXT_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
const BUTTON_TAGS = new Set(['BUTTON', 'SUMMARY']);
const BUTTON_ROLES = new Set([
  'button', 'checkbox', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'switch',
  'tab', 'treeitem',
]);
const IME_KEYCODE = 229;

// "Ctrl+Shift+Enter" for a keydown; "" for a modifier on its own.
export function comboOf(event) {
  const key = event.key;
  if (typeof key !== 'string' || !key || MODIFIER_KEYS.has(key)) return '';
  const parts = [];
  if (event.ctrlKey) parts.push('Ctrl');
  if (event.shiftKey) parts.push('Shift');
  if (event.altKey) parts.push('Alt');
  parts.push(key === ' ' ? 'Space' : key.length === 1 ? key.toUpperCase() : key.replace('Arrow', ''));
  return parts.join('+');
}

export function isComposing(event) {
  return Boolean(event.isComposing) || event.key === 'Process' || event.keyCode === IME_KEYCODE;
}

export function isTextEntry(element) {
  return Boolean(element) && (TEXT_TAGS.has(element.tagName) || element.isContentEditable === true);
}

export function isButtonLike(element) {
  if (!element) return false;
  if (BUTTON_TAGS.has(element.tagName)) return true;
  if (element.tagName === 'A' && typeof element.hasAttribute === 'function' && element.hasAttribute('href')) {
    return true;
  }
  const role = typeof element.getAttribute === 'function' ? element.getAttribute('role') : null;
  return typeof role === 'string' && role.trim().toLowerCase().split(/\s+/).some((name) => BUTTON_ROLES.has(name));
}

export class KeyRouter {
  constructor(send) {
    this.send = send; // (commandId, combo) => void
    this.combos = {}; // combo -> command id
    this.suspended = false;
  }

  // The command id sent for this keydown, or null when the key is left alone or only swallowed.
  handle(event) {
    if (this.suspended || isComposing(event)) return null;
    if (event.key === 'Enter' && event.repeat && isButtonLike(event.target)) {
      event.preventDefault();
      return null;
    }
    const combo = comboOf(event);
    if (!combo || !Object.prototype.hasOwnProperty.call(this.combos, combo)) return null;
    const commandId = this.combos[combo];
    if (typeof commandId !== 'string' || !commandId) return null;
    const withModifier = Boolean(event.ctrlKey || event.altKey);
    if (!withModifier && isTextEntry(event.target)) return null;
    if (!withModifier && (event.key === ' ' || event.key === 'Enter') && isButtonLike(event.target)) return null;
    event.preventDefault();
    if (event.repeat) return null;
    this.send(commandId, combo);
    return commandId;
  }
}

export default {
  template: '<span hidden></span>',
  props: {
    combos: Object,
    suspended: Boolean,
  },
  created() {
    this.router = new KeyRouter((id, combo) => this.$emit('command', { id, combo }));
    this.router.combos = this.combos || {};
    this.router.suspended = Boolean(this.suspended);
  },
  mounted() {
    this.onKeydown = (event) => this.router.handle(event);
    document.addEventListener('keydown', this.onKeydown);
  },
  watch: {
    combos(value) {
      this.router.combos = value || {};
    },
    suspended(value) {
      this.router.suspended = Boolean(value);
    },
  },
  unmounted() {
    document.removeEventListener('keydown', this.onKeydown);
  },
};
