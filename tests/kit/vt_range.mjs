import assert from 'node:assert/strict';
import Component from '../../ui/kit/vt_range.js';

globalThis.window = new EventTarget();
const sent = [];
const style = new Map();
const input = {
  min: '0.25', max: '2', value: '',
  get valueAsNumber() { return Number(this.value); },
  style: { setProperty: (key, value) => style.set(key, value) },
  parentElement: { dispatchEvent: event => sent.push(event.detail) },
};
const vm = { $el: input, sync: 0, serverValue: 0.5, disabled: false };
for (const [key, method] of Object.entries(Component.methods)) vm[key] = method.bind(vm);
Component.created.call(vm);
Component.mounted.call(vm);
let passed = 0;
function equal(actual, expected) { assert.deepEqual(actual, expected); ++passed; }
equal(input.value, '0.5');
equal(style.get('--pct'), '14.29%');
vm.pointerDown({ pointerId: 7, button: 0 });
input.value = '1.5';
vm.paint();
equal(style.get('--pct'), '71.43%');
vm.serverValue = 0.75;
vm.applyServer();
vm.changed();
equal(input.value, '1.5');
equal(sent, []);
vm.pointerUp({ pointerId: 99 });
equal(vm.pointer, 7);
vm.pointerUp({ pointerId: 7 });
vm.changed();                    // WebView emits native change after pointerup.
equal(sent, [{ value: 1.5, sync: 1 }]);
vm.applyServer();                // Still waiting for Voices' asynchronous response.
equal(input.value, '1.5');
vm.sync = 1;
vm.applyServer();                // Refused: server acknowledges with the original value.
equal(input.value, '0.75');
equal(style.get('--pct'), '28.57%');

vm.keyDown({ key: 'ArrowRight' });
for (const value of ['0.8', '0.85', '0.9']) {
  input.value = value;
  vm.paint();
  vm.changed();                  // Native range sends a change on every key repeat.
  vm.applyServer();
  equal(input.value, value);
}
equal(sent.length, 1);
vm.keyUp({ key: 'ArrowRight' });
vm.keyUp({ key: 'ArrowRight' });
equal(sent[1], { value: 0.9, sync: 2 });
equal(sent.length, 2);

vm.pointerDown({ pointerId: 8, button: 0 });
input.value = '1.1';
vm.pointerUp({ pointerId: 8 });
vm.serverValue = 0.9;
vm.sync = 2;
vm.applyServer();                // Earlier acknowledgment arrives after the next gesture.
equal(input.value, '1.1');
equal(sent[2], { value: 1.1, sync: 3 });
vm.serverValue = 1.1;
vm.sync = 3;
vm.applyServer();
vm.pointerDown({ pointerId: 9, button: 0 });
input.value = '1.8';
vm.pointerCancel({ pointerId: 9 });
equal(input.value, '1.1');
equal(sent.length, 3);
vm.keyDown({ key: 'End' });
input.value = '2';
vm.blur();
equal(sent[3], { value: 2, sync: 4 });
equal(sent.length, 4);
Component.beforeUnmount.call(vm);
console.log(JSON.stringify({ ok: true, passed, failed: [] }));
