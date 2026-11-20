const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const tick = () => new Promise(resolve => setImmediate(resolve));

function setup({ ResizeObserver } = {}) {
  const timers = new Map(), frames = new Map(), scrolls = [], animations = [];
  let id = 0, app, domStart = 0, domEstimate = 320;
  const anchorEstimates = [];
  const classes = () => ({ toggle() {}, add() {}, remove() {} });
  const root = { style: {}, classList: classes(), dataset: {} };
  const body = { style: { width: 'calc(100% - 5px)', paddingRight: '3px' }, classList: classes(),
    getBoundingClientRect: () => ({ width: 1185 }) };
  const grid = { opacity: '1', animate(keyframes, options) {
    let resolve, reject;
    const animation = { keyframes, options, cancelled: false,
      finished: new Promise((yes, no) => { resolve = yes; reject = no; }),
      finish() { resolve(); }, cancel() { this.cancelled = true; reject(new Error('cancelled')); } };
    animations.push(animation); return animation;
  } };
  const cards = Array.from({ length: 12 }, (_, index) => ({
    getAttribute: () => `item-${index}`,
    getBoundingClientRect() {
      if (index === 2) anchorEstimates.push(domEstimate);
      const top = (index - domStart) * 200 + domStart / 2 * domEstimate - window.scrollY;
      return { top, bottom: top + 200, width: 200, height: 200 };
    },
  }));
  const document = { documentElement: root, body,
    querySelector(selector) {
      if (selector === '.settings-grid') return grid;
      if (selector === '.gallery-page') return { getBoundingClientRect: () => ({ height: 1280 }) };
      if (selector === '.gallery-virtual-page') return { getBoundingClientRect: () => ({ top: -window.scrollY }) };
      const match = selector.match(/^\[data-gallery-key="item-(\d+)"\]$/);
      return match ? cards[Number(match[1])] : null;
    },
    querySelectorAll: selector => selector.startsWith('.gallery-item-card') ? cards.slice(domStart, domStart + 4) : [],
  };
  const window = { scrollY: 500, pageYOffset: 0, innerWidth: 1200, innerHeight: 800,
    setTimeout(callback, delay) { timers.set(++id, { callback, delay }); return id; },
    clearTimeout(key) { timers.delete(key); },
    requestAnimationFrame(callback) { frames.set(++id, callback); return id; },
    cancelAnimationFrame(key) { frames.delete(key); },
    matchMedia: () => ({ matches: false }),
    getComputedStyle: element => ({ opacity: element.opacity || '1' }),
    scrollBy(options) { scrolls.push(options.top); window.scrollY += options.top; },
    scrollTo(x, y) { window.scrollY = typeof x === 'object' ? x.top : y; },
  };
  const context = vm.createContext({ window, document, localStorage: { getItem: () => null, setItem() {} },
    Date, Intl, URLSearchParams, AbortController, console, ResizeObserver,
    getComputedStyle: element => window.getComputedStyle(element) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  app = context.galleryApp();
  app.gallery = { items: Array.from({ length: 12 }, (_, index) => ({ item_key: `item-${index}` })), page_size: 2 };
  app.galleryWindowPageCount = 2;
  app.galleryRenderStart = 0; app.galleryRenderEnd = 4; app.galleryEstimatedPageHeight = 320;
  app.scrollDirection = 'down'; app.galleryPreloadDistance = () => 200;
  app.galleryItemKey = item => item?.item_key || '';
  app.cssEscape = value => value;
  for (const name of ['pruneGalleryWindowCaches', 'observeGalleryCards', 'observeGalleryLoadSentinel',
    'stopHoverPreviewByCard', 'scrollViewTop']) app[name] = () => {};
  app.$nextTick = callback => Promise.resolve(callback?.());
  return { app, window, document, timers, frames, scrolls, animations, grid, body, anchorEstimates,
    setDom(start, estimate) { domStart = start; domEstimate = estimate; },
    dom: () => ({ start: domStart, estimate: domEstimate }) };
}

function withAlpine(state) {
  class Element {}
  let initializing = true;
  const context = vm.createContext({ window: { Element }, Element, console, setTimeout, clearTimeout,
    document: { createElement: () => new Element() },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    queueMicrotask(callback) { if (!initializing) queueMicrotask(callback); } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/vendor/alpinejs.min.js'), 'utf8'), context);
  initializing = false;
  const Alpine = context.window.Alpine;
  const app = Alpine.reactive(state.app);
  app.$nextTick = Alpine.nextTick;
  // Model the template's reactive spacer/render bindings with Alpine's actual
  // effect scheduler, so a nested callback cannot pretend the DOM has flushed.
  const effect = Alpine.effect(() => state.setDom(app.galleryRenderStart, app.galleryEstimatedPageHeight));
  return { app, Alpine, dispose: () => Alpine.release(effect) };
}

test('virtual window compensation waits for measured spacer bindings to flush', async () => {
  const state = setup(), { app, Alpine, dispose } = withAlpine(state);
  try {
    app.shiftGalleryWindowIfNeeded();
    await Alpine.nextTick(); await tick();
    assert.equal(state.dom().estimate, 640);
    assert.deepEqual(state.anchorEstimates, [320, 640]);
    assert.deepEqual(state.scrolls, [240], 'anchor correction includes the final measured spacer size');
    assert.equal(state.window.scrollY, 740);
  } finally { dispose(); }
});

test('virtual window height is measured after the narrow masonry spans are installed', async () => {
  const state = setup(), { app, Alpine, dispose } = withAlpine(state);
  const query = state.document.querySelector;
  let masonryReady = false;
  state.document.querySelector = selector => selector === '.gallery-page'
    ? { getBoundingClientRect: () => ({ height: masonryReady ? 1280 : 1920 }) } : query(selector);
  app.observeGalleryCards = () => { masonryReady = true; };
  try {
    app.shiftGalleryWindowIfNeeded(); await Alpine.nextTick(); await tick();
    assert.equal(app.galleryEstimatedPageHeight, 640, 'use the committed masonry geometry, not its temporary normal rows');
    assert.deepEqual(state.scrolls, [240]);
  } finally { dispose(); }
});

for (const operation of ['batch', 'payload']) {
  test(`${operation} updates measure page height after installing masonry`, () => {
    const { app, document } = setup();
    const query = document.querySelector;
    let masonryReady = false;
    document.querySelector = selector => selector === '.gallery-page'
      ? { getBoundingClientRect: () => ({ height: masonryReady ? 1280 : 1920 }) } : query(selector);
    app.observeGalleryCards = () => { masonryReady = true; };
    if (operation === 'batch') app.queueGalleryBatch([{ item_key: 'new-item' }], app.galleryRenderToken);
    else app.applyGalleryPagePayload({ items: app.gallery.items, page_size: 2 }, 1, 'replace');
    assert.equal(app.galleryEstimatedPageHeight, 640);
  });
}

test('masonry batches geometry reads and does not rewrite unchanged spans or stale targets', () => {
  let observer;
  class ResizeObserver {
    constructor(callback) { this.callback = callback; observer = this; this.targets = new Set(); }
    observe(card) { this.targets.add(card); }
    unobserve(card) { this.targets.delete(card); }
    disconnect() { this.targets.clear(); }
  }
  const { app, document } = setup({ ResizeObserver });
  const events = [];
  function card(name, height, width = 180) {
    const attributes = new Set(); let span = '';
    return { isConnected: true, height, width,
      get offsetWidth() { events.push(`read-width:${name}`); return this.width; },
      get offsetHeight() { events.push(`read-height:${name}`); return this.height; },
      style: { get gridRowEnd() { return span; }, set gridRowEnd(value) { events.push(`write-span:${name}`); span = value; },
        removeProperty() { events.push(`write-clear:${name}`); span = ''; } },
      hasAttribute: attribute => attributes.has(attribute),
      setAttribute(attribute) { events.push(`write-attribute:${name}`); attributes.add(attribute); },
      removeAttribute(attribute) { events.push(`write-remove:${name}`); attributes.delete(attribute); },
    };
  }
  const first = card('first', 200), second = card('second', 320), hidden = card('hidden', 200, 0);
  hidden.style.gridRowEnd = 'span 20'; hidden.setAttribute('data-masonry-measured');
  let cards = [first, second, hidden];
  document.querySelectorAll = () => cards;
  app.narrowViewport = true;
  const assertBatched = () => {
    const firstWrite = events.findIndex(event => event.startsWith('write-'));
    assert.ok(firstWrite >= 0);
    assert.equal(events.slice(firstWrite).some(event => event.startsWith('read-')), false,
      'changing one card must not force the next card geometry read');
  };
  events.length = 0; app.observeNarrowMasonry(); assertBatched();
  assert.equal(first.style.gridRowEnd, 'span 18');
  assert.equal(second.style.gridRowEnd, 'span 28');
  assert.equal(hidden.style.gridRowEnd, 'span 20', 'hidden cards retain their last useful span');
  assert.equal(events.filter(event => event === 'read-height:first').length, 1);
  events.length = 0;
  observer.callback(cards.map(target => ({ target })));
  assert.equal(events.some(event => event.startsWith('write-')), false, 'unchanged resize delivery must not cause another layout');
  first.height = 260; events.length = 0;
  observer.callback(cards.map(target => ({ target }))); assertBatched();
  assert.equal(first.style.gridRowEnd, 'span 23');
  cards = [first, hidden]; app.observeNarrowMasonry(); events.length = 0;
  observer.callback([{ target: second }]);
  assert.equal(events.length, 0, 'queued entries for unobserved cards are ignored');
  app.narrowViewport = false; app.observeNarrowMasonry(); events.length = 0;
  observer.callback([{ target: first }]);
  assert.equal(events.length, 0, 'a late narrow-layout callback cannot rewrite a wide layout');
  assert.equal(first.style.gridRowEnd, '');
});

for (const reason of ['reset', 'navigation', 'request', 'modal', 'scroll']) {
  test(`a pending virtual-window correction cannot override ${reason}`, async () => {
    const state = setup(), { app, Alpine, dispose } = withAlpine(state);
    try {
      app.shiftGalleryWindowIfNeeded();
      if (reason === 'reset') app.resetGalleryWindowToTop();
      if (reason === 'navigation') app.currentView = 'settings';
      if (reason === 'request') app.galleryRenderToken += 1;
      if (reason === 'modal') { app.detail.open = true; app.bodyLockTop = state.window.scrollY; }
      if (reason === 'scroll') state.window.scrollY += 35;
      await Alpine.nextTick(); await tick();
      assert.deepEqual(state.scrolls, []);
      if (reason === 'scroll') assert.equal(state.window.scrollY, 535, 'keep the user/momentum scroll position');
      for (const timer of state.timers.values()) if (timer.delay === 80) timer.callback();
      assert.equal(app.galleryWindowShiftLock, false);
    } finally { dispose(); }
  });
}

test('an old window unlock cannot release a newer shift, even after a reset at the first page', async () => {
  const state = setup(), { app, Alpine, dispose } = withAlpine(state);
  try {
    app.shiftGalleryWindowIfNeeded(); await Alpine.nextTick(); await tick();
    const oldRelease = [...state.timers.values()].find(timer => timer.delay === 80).callback;
    app.resetGalleryWindowToTop(); await Alpine.nextTick();
    state.window.scrollY = 500;
    app.shiftGalleryWindowIfNeeded();
    assert.equal(app.galleryWindowShiftLock, true);
    oldRelease();
    assert.equal(app.galleryWindowShiftLock, true, 'stale timer does not own the new lock');
    app.resetGalleryWindowToTop();
    app.galleryWindowShiftRaf = state.window.requestAnimationFrame(() => {});
    app.resetGalleryWindowToTop();
    assert.equal(state.frames.size, 0, 'a reset with no remaining range change still cancels queued work');
    await Alpine.nextTick(); await tick();
  } finally { dispose(); }
});

test('virtual correction rechecks overlay state after the measurement flush', async () => {
  const state = setup(), { app, Alpine, dispose } = withAlpine(state);
  const measure = app.measureGalleryWindow;
  app.measureGalleryWindow = () => {
    measure.call(app);
    queueMicrotask(() => { app.videoNote.open = true; });
  };
  try {
    app.shiftGalleryWindowIfNeeded(); await Alpine.nextTick(); await tick();
    assert.deepEqual(state.scrolls, []);
    assert.equal(app.galleryWindowShiftLock, false);
  } finally { dispose(); }
});

test('settings transitions sample interrupted opacity and stale exits cannot commit another group', async () => {
  const { app, grid, animations } = setup();
  app.currentView = 'settings'; app.settingsGroup = 'general'; app.resolvedPerformanceMode = 'full';
  grid.opacity = '.4'; const first = app.setSettingsGroup('network');
  assert.equal(animations[0].keyframes[0].opacity, .4);
  grid.opacity = '.2'; const second = app.setSettingsGroup('media');
  assert.equal(animations[0].cancelled, true);
  assert.equal(animations[1].keyframes[0].opacity, .2);
  animations[1].finish(); await second; await first;
  assert.equal(app.settingsGroup, 'media');
  const entrance = animations[2];
  grid.opacity = '.35'; const third = app.setSettingsGroup('network');
  assert.equal(entrance.cancelled, true, 'entrances and exits share one owner');
  assert.equal(animations[3].keyframes[0].opacity, .35);
  animations[3].finish(); await third;
  assert.equal(app.settingsGroup, 'network');
});

test('returning to the displayed settings group reverses its fade instead of snapping to full opacity', async () => {
  const { app, grid, animations } = setup();
  app.currentView = 'settings'; app.settingsGroup = 'general'; app.resolvedPerformanceMode = 'full';
  const leaving = app.setSettingsGroup('network');
  grid.opacity = '.45'; await app.setSettingsGroup('general'); await leaving;
  assert.equal(app.settingsGroup, 'general');
  assert.equal(animations[0].cancelled, true);
  assert.equal(animations[1].keyframes[0].opacity, .45);
  assert.equal(animations[1].keyframes[1].opacity, 1);
});

test('settings commit survives a failed animation API', async () => {
  const { app, grid } = setup();
  app.currentView = 'settings'; app.settingsGroup = 'general'; app.resolvedPerformanceMode = 'full';
  grid.animate = () => { throw new Error('unsupported'); };
  await app.setSettingsGroup('network');
  assert.equal(app.settingsGroup, 'network');
});

test('modal scroll locking retains responsive width and restores the exact previous body style', () => {
  const { app, window, body } = setup();
  app.detail.open = true; app.syncBodyLock();
  assert.equal(body.style.width, 'calc(100% - 15px)');
  window.innerWidth = 800; app.syncBodyLock();
  assert.equal(body.style.width, 'calc(100% - 15px)', 'viewport changes remain relative while locked');
  app.detail.open = false; app.syncBodyLock();
  assert.equal(body.style.width, 'calc(100% - 5px)');
  assert.equal(body.style.paddingRight, '3px');
  assert.equal(app.bodyLockTop, null);
  assert.equal(window.scrollY, 500);
});

function fixedLayoutFixture({ viewportWidth = 1440, viewportHeight = 900, shellWidth = 1425,
  workspaceLeft = 228, workspaceWidth = 1181, sidebarWidth = 196 } = {}) {
  const state = setup();
  const { app, document, window } = state;
  const values = new Map();
  document.documentElement.style = {
    getPropertyValue: name => values.get(name) || '',
    setProperty: (name, value) => values.set(name, value),
    removeProperty: name => values.delete(name),
  };
  const shell = { getBoundingClientRect: () => ({ left: 0, width: shellWidth }) };
  const workspace = { getBoundingClientRect: () => ({ left: workspaceLeft, width: workspaceWidth }) };
  const topbar = { getBoundingClientRect: () => ({ height: 56 }) };
  app.$refs = { shell, workspace, topbar };
  window.innerWidth = viewportWidth;
  window.innerHeight = viewportHeight;
  window.matchMedia = () => ({ matches: true });
  window.getComputedStyle = element => element === shell
    ? { paddingTop: '16px', paddingLeft: '16px', paddingRight: '16px', columnGap: '16px',
      gridTemplateColumns: `${sidebarWidth}px ${workspaceWidth}px` }
    : element === topbar ? { top: '22px' } : { getPropertyValue: () => `${sidebarWidth}px` };
  return { ...state, values };
}

for (const geometry of [
  { name: 'reserved scrollbar', workspaceLeft: 228, workspaceWidth: 1181 },
  { name: 'collapsed sidebar', workspaceLeft: 88, workspaceWidth: 1321, sidebarWidth: 56 },
  { name: 'fractional tracks and modal width', workspaceLeft: 228.5, workspaceWidth: 1165.25, shellWidth: 1409.75 },
]) {
  test(`fixed topbar shares workspace edges with ${geometry.name}`, () => {
    const { app, values } = fixedLayoutFixture(geometry);
    app.updateFixedLayoutMetrics();
    assert.equal(values.get('--layout-topbar-left'), `${geometry.workspaceLeft}px`);
    assert.equal(values.get('--layout-topbar-width'), `${geometry.workspaceWidth}px`);
    assert.equal(values.get('--page-toolbar-top'), '78px', 'sticky actions use the newly pinned header position');
  });
}

test('a short desktop viewport keeps the entire sidebar inside its vertical insets', () => {
  const { app, values } = fixedLayoutFixture({ viewportHeight: 200 });
  app.updateFixedLayoutMetrics();
  assert.equal(values.get('--layout-sidebar-top'), '16px');
  assert.equal(values.get('--layout-sidebar-height'), '168px', 'the old 240px floor must not put navigation below the viewport');
});
