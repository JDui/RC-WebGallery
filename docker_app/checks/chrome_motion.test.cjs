const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(callback);
  }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }
  fire(name, event = {}) { this.listeners.get(name)?.forEach(callback => callback(event)); }
}

class Element extends Target {
  constructor(tag = 'DIV') {
    super();
    this.tagName = tag;
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.isConnected = true;
    this.inert = false;
    this.textContent = 'Heading';
    this.computed = { visibility: 'visible', opacity: '1', transform: 'none' };
    this.rect = { left: 10, top: 20, width: 200, height: 80, bottom: 100 };
    this.rectReads = 0;
    this.animations = [];
    this.queries = new Map();
    const values = new Map(), classes = new Set();
    this.style = { display: '', getPropertyValue: key => values.get(key) || '',
      setProperty: (key, value) => values.set(key, value), removeProperty: key => values.delete(key) };
    this.classList = { contains: key => classes.has(key), remove: key => classes.delete(key),
      toggle(key, value) { if (value) classes.add(key); else classes.delete(key); } };
  }
  append(element) { element.parentElement = this; this.children.push(element); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  hasAttribute(key) { return this.attributes.has(key); }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  removeAttribute(key) { this.attributes.delete(key); }
  getClientRects() { return this.hidden ? [] : [this.rect]; }
  getBoundingClientRect() { this.rectReads += 1; return this.rect; }
  querySelector(selector) { return this.queries.get(selector) || null; }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  closest(selector) {
    if ((selector === '[data-tooltip]' && this.hasAttribute('data-tooltip')) || (selector === '[inert]' && this.inert)) return this;
    return this.parentElement?.closest(selector) || null;
  }
  animate(frames, options) {
    const animation = new Target();
    Object.assign(animation, { frames, options, cancelled: false,
      cancel() { this.cancelled = true; this.fire('cancel'); } });
    this.animations.push(animation);
    return animation;
  }
}

function fixture(script) {
  const root = new Element('HTML'), body = new Element('BODY'); root.dataset = {};
  root.append(body);
  const document = new Target(), window = new Target();
  const frames = new Map(), timers = new Map(), observers = []; let id = 0;
  const reduced = new Target(), fine = new Target(); reduced.matches = false; fine.matches = true;
  Object.assign(document, { documentElement: root, body, readyState: 'loading', createElement: tag => new Element(tag),
    querySelector: () => null, querySelectorAll: () => [] });
  class Observer {
    constructor(callback) { this.callback = callback; this.targets = new Map(); observers.push(this); }
    observe(element, options) { this.targets.set(element, options); }
    disconnect() { this.targets.clear(); }
  }
  Object.assign(window, { innerWidth: 1280, innerHeight: 720,
    requestAnimationFrame: callback => { frames.set(++id, callback); return id; },
    cancelAnimationFrame: key => frames.delete(key),
    setTimeout: callback => { timers.set(++id, callback); return id; }, clearTimeout: key => timers.delete(key),
    matchMedia: query => query.includes('reduced-motion') ? reduced : fine,
    getComputedStyle: element => element.computed,
    MutationObserver: Observer, ResizeObserver: Observer });
  const context = vm.createContext({ window, document, MutationObserver: Observer, ResizeObserver: Observer,
    setTimeout: window.setTimeout, clearTimeout: window.clearTimeout, console });
  function run() {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static', script), 'utf8'), context);
  }
  function frame() { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback()); }
  return { window, document, root, body, reduced, fine, frames, timers, observers, run, frame };
}

function glass() {
  const state = fixture('liquid-glass.js');
  const workspace = new Element(), stack = new Element(), sidebar = new Element(), topbar = new Element();
  const first = new Element('SECTION'), second = new Element('SECTION');
  const firstHeading = new Element(), secondHeading = new Element(), label = new Element(), title = new Element();
  first.setAttribute('x-transition:enter', 'view-enter'); second.setAttribute('x-transition:enter', 'view-enter');
  first.queries.set('.section-heading > div:first-child', firstHeading);
  second.queries.set('.section-heading > div:first-child', secondHeading);
  second.inert = true;
  stack.append(first); stack.append(second); workspace.append(topbar); workspace.append(stack);
  workspace.queries.set('.view-stack', stack);
  const queries = new Map([['.workspace', workspace], ['.topbar-label', label], ['.topbar-title-row > h2', title]]);
  state.document.querySelector = selector => queries.get(selector) || null;
  state.document.querySelectorAll = selector => selector === '.sidebar, .topbar' ? [sidebar, topbar] : [];
  state.run(); state.document.fire('alpine:initialized'); state.frame();
  return { ...state, first, second, firstHeading, secondHeading, title, label, sidebar, topbar,
    navigate(key) {
      first.inert = true; second.inert = false;
      state.window.GalleryGlass.refresh({ viewChanged: true, navigationKey: key }); state.frame();
    } };
}

