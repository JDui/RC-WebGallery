const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/static/layer-motion.js'), 'utf8');
const fullTransform = 'translate3d(0px, 0px, 0) scale(1)';

function setup({ reduced = false, low = false } = {}) {
  let id = 0;
  const timers = new Map(), frames = new Map();
  const document = { documentElement: { dataset: { performanceMode: low ? 'low' : 'full' } }, body: {} };
  const window = {
    innerWidth: 1600, innerHeight: 1000,
    getComputedStyle: element => element.computed,
    matchMedia: () => ({ matches: reduced }),
    setTimeout(fn) { timers.set(++id, fn); return id; },
    clearTimeout(key) { timers.delete(key); },
    requestAnimationFrame(fn) { frames.set(++id, fn); return id; },
    cancelAnimationFrame(key) { frames.delete(key); },
  };
  class Element {
    constructor(rect, attributes = {}) {
      this.rect = rect;
      this.attributes = new Map(Object.entries(attributes));
      this.computed = { display: 'block', visibility: 'visible', opacity: '1', transform: 'none' };
      this.isConnected = true;
      this.parentElement = null;
      this.animations = [];
      this.reads = 0;
    }
    getBoundingClientRect() { this.reads++; return this.rect; }
    getClientRects() { return this.isConnected ? [this.rect] : []; }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    closest() { return this; }
    animate(keyframes, options) {
      let resolve, reject;
      const finished = new Promise((yes, no) => { resolve = yes; reject = no; });
      const animation = { keyframes, options, finished, cancelled: false, settled: false,
        finish: () => {
          if (animation.settled) return;
          animation.settled = true;
          this.computed = { ...this.computed, ...keyframes.at(-1) };
          resolve();
        },
        cancel: () => {
          animation.cancelled = true;
          if (!animation.settled) { animation.settled = true; reject(new Error('cancelled')); }
          this.computed = { ...this.computed, opacity: '1', transform: 'none' };
        },
      };
      this.animations.push(animation);
      return animation;
    }
  }
  const context = vm.createContext({ window, document, console });
  vm.runInContext(source, context);
  const element = new Element({ left: 0, top: 0, width: 1000, height: 800 });
  const backdrop = new Element({ left: 0, top: 0, width: 1600, height: 1000 });
  const card = new Element({ left: 100, top: 200, width: 200, height: 120 }, { 'data-gallery-key': 'post-1' });
  const finish = (...elements) => elements.forEach(item => item.animations.forEach(animation => animation.finish()));
  const frame = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()); };
  return { motion: window.galleryLayerMotion, window, document, Element, element, backdrop, card, finish, frame, timers };
}

test('enter expands uniformly from the captured visible card and releases completed animations', async () => {
  const { motion, element, backdrop, card, finish, timers } = setup();
  element.setAttribute('data-layer-motion', 'detail');
  const origin = motion.capture(card);
  const promise = motion.enter({ key: 'detail', element, backdrop, origin });
  const animation = element.animations[0];
  assert.equal(animation.keyframes[0].transform, 'translate3d(-300.00px, -140.00px, 0) scale(0.15000)');
  assert.equal(animation.keyframes[1].transform, fullTransform);
  assert.equal(animation.options.duration, 300);
  finish(element, backdrop);
  assert.equal(await promise, true);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
  assert.equal(element.getAttribute('data-layer-motion'), 'detail');
  assert.equal(animation.cancelled, true);
  assert.equal(timers.size, 0);
});

test('exit resolves the current source position and holds the last frame until the caller can hide the layer', async () => {
  const { motion, element, backdrop, card, finish, frame } = setup();
  const origin = motion.capture(card, () => card);
  card.rect = { left: 800, top: 700, width: 200, height: 120 };
  card.inert = true;
  const promise = motion.exit({ key: 'detail', element, backdrop, origin });
  const animation = element.animations[0];
  assert.equal(animation.keyframes[1].transform, 'translate3d(400.00px, 360.00px, 0) scale(0.15000)');
  finish(element, backdrop);
  assert.equal(await promise, true);
  assert.equal(animation.cancelled, false);
  assert.equal(element.getAttribute('data-layer-motion-active'), 'exit');
  frame();
  assert.equal(animation.cancelled, true);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
});

test('gone, recycled, hidden and scroll-clipped return targets fall back to a stationary fade', async () => {
  const cases = [
    ({ card }) => { card.isConnected = false; },
    ({ card }) => { card.setAttribute('data-gallery-key', 'post-2'); },
    ({ card }) => { card.rect.top = 1200; },
    ({ card }) => { card.computed.visibility = 'hidden'; },
    ({ card, Element }) => {
      card.parentElement = new Element({ left: 0, top: 0, width: 1000, height: 100 });
      card.parentElement.computed.overflowY = 'auto';
    },
  ];
  for (const mutate of cases) {
    const state = setup();
    const origin = state.motion.capture(state.card);
    mutate(state);
    const promise = state.motion.exit({ key: 'detail', element: state.element, origin });
    assert.equal(state.element.animations[0].keyframes[1].transform, fullTransform);
    assert.equal(state.element.animations[0].keyframes[1].opacity, 0);
    state.finish(state.element);
    assert.equal(await promise, true);
    state.frame();
  }
});

