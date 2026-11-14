// Node test for ui/kit/vt_keys.js: normalisation, IME skip, focus rules, preventDefault on a match,
// suspension while a key slot listens, repeats, live bindings and the Vue component's wiring.
//
// Usage: node tests/kit/vt_keys.mjs  -> one JSON line {"ok", "passed", "failed"}; exit code 1 on a failure.
import Component, { KeyRouter, comboOf, isButtonLike, isComposing, isTextEntry } from '../../ui/kit/vt_keys.js';

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

function element(tagName, { role = null, href = null, editable = false } = {}) {
  const attributes = {};
  if (role !== null) attributes.role = role;
  if (href !== null) attributes.href = href;
  return {
    tagName,
    isContentEditable: editable,
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    hasAttribute: (name) => name in attributes,
  };
}

const BODY = element('BODY');

function keydown(key, options = {}) {
  const event = {
    type: 'keydown',
    key,
    ctrlKey: Boolean(options.ctrl),
    shiftKey: Boolean(options.shift),
    altKey: Boolean(options.alt),
    metaKey: Boolean(options.meta),
    repeat: Boolean(options.repeat),
    isComposing: Boolean(options.composing),
    keyCode: options.keyCode ?? 0,
    target: options.target ?? BODY,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
  return event;
}

// The defaults of BUILD_SPEC 5.5, as CommandRegistry.combos() gives them.
const COMBOS = {
  'Ctrl+Enter': 'gen',
  'Ctrl+Shift+Enter': 'gen3',
  Space: 'play',
  'Alt+Up': 'prev',
  'Alt+Down': 'next',
  'Ctrl+B': 'ab',
  'Ctrl+S': 'save',
  'Ctrl+K': 'search',
};

function router() {
  const sent = [];
  const keys = new KeyRouter((id, combo) => sent.push([id, combo]));
  keys.combos = { ...COMBOS };
  return { keys, sent };
}

{
  const { keys, sent } = router();
  const clear = element('BUTTON');
  const first = keydown('Enter', { target: clear });
  const held = keydown('Enter', { target: clear, repeat: true });
  keys.handle(first);
  keys.handle(held);
  check('a held Enter activates a focused button only once', !first.prevented && held.prevented && !sent.length);
}

// ---- normalisation (same rules as the Keys capture and CommandRegistry.normalize) ----------------
equal('ctrl enter', comboOf(keydown('Enter', { ctrl: true })), 'Ctrl+Enter');
equal('ctrl shift enter', comboOf(keydown('Enter', { ctrl: true, shift: true })), 'Ctrl+Shift+Enter');
equal('space', comboOf(keydown(' ')), 'Space');
equal('alt up drops Arrow', comboOf(keydown('ArrowUp', { alt: true })), 'Alt+Up');
equal('one character upper case', comboOf(keydown('b', { ctrl: true })), 'Ctrl+B');
equal('modifier order', comboOf(keydown('x', { alt: true, shift: true, ctrl: true })), 'Ctrl+Shift+Alt+X');
equal('named key kept', comboOf(keydown('F5')), 'F5');
equal('windows key ignored', comboOf(keydown('k', { meta: true })), 'K');
for (const key of ['Control', 'Shift', 'Alt', 'Meta', 'AltGraph']) {
  equal(`modifier alone (${key})`, comboOf(keydown(key, { ctrl: key === 'Control' })), '');
}
equal('no key', comboOf(keydown(undefined)), '');

// ---- a match: preventDefault and one command ------------------------------------------------------
{
  const { keys, sent } = router();
  const space = keydown(' ');
  equal('space on the page plays', keys.handle(space), 'play');
  check('space on the page is prevented (no scroll)', space.prevented);
  const unbound = keydown('q', { ctrl: true });
  equal('unbound combo is left alone', keys.handle(unbound), null);
  check('unbound combo is not prevented', !unbound.prevented);
  const plain = keydown('k');
  equal('plain K is not Ctrl+K', keys.handle(plain), null);
  check('plain K is not prevented', !plain.prevented);
  equal('one command per match', sent, [['play', 'Space']]);
}

// ---- IME composition --------------------------------------------------------------------------------
{
  const { keys, sent } = router();
  const composing = keydown('Enter', { ctrl: true, composing: true, target: element('TEXTAREA') });
  equal('isComposing: nothing fires', keys.handle(composing), null);
  check('isComposing: not prevented', !composing.prevented);
  const process = keydown('Process', { ctrl: true });
  equal('key Process: nothing fires', keys.handle(process), null);
  check('key Process: not prevented', !process.prevented);
  const legacy = keydown(' ', { keyCode: 229 });
  equal('keyCode 229: nothing fires', keys.handle(legacy), null);
  check('keyCode 229: not prevented', !legacy.prevented);
  check('isComposing() helper', isComposing(keydown('a', { composing: true })) && !isComposing(keydown('a')));
  equal('no command during composition', sent, []);
}

// ---- focus in text entry: only Ctrl or Alt combos -------------------------------------------------
{
  const { keys, sent } = router();
  for (const tag of ['INPUT', 'TEXTAREA', 'SELECT']) {
    const space = keydown(' ', { target: element(tag) });
    equal(`plain Space types in ${tag}`, keys.handle(space), null);
    check(`plain Space in ${tag} is not prevented`, !space.prevented);
  }
  const editable = keydown(' ', { target: element('DIV', { editable: true }) });
  equal('plain Space types in contenteditable', keys.handle(editable), null);
  check('plain Space in contenteditable is not prevented', !editable.prevented);
  const generate = keydown('Enter', { ctrl: true, target: element('TEXTAREA') });
  equal('Ctrl+Enter generates from the script box', keys.handle(generate), 'gen');
  check('Ctrl+Enter in the script box is prevented (no newline)', generate.prevented);
  const previous = keydown('ArrowUp', { alt: true, target: element('INPUT') });
  equal('Alt+Up fires from an input', keys.handle(previous), 'prev');
  check('Alt+Up in an input is prevented', previous.prevented);
  equal('text entry sends only the modifier combos', sent, [['gen', 'Ctrl+Enter'], ['prev', 'Alt+Up']]);
  check('isTextEntry() helper', isTextEntry(element('INPUT')) && !isTextEntry(element('BUTTON')) && !isTextEntry(null));
}

// ---- focus on a button or a segment: Space and Enter keep their meaning ----------------------------
{
  const { keys, sent } = router();
  keys.combos = { ...COMBOS, Enter: 'search' };
  const targets = [
    ['BUTTON', element('BUTTON')],
    ['role=radio (dock row)', element('DIV', { role: 'radio' })],
    ['role=tab', element('DIV', { role: 'tab' })],
    ['role=button', element('SPAN', { role: 'button' })],
    ['link', element('A', { href: '/forge' })],
  ];
  for (const [name, target] of targets) {
    for (const key of [' ', 'Enter']) {
      const event = keydown(key, { target });
      equal(`${JSON.stringify(key)} keeps its meaning on ${name}`, keys.handle(event), null);
      check(`${JSON.stringify(key)} on ${name} is not prevented`, !event.prevented);
    }
  }
  const generate = keydown('Enter', { ctrl: true, target: element('BUTTON') });
  equal('Ctrl+Enter still fires on a button', keys.handle(generate), 'gen');
  check('Ctrl+Enter on a button is prevented', generate.prevented);
  const other = keydown('b', { ctrl: true, target: element('BUTTON') });
  equal('Ctrl+B fires on a button', keys.handle(other), 'ab');
  const anchorWithoutHref = keydown(' ', { target: element('A') });
  equal('an anchor without href is not a button', keys.handle(anchorWithoutHref), 'play');
  equal('button focus sends only the other combos', sent, [['gen', 'Ctrl+Enter'], ['ab', 'Ctrl+B'], ['play', 'Space']]);
  check('isButtonLike() roles', isButtonLike(element('DIV', { role: 'switch' })) && !isButtonLike(element('DIV')));
}

// ---- suspended while a Keys slot listens ----------------------------------------------------------
{
  const { keys, sent } = router();
  keys.suspended = true;
  const generate = keydown('Enter', { ctrl: true });
  equal('suspended: nothing fires', keys.handle(generate), null);
  check('suspended: not prevented (the capture handles the key)', !generate.prevented);
  const space = keydown(' ');
  keys.handle(space);
  check('suspended: Space is not prevented either', !space.prevented);
  keys.suspended = false;
  equal('resumed: the combo fires again', keys.handle(keydown('Enter', { ctrl: true })), 'gen');
  equal('suspension sent nothing', sent, [['gen', 'Ctrl+Enter']]);
}

// ---- repeats and live bindings --------------------------------------------------------------------
{
  const { keys, sent } = router();
  const held = keydown(' ', { repeat: true });
  equal('a repeat sends nothing', keys.handle(held), null);
  check('a repeat is still prevented', held.prevented);
  keys.combos = { 'Ctrl+G': 'gen' };
  equal('a cleared binding stops firing', keys.handle(keydown('Enter', { ctrl: true })), null);
  equal('a new binding fires at once', keys.handle(keydown('g', { ctrl: true })), 'gen');
  keys.combos = { 'Ctrl+G': 'gen', 'Ctrl+Q': '' };
  for (const name of ['toString', 'constructor', '__proto__']) {
    equal(`inherited name ${name} never matches`, keys.handle(keydown(name)), null);
  }
  equal('an empty command id never fires', keys.handle(keydown('q', { ctrl: true })), null);
  equal('repeats and rebinding', sent, [['gen', 'Ctrl+G']]);
}

// ---- the Vue component: document listener, props, emitted payload ---------------------------------
{
  const listeners = [];
  globalThis.document = {
    addEventListener: (type, fn) => listeners.push([type, fn]),
    removeEventListener: (type, fn) => {
      const index = listeners.findIndex(([t, f]) => t === type && f === fn);
      if (index >= 0) listeners.splice(index, 1);
    },
  };
  const emitted = [];
  const vm = { combos: { ...COMBOS }, suspended: false, $emit: (name, payload) => emitted.push([name, payload]) };
  Component.created.call(vm);
  Component.mounted.call(vm);
  equal('one keydown listener on document', listeners.map(([type]) => type), ['keydown']);
  const handler = listeners[0][1];
  const space = keydown(' ');
  handler(space);
  equal('the component emits command {id, combo}', emitted, [['command', { id: 'play', combo: 'Space' }]]);
  check('the component prevents a matched key', space.prevented);
  Component.watch.suspended.call(vm, true);
  handler(keydown(' '));
  equal('the suspended prop applies live', emitted.length, 1);
  Component.watch.suspended.call(vm, false);
  Component.watch.combos.call(vm, { 'Ctrl+K': 'search' });
  handler(keydown(' '));
  handler(keydown('k', { ctrl: true }));
  equal('the combos prop applies live', emitted.slice(1), [['command', { id: 'search', combo: 'Ctrl+K' }]]);
  Component.watch.combos.call(vm, null);
  handler(keydown('k', { ctrl: true }));
  equal('null combos bind nothing', emitted.length, 2);
  Component.unmounted.call(vm);
  equal('unmounting removes the listener', listeners.length, 0);
  check('the template renders nothing visible', /^<span hidden><\/span>$/.test(Component.template));
}

const ok = results.failed.length === 0;
console.log(JSON.stringify({ ok, passed: results.passed, failed: results.failed }));
process.exit(ok ? 0 : 1);