test('navigation discovers nested views, animates only their copy and does not replay on refresh', () => {
  const state = glass();
  state.navigate('second');
  assert.equal(state.secondHeading.animations.length, 1);
  assert.equal(state.firstHeading.animations.length, 0);
  assert.equal(state.second.animations.length, 0, 'Alpine remains the only owner of the section fade');
  assert.equal(state.title.animations.length, 1);
  assert.equal(state.title.animations[0].options.fill, 'backwards', 'staggered copy cannot flash at full opacity before starting');
  state.window.GalleryGlass.refresh({ navigationKey: 'second' }); state.frame();
  state.window.GalleryGlass.refresh({ viewChanged: true, navigationKey: 'second' }); state.frame();
  assert.equal(state.title.animations.length, 1);
});

test('rapid navigation continues rendered heading opacity and transform without replaying its delay', () => {
  const state = glass(); state.navigate('one');
  state.title.computed.opacity = '.73';
  state.title.computed.transform = 'matrix(1, 0, 0, 1, 0, 2)';
  const first = state.title.animations[0];
  state.navigate('two');
  const second = state.title.animations[1];
  assert.equal(first.cancelled, true);
  assert.equal(second.frames[0].opacity, '.73');
  assert.equal(second.frames[0].transform, 'matrix(1, 0, 0, 1, 0, 2)');
  assert.equal(second.options.delay, 0);
});

test('pointer bursts perform one geometry read per surface and frame using the final position', () => {
  const state = glass();
  for (let index = 0; index < 30; index += 1) {
    state.sidebar.fire('pointermove', { pointerType: 'mouse', clientX: 20 + index, clientY: 40 });
  }
  assert.equal(state.sidebar.rectReads, 0, 'raw pointer events must not force synchronous geometry');
  state.frame();
  assert.equal(state.sidebar.rectReads, 1);
  assert.equal(state.sidebar.style.getPropertyValue('--glass-pointer-x'), '19.5%');
  state.sidebar.fire('pointerleave'); state.frame();
  assert.equal(state.sidebar.rectReads, 1, 'resetting the highlight needs no geometry read');
  assert.equal(state.sidebar.style.getPropertyValue('--glass-pointer-x'), '50%');
});

test('motion preference changes and unsupported animations leave navigation and cleanup usable', () => {
  const state = glass(); state.navigate('one');
  state.reduced.matches = true; state.reduced.fire('change');
  assert.equal(state.title.animations[0].cancelled, true);
  state.navigate('two'); assert.equal(state.title.animations.length, 1);
  state.reduced.matches = false;
  state.title.animate = () => { throw new Error('unsupported effect'); };
  assert.doesNotThrow(() => state.navigate('three'));
  state.sidebar.fire('pointermove', { pointerType: 'mouse', clientX: 50, clientY: 40 });
  state.window.fire('pagehide');
  assert.equal(state.frames.size, 0);
  assert.equal(state.timers.size, 0);
  assert.equal(state.observers.every(observer => observer.targets.size === 0), true);
});

test('help observes only its open source chain and dismisses removed or hidden anchors', () => {
  const state = fixture('ui-help.js');
  const panel = new Element(), button = new Element('BUTTON');
  state.body.append(panel); panel.append(button); button.setAttribute('data-tooltip', 'Help');
  state.run();
  const observer = state.observers[0], tooltip = state.body.children.at(-1);
  assert.equal(observer.targets.size, 0, 'closed help must not observe all media style writes');
  state.document.fire('pointerover', { target: button });
  assert.equal(tooltip.hidden, false);
  assert.equal(observer.targets.has(button), true);
  assert.equal(observer.targets.get(panel).childList, true);
  assert.equal([...observer.targets.values()].some(options => options.subtree), false);
  const reads = button.rectReads;
  state.document.fire('pointerover', { target: button });
  assert.equal(button.rectReads, reads, 'pointer movement within one help button should not remeasure');
  button.isConnected = false; observer.callback();
  assert.equal(tooltip.hidden, true);
  assert.equal(button.hasAttribute('aria-describedby'), false);
  assert.equal(observer.targets.size, 0);
  button.isConnected = true;
  state.document.fire('focusin', { target: button });
  button.computed.visibility = 'hidden'; observer.callback();
  assert.equal(tooltip.hidden, true);
});

test('scrolling a long help tooltip preserves it while scrolling the page dismisses it', () => {
  const state = fixture('ui-help.js'), button = new Element('BUTTON');
  state.body.append(button); button.setAttribute('data-tooltip', 'Long help'); state.run();
  const tooltip = state.body.children.at(-1);
  state.document.fire('click', { target: button });
  state.document.fire('scroll', { target: tooltip });
  assert.equal(tooltip.hidden, false);
  state.document.fire('scroll', { target: state.document });
  assert.equal(tooltip.hidden, true);
});
