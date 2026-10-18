const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  class Element {
    constructor(parent = null) {
      this.parent = parent;
      this.attrs = new Map();
      this.style = {};
      this.hidden = false;
      this.isConnected = true;
    }
    append(child) { child.parent = this; this.child = child; }
    setAttribute(key, value) { this.attrs.set(key, value); }
    getAttribute(key) { return this.attrs.get(key) ?? null; }
    removeAttribute(key) { this.attrs.delete(key); }
    contains(child) { for (; child; child = child.parent) if (child === this) return true; return false; }
    closest(selector) {
      for (let node = this; node; node = node.parent) {
        if (node.attrs.has(selector.slice(1, -1))) return node;
      }
      return null;
    }
    getClientRects() { return this.hidden ? [] : [this.getBoundingClientRect()]; }
    getBoundingClientRect() {
      return this.id === 'ui-help-tooltip'
        ? {width: 280, height: 80}
        : {left: 350, top: 700, bottom: 722, width: 22};
    }
  }
  const handlers = new Map();
  const body = new Element();
  let observe;
  const document = {
    body,
    createElement: () => new Element(),
    addEventListener: (type, callback) => handlers.set(type, callback),
  };
  const context = {
    document,
    window: {innerWidth: 390, innerHeight: 744, addEventListener() {}},
    setTimeout, clearTimeout,
    MutationObserver: class { constructor(callback) { observe = callback; } observe() {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/ui-help.js'), 'utf8'), context);
  const control = new Element(body);
  control.setAttribute('data-tooltip', '说明 <img onerror=alert(1)>');
  return {control, tooltip: body.child, emit: (type, event) => handlers.get(type)(event), observe, Element};
}

test('keyboard help preserves an existing description and Escape restores it', () => {
  const {control, tooltip, emit} = setup();
  control.setAttribute('aria-describedby', 'field-error');
  emit('focusin', {target: control});
  assert.equal(tooltip.hidden, false);
  assert.equal(tooltip.textContent, '说明 <img onerror=alert(1)>');
  assert.equal(control.getAttribute('aria-describedby'), 'field-error ui-help-tooltip');
  let prevented = false;
  let stopped = false;
  emit('keydown', {key: 'Escape', preventDefault() { prevented = true; }, stopImmediatePropagation() { stopped = true; }});
  assert.equal(tooltip.hidden, true);
  assert.equal(control.getAttribute('aria-describedby'), 'field-error');
  assert.ok(prevented && stopped);
});

test('help stays inside a narrow viewport and flips above a low control', () => {
  const {control, tooltip, emit} = setup();
  emit('pointerover', {target: control});
  assert.equal(tooltip.style.left, '98px');
  assert.equal(tooltip.style.top, '612px');
});

test('the pointer can enter the tooltip before it dismisses', async () => {
  const {control, tooltip, emit} = setup();
  emit('pointerover', {target: control});
  emit('pointerout', {target: control, relatedTarget: tooltip});
  emit('pointerover', {target: tooltip});
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.equal(tooltip.hidden, false);
  emit('pointerout', {target: tooltip, relatedTarget: null});
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.equal(tooltip.hidden, true);
  assert.equal(control.getAttribute('aria-describedby'), null);
});

test('switching views or modal layers clears help for a hidden or inert anchor', () => {
  const {control, tooltip, emit, observe} = setup();
  emit('click', {target: control});
  control.setAttribute('inert', '');
  observe();
  assert.equal(tooltip.hidden, true);
  assert.equal(control.getAttribute('aria-describedby'), null);
});

test('an empty conditional description dismisses the previous tooltip', () => {
  const {control, tooltip, emit} = setup();
  emit('focusin', {target: control});
  control.setAttribute('data-tooltip', '');
  emit('focusin', {target: control});
  assert.equal(tooltip.hidden, true);
  assert.equal(control.getAttribute('aria-describedby'), null);
});
