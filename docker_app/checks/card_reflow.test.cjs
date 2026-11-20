const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup({ reduced = false, low = false } = {}) {
  const timers = new Map(), cards = [], events = [];
  let nextTimer = 0, detailEntering = false;
  const window = {
    innerHeight: 800, matchMedia: () => ({ matches: reduced }),
    setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
  };
  const document = {
    querySelectorAll: () => cards,
    querySelector: () => ({ hasAttribute: () => detailEntering }),
  };
  const context = vm.createContext({ window, document, console, Date, Intl, URLSearchParams,
    localStorage: { getItem: () => null } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  const app = context.galleryApp();
  app.resolvedPerformanceMode = low ? 'low' : 'full';
  app.$nextTick = fn => Promise.resolve().then(() => fn?.());
  function card(key, top) {
    const element = {
      key, top, offset: 0, isConnected: true, animations: [],
      style: { transform: 'scale(1.02)', transition: 'transform 200ms ease' },
      classList: { contains: () => false },
      getAttribute: () => key,
      getBoundingClientRect() {
        events.push(`read:${key}`);
        return { left: 0, top: this.top + this.offset, bottom: this.top + this.offset + 100, width: 100, height: 100 };
      },
      animate(frames, options) {
        events.push(`write:${key}`);
        let resolve, reject;
        const animation = { frames, options, cancelled: false,
          finished: new Promise((yes, no) => { resolve = yes; reject = no; }),
          finish: () => resolve(),
          cancel: () => {
            animation.cancelled = true;
            if (this.active === animation) { this.active = null; this.offset = 0; }
            reject(new Error('cancelled'));
          },
        };
        this.active = animation;
        this.offset = Number.parseFloat(frames[0].translate.split(' ')[1]);
        this.animations.push(animation);
        return animation;
      },
    };
    cards.push(element);
    return element;
  }
  return { app, card, cards, timers, events, setDetailEntering: value => { detailEntering = value; } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('reflow measures the entire layout before animation writes and preserves hover declarations', async () => {
  const { app, card, events } = setup();
  const first = card('a', 40), second = card('b', 170);
  app.animateGalleryReflow(() => { first.top = 90; second.top = 300; events.length = 0; });
  await tick();
  assert.deepEqual(events, ['read:a', 'read:b', 'write:a', 'write:b']);
  assert.equal(first.animations[0].frames[0].translate, '0px -50px');
  assert.equal(first.style.transform, 'scale(1.02)');
  assert.equal(first.style.transition, 'transform 200ms ease');
  first.animations[0].finish(); second.animations[0].finish();
  await tick();
  assert.equal(first.style.transform, 'scale(1.02)');
});

test('overlapping reflows continue from rendered pixels and stale cleanup cannot cancel the replacement', async () => {
  const { app, card, timers } = setup();
  const element = card('a', 50);
  app.animateGalleryReflow(() => { element.top = 150; });
  await tick();
  const first = element.animations[0];
  const staleTimeout = [...timers.values()][0];
  element.offset = -40;
  app.animateGalleryReflow(() => { element.top = 200; });
  await tick();
  const second = element.animations[1];
  assert.equal(first.cancelled, true);
  assert.equal(second.frames[0].translate, '0px -90px');
  staleTimeout(); first.finish();
  await tick();
  assert.equal(second.cancelled, false);
  second.finish(); await tick();
  assert.equal(timers.size, 0);
});

test('only the latest pending reflow may animate, and navigating away drops pending effects', async () => {
  const { app, card } = setup();
  const element = card('a', 50);
  app.animateGalleryReflow(() => { element.top = 100; });
  app.animateGalleryReflow(() => { element.top = 200; });
  await tick();
  assert.equal(element.animations.length, 1);
  app.animateGalleryReflow(() => { element.top = 300; });
  app.currentView = 'settings';
  await tick();
  assert.equal(element.animations.length, 1);
});

test('reduced motion, low performance and covered galleries commit without geometry work', async () => {
  for (const options of [{ reduced: true }, { low: true }, { covered: true }]) {
    const { app, card, events } = setup(options);
    const element = card('a', 50);
    if (options.covered) app.bodyLockTop = 500;
    app.animateGalleryReflow(() => { element.top = 100; });
    await tick();
    assert.equal(element.top, 100);
    assert.equal(element.animations.length, 0);
    assert.deepEqual(events, []);
  }
});

test('detail payload reflow does not compete with its parent entrance', async () => {
  const { app, card, setDetailEntering } = setup();
  const element = card('a', 50);
  app.detail.open = true;
  setDetailEntering(true);
  app.animateDetailReflow(() => { element.top = 100; });
  await tick();
  assert.equal(element.animations.length, 0);
  setDetailEntering(false);
  app.animateDetailReflow(() => { element.top = 200; });
  await tick();
  assert.equal(element.animations.length, 1);
  assert.equal(element.animations[0].options.duration, 360);
});

test('offscreen cards and unsupported animation APIs cannot delay the committed data', async () => {
  const { app, card } = setup();
  const offscreen = card('offscreen', 1400), visible = card('visible', 50);
  visible.animate = () => { throw new Error('unsupported'); };
  app.animateGalleryReflow(() => { offscreen.top = 1300; visible.top = 150; });
  await tick();
  assert.equal(offscreen.animations.length, 0);
  assert.equal(visible.top, 150);
});

test('a failed review-row animation cannot strand removal completion or its pending state', async () => {
  const { app, card, timers } = setup();
  app.currentView = 'review';
  app.reviewItems = [{ id: 1 }, { id: 2 }];
  app.sidebarCounts.review = 2;
  const remaining = card('2', 200);
  Object.defineProperty(remaining, 'top', { get: () => app.reviewItems.length === 2 ? 200 : 100 });
  remaining.animate = () => { throw new Error('unsupported'); };
  const removal = app.animateReviewItemRemoval(1);
  [...timers.values()][0]();
  await removal;
  await tick();
  assert.equal(app.reviewItems.length, 1);
  assert.equal(app.reviewItems[0].id, 2);
  assert.equal(app.sidebarCounts.review, 1);
  assert.equal(app.reviewRemovingIds['1'], undefined);
});
