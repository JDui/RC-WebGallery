const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  const calls = [], exits = [], timers = new Map(); let timerId = 0;
  const cards = new Map(), detailCards = new Map();
  const player = { paused: false, released: false, pause() { this.paused = true; },
    removeAttribute() { this.released = true; }, load() {} };
  const motion = {
    capture(element, options = {}) { return { element, ...options }; },
    enter(options) { calls.push(options); return Promise.resolve(true); },
    exit(options) { return new Promise(resolve => exits.push({ ...options, resolve })); },
    cancel() {},
  };
  const document = {
    documentElement: { dataset: {} }, activeElement: null,
    querySelector: selector => selector.includes('video.is-current') ? player : {},
    querySelectorAll: selector => selector === '[data-motion-folder]' || selector === '[data-gallery-key]'
      ? [...cards.values()] : selector === '[data-detail-key]' ? [...detailCards.values()] : [],
  };
  const context = vm.createContext({
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    Date, Intl, URLSearchParams, AbortController, console, document,
    window: { galleryLayerMotion: motion, innerWidth: 1600, innerHeight: 900,
      matchMedia: () => ({ matches: false }), getComputedStyle: () => ({ visibility: 'visible' }),
      setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  const app = context.galleryApp();
  app.$refs = {};
  app.$nextTick = fn => fn ? Promise.resolve(fn()) : Promise.resolve();
  for (const name of ['syncBodyLock', 'syncModalAccessibility', 'setInteracting', 'pauseCurrentViewerMedia', 'resetDetailWindow',
    'scrollDetailTop', 'scheduleIdleWork', 'loadViewerOriginal', 'startVideoNote']) app[name] = () => {};
  app.animateDetailReflow = update => update();
  app.cachedDetail = () => null;
  app.cacheDetailPayload = () => {};
  function card(folder, index = null) {
    const element = { isConnected: true, dataset: { motionFolder: folder,
      galleryKey: index ? `${folder}::${index}` : folder, detailKey: `${folder}::${index}` },
      getBoundingClientRect: () => ({ left: 30, top: 80, width: 200, height: 240, right: 230, bottom: 320 }),
      querySelector: () => null };
    (index ? detailCards : cards).set(index ? `${folder}::${index}` : folder, element);
    return element;
  }
  return { app, calls, exits, timers, player, card, cards, detailCards, motion };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const folder = name => ({ folder_name: name, title: name, image_count: 1, asset_count: 1 });
const pair = index => ({ pair_index: index, image: { url: `original-${index}`, small_thumb_url: `thumb-${index}` } });

test('detail enters once from its card and does not replay when the full payload arrives', async () => {
  const { app, calls, exits, card } = setup();
  const first = card('first'), second = card('second');
  let resolve;
  app.api = () => new Promise(done => { resolve = done; });
  const opening = app.openFolder('first', folder('first'));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, 'detail');
  assert.equal(calls[0].origin.element, first);
  assert.equal(app.layerMotionPending.detail, false);
  resolve({ folder: folder('first'), pairs: [pair(1)], videos: [] });
  await opening;
  assert.equal(calls.length, 1);
  app.detail.folder = folder('second');
  app.closeDetail();
  assert.equal(exits[0].origin.resolve(), second, 'return follows the currently displayed post');
  assert.equal(app.detail.pairs.length, 1, 'outgoing content remains until motion ends');
  exits[0].resolve(true); await tick();
  assert.equal(app.detailClosing, false);
  assert.equal(app.detail.folder, null);
});

test('a stale detail exit cannot clear a reopened detail or its pending entrance', async () => {
  const { app, calls, exits, card } = setup();
  card('first'); card('second');
  app.cachedDetail = name => ({ folder: folder(name), pairs: [pair(1)], videos: [] });
  await app.openFolder('first', folder('first'));
  app.closeDetail();
  await app.openFolder('second', folder('second'));
  exits[0].resolve(true); await tick();
  assert.equal(app.detail.open, true);
  assert.equal(app.detail.folder.folder_name, 'second');
  assert.equal(calls.filter(call => call.key === 'detail').length, 2);
  assert.equal(app.layerMotionPending.detail, false);
});

test('repeated copies of a post preserve the actual clicked source and reject recycled identities', async () => {
  const { app, calls, exits, card, cards } = setup();
  const first = card('post'), clicked = card('post');
  cards.clear(); cards.set('first', first); cards.set('clicked', clicked);
  app.cachedDetail = () => ({ folder: folder('post'), pairs: [pair(1)], videos: [] });
  await app.openFolder('post', folder('post'), { currentTarget: { closest: () => clicked } });
  assert.equal(calls[0].origin.element, clicked);
  app.closeDetail();
  assert.equal(exits[0].origin.resolve(), clicked, 'return to the clicked copy, not the first match');
  clicked.dataset.motionFolder = 'recycled';
  assert.equal(exits[0].origin.resolve(), null, 'a reused source node cannot become another destination');
  exits[0].resolve(true); await tick();
});

test('video loading and playback share one entrance and retain their paused player until exit ends', async () => {
  const { app, calls, exits, player, card } = setup();
  card('video');
  const data = { folder_name: 'video', video_count: 1, image_count: 0 };
  app.rememberFolderMotionOrigin('video', 'video');
  app.openVideoNoteLoading(data);
  app.openVideoNote({ folder: data, videos: [{ url: 'video.mp4' }] });
  assert.equal(calls.filter(call => call.key === 'video').length, 1);
  app.closeVideoNote();
  assert.equal(player.paused, true);
  assert.equal(player.released, false);
  assert.equal(app.videoNoteClosing, true);
  assert.equal(app.videoNote.video.url, 'video.mp4');
  exits[0].resolve(true); await tick();
  assert.equal(player.released, true);
  assert.equal(app.videoNoteClosing, false);
  assert.equal(app.videoNote.video, null);
});

test('closing a native fullscreen video does not race a spatial exit against its resize', () => {
  const { app, exits, player } = setup();
  app.videoNote = { open: true, folder: folder('video'), video: { url: 'video.mp4' }, error: '' };
  app.videoNoteFullscreen = true;
  let nativeExits = 0;
  app.exitCurrentFullscreen = () => { nativeExits += 1; };
  app.closeVideoNote();
  assert.equal(nativeExits, 1);
  assert.equal(exits.length, 0);
  assert.equal(app.videoNoteClosing, false);
  assert.equal(app.videoNote.video, null);
  assert.equal(player.released, true);
});

test('viewer opens from its detail thumbnail and returns to the current image only when visible', async () => {
  const { app, calls, exits, card, detailCards } = setup();
  const first = card('post', 1), second = card('post', 2);
  app.detail = { open: true, folder: folder('post'), pairs: [pair(1), pair(2)], videos: [] };
  app.openPair(1);
  assert.equal(calls[0].key, 'viewer');
  assert.equal(calls[0].origin.element, first);
  await app.openViewerEntry(app.viewerSequence[1]);
  assert.equal(calls.length, 1, 'same-level image changes do not replay layer entrance');
  app.closeViewer();
  assert.equal(exits[0].origin.resolve(), second);
  detailCards.delete('post::2');
  assert.equal(exits[0].origin.resolve(), null, 'virtualized or deleted sources use the engine fade fallback');
  assert.equal(app.viewer.pair.pair_index, 2);
  exits[0].resolve(true); await tick();
  assert.equal(app.viewer.pair, null);
  assert.equal(app.viewerClosing, false);
});

test('closing the parent removes a nested viewer without targeting a disappearing thumbnail', async () => {
  const { app, exits, card } = setup();
  card('post', 1);
  app.detail = { open: true, folder: folder('post'), pairs: [pair(1)], videos: [] };
  app.openPair(1);
  app.closeDetail();
  assert.equal(app.viewer.open, false);
  assert.equal(app.viewerClosing, false);
  assert.equal(app.viewer.pair, null);
  assert.deepEqual(exits.map(exit => exit.key), ['detail']);
  exits[0].resolve(true); await tick();
});

test('an unexpected synchronous animation failure cannot strand a closing layer', () => {
  const { app, motion } = setup();
  app.detail = { open: true, folder: folder('post'), pairs: [pair(1)], videos: [] };
  motion.exit = () => { throw new Error('animation unavailable'); };
  app.closeDetail();
  assert.equal(app.detail.open, false);
  assert.equal(app.detailClosing, false);
  assert.equal(app.detail.folder, null);
});

test('layer focus waits for the bundled Alpine visibility binding to flush', async () => {
  class Element {
    constructor() { this.attributes = new Map(); this.tagName = 'DIV'; }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    removeAttribute(name) { this.attributes.delete(name); }
    hasAttribute(name) { return this.attributes.has(name); }
  }
  let initializing = true;
  const context = vm.createContext({
    window: { Element }, Element,
    document: { createElement: () => new Element() },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    queueMicrotask(callback) { if (!initializing) queueMicrotask(callback); },
    setTimeout, clearTimeout, console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/vendor/alpinejs.min.js'), 'utf8'), context);
  initializing = false;
  const Alpine = context.window.Alpine;
  const { app } = setup();
  const scope = Alpine.reactive(app);
  scope.$nextTick = Alpine.nextTick;
  const backdrop = new Element();
  Alpine.addScopeToNode(backdrop, scope);
  const dispose = Alpine.bind(backdrop, { ':data-layer-motion-pending': "layerMotionPending.detail ? '' : null" });
  const hiddenAtFocus = [];
  scope.syncModalAccessibility = () => hiddenAtFocus.push(backdrop.hasAttribute('data-layer-motion-pending'));
  try {
    scope.detail.open = true;
    const generation = scope.prepareLayerEntrance('detail');
    await Alpine.nextTick();
    assert.equal(backdrop.hasAttribute('data-layer-motion-pending'), true);
    scope.enterGalleryLayer('detail', generation);
    await Alpine.nextTick();
    await tick();
    assert.deepEqual(hiddenAtFocus, [false], 'focus must run after the hidden attribute is removed');
  } finally {
    dispose();
  }
});