test('explicit source validation rejects a recycled resolver result while a remounted matching source works', async () => {
  const state = setup();
  let current = state.card;
  const origin = state.motion.capture(state.card, { resolve: () => current,
    identity: node => node?.getAttribute('data-gallery-key') === 'post-1' });
  current = new state.Element({ left: 400, top: 200, width: 200, height: 120 }, { 'data-gallery-key': 'post-1' });
  let promise = state.motion.exit({ key: 'detail', element: state.element, origin });
  assert.notEqual(state.element.animations.at(-1).keyframes[1].transform, fullTransform);
  state.finish(state.element); await promise; state.frame();
  current.setAttribute('data-gallery-key', 'post-2');
  promise = state.motion.exit({ key: 'detail', element: state.element, origin });
  assert.equal(state.element.animations.at(-1).keyframes[1].transform, fullTransform);
  state.finish(state.element); await promise; state.frame();
});

test('an interrupted entrance reverses from its rendered transform and cannot finish a newer exit', async () => {
  const { motion, element, backdrop, card, finish, frame } = setup();
  const origin = motion.capture(card);
  const entering = motion.enter({ key: 'detail', element, backdrop, origin });
  element.computed.transform = 'matrix(0.5, 0, 0, 0.5, -150, -70)';
  element.computed.opacity = '.6';
  backdrop.computed.opacity = '.4';
  const exiting = motion.exit({ key: 'detail', element, backdrop, origin });
  assert.equal(await entering, false);
  assert.equal(element.animations[1].keyframes[0].transform, 'matrix(0.5, 0, 0, 0.5, -150, -70)');
  assert.equal(element.animations[1].keyframes[0].opacity, .6);
  assert.equal(backdrop.animations[1].keyframes[0].opacity, .4);
  assert.equal(element.getAttribute('data-layer-motion-active'), 'exit');
  finish(element, backdrop);
  assert.equal(await exiting, true);
  frame();
});

test('reopening cancels an exit and stale cleanup never removes the new animation owner', async () => {
  const { motion, element, card, finish, frame } = setup();
  const origin = motion.capture(card);
  const exiting = motion.exit({ key: 'detail', element, origin });
  const entering = motion.enter({ key: 'detail', element, origin });
  assert.equal(await exiting, false);
  frame();
  assert.equal(element.getAttribute('data-layer-motion-active'), 'enter');
  finish(element);
  assert.equal(await entering, true);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
});

test('reduced motion and low performance use short opacity-only transitions', async () => {
  for (const options of [{ reduced: true }, { low: true }]) {
    const { motion, element, card, finish, frame } = setup(options);
    const origin = motion.capture(card);
    let promise = motion.enter({ key: 'detail', element, origin });
    let animation = element.animations.at(-1);
    assert.equal(animation.keyframes[0].transform, fullTransform);
    assert.equal(animation.keyframes[1].transform, fullTransform);
    assert.equal(animation.options.duration, options.reduced ? 80 : 140);
    finish(element); await promise;
    promise = motion.exit({ key: 'detail', element, origin });
    animation = element.animations.at(-1);
    assert.equal(animation.keyframes[1].transform, fullTransform);
    assert.equal(animation.options.duration, options.reduced ? 60 : 120);
    finish(element); await promise; frame();
  }
});

test('hidden pending surfaces still get measured and unavailable WAAPI never blocks closing', async () => {
  const { motion, element, backdrop, card, finish } = setup();
  element.computed.visibility = 'hidden';
  const promise = motion.enter({ key: 'detail', element, origin: motion.capture(card) });
  assert.equal(element.animations.length, 1);
  finish(element); assert.equal(await promise, true);
  element.animate = undefined;
  assert.equal(await motion.exit({ key: 'detail', element, backdrop }), true);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
});

test('a broken animation API or missing completion cannot strand a layer or leak rejected promises', async () => {
  const { motion, element, backdrop, card, finish, frame, timers } = setup();
  backdrop.animate = () => { throw new Error('unsupported'); };
  let promise = motion.enter({ key: 'detail', element, backdrop, origin: motion.capture(card) });
  assert.equal(await promise, true);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
  promise = motion.exit({ key: 'detail', element, origin: motion.capture(card) });
  [...timers.values()][0]();
  assert.equal(await promise, true);
  frame();
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
  finish(element);
});

test('cancelAll settles every layer once and removes pending animation work', async () => {
  const { motion, element, backdrop, card, timers } = setup();
  const first = motion.enter({ key: 'detail', element, origin: motion.capture(card) });
  const second = motion.enter({ key: 'viewer', element: backdrop, origin: motion.capture(card) });
  motion.cancelAll();
  assert.equal(await first, false);
  assert.equal(await second, false);
  assert.equal(timers.size, 0);
  assert.equal(element.getAttribute('data-layer-motion-active'), null);
  assert.equal(backdrop.getAttribute('data-layer-motion-active'), null);
});
