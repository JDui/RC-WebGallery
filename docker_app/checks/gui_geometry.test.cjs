const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const tick = () => new Promise(resolve => setImmediate(resolve));

function target() {
  const listeners = new Map();
  return {
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    fire(name) { listeners.get(name)?.forEach(callback => callback({})); },
  };
}

function fixture({ width = 360, height = 640, panelWidth = 220, panelHeight = 164, viewport } = {}) {
  const frames = [], events = [];
  const document = { ...target(), documentElement: { clientWidth: width }, querySelector: () => null };
  const window = { innerWidth: width + 15, innerHeight: height, visualViewport: viewport,
    matchMedia: () => ({ matches: true }), clearTimeout() {},
    requestAnimationFrame(callback) { frames.push(callback); return frames.length; } };
  const context = vm.createContext({ window, document, localStorage: { getItem: () => null },
    Date, Intl, URLSearchParams, console });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  const app = context.galleryApp();
  const panel = { style: {}, getBoundingClientRect() {
    events.push('measure');
    return { width: Math.min(panelWidth, Number.parseFloat(this.style.maxWidth)),
      height: Math.min(panelHeight, Number.parseFloat(this.style.maxHeight)) };
  } };
  app.compactViewport = true;
  app.$refs = { mediaContextPanel: panel, mediaContextDelete: { focus() { events.push('focus'); } } };
  app.$nextTick = callback => Promise.resolve().then(() => callback?.());
  for (const name of ['cancelTouchBack', 'cancelVideoFeedGesture', 'observeGalleryCards',
    'observeGalleryLoadSentinel', 'scheduleFixedLayoutMetrics', 'refreshDetailTextOverflow',
    'scheduleRecentUpdatesMeasure', 'scheduleDetailWindowShift']) app[name] = () => {};
  const anchor = { isConnected: true, dataset: { mediaMenuFolder: 'sample' },
    getBoundingClientRect: () => ({ left: 80, top: 100 }) };
  return { app, panel, window, document, events, anchor, frames,
    async frame() { await tick(); frames.shift()?.(); await tick(); } };
}

test('menu waits for its display frame and fits the actual wrapped height inside the scrollbar-free viewport', async () => {
  const state = fixture();
  state.app.openMediaContextMenu(state.anchor, 359, 639);
  await tick();
  assert.equal(state.app.mediaContextMenu.positioned, false);
  assert.deepEqual(state.events, [], 'no hidden geometry read or premature focus');
  await state.frame();
  assert.equal(state.app.mediaContextMenu.x, 128);
  assert.equal(state.app.mediaContextMenu.y, 464);
  assert.equal(state.app.mediaContextMenu.positioned, true);
  assert.deepEqual(state.events, ['measure', 'focus']);
});

test('menu preserves a zero pointer coordinate and bounds oversized content in a short visual viewport', async () => {
  const viewport = { ...target(), offsetLeft: 30, offsetTop: 200, width: 180, height: 120 };
  const state = fixture({ viewport, panelHeight: 220 });
  state.app.openMediaContextMenu(state.anchor, 0, 0);
  await state.frame();
  assert.equal(state.app.mediaContextMenu.x, 42);
  assert.equal(state.app.mediaContextMenu.y, 212);
  assert.equal(state.panel.style.maxWidth, '156px');
  assert.equal(state.panel.style.maxHeight, '96px');
});

test('old menu display callbacks cannot reposition or focus a newer open', async () => {
  const state = fixture();
  state.app.openMediaContextMenu(state.anchor, 350, 600);
  state.app.openMediaContextMenu(state.anchor, 25, 30);
  await state.frame();
  assert.deepEqual(state.events, []);
  assert.equal(state.app.mediaContextMenu.positioned, false);
  await state.frame();
  assert.equal(state.app.mediaContextMenu.x, 25);
  assert.equal(state.app.mediaContextMenu.y, 30);
  assert.deepEqual(state.events, ['measure', 'focus']);
});

test('closing before the display frame leaves no late focus or visible menu', async () => {
  const state = fixture();
  state.app.openMediaContextMenu(state.anchor, 300, 600);
  state.app.closeMediaContextMenu();
  await state.frame();
  assert.equal(state.app.mediaContextMenu.open, false);
  assert.equal(state.app.mediaContextMenu.positioned, false);
  assert.deepEqual(state.events, []);
});

test('a compact-to-compact resize dismisses a positioned menu instead of keeping stale coordinates', async () => {
  const state = fixture();
  state.app.openMediaContextMenu(state.anchor, 300, 600);
  await state.frame();
  state.app.updateViewportMode();
  await tick();
  assert.equal(state.app.compactViewport, true);
  assert.equal(state.app.mediaContextMenu.open, false);
});

test('visual viewport movement dismisses an open menu without requiring a window resize', async () => {
  const viewport = { ...target(), width: 360, height: 400, offsetLeft: 0, offsetTop: 0 };
  const state = fixture({ viewport });
  state.app.installMediaContextMenu();
  for (const event of ['resize', 'scroll']) {
    state.app.openMediaContextMenu(state.anchor, 300, 390);
    await state.frame();
    viewport.fire(event);
    assert.equal(state.app.mediaContextMenu.open, false);
  }
});

test('a native fullscreen menu waits for exit and survives the resize caused by that exit', async () => {
  const state = fixture();
  state.document.fullscreenElement = {};
  let release;
  state.app.exitFullscreenForOverlay = () => new Promise(resolve => { release = resolve; });
  const opening = state.app.openMediaContextMenu(state.anchor, 300, 600);
  assert.equal(state.app.mediaContextMenu.open, false);
  state.app.updateViewportMode();
  state.document.fullscreenElement = null;
  release(); await opening; await state.frame();
  assert.equal(state.app.mediaContextMenu.open, true);
  assert.equal(state.app.mediaContextMenu.positioned, true);
});

for (const changed of ['close', 'recycled', 'navigation']) {
  test(`a pending fullscreen menu is discarded after ${changed}`, async () => {
    const state = fixture();
    state.document.fullscreenElement = {};
    let release;
    state.app.exitFullscreenForOverlay = () => new Promise(resolve => { release = resolve; });
    const opening = state.app.openMediaContextMenu(state.anchor, 300, 600);
    if (changed === 'close') state.app.closeMediaContextMenu();
    if (changed === 'recycled') state.anchor.dataset.mediaMenuFolder = 'different';
    if (changed === 'navigation') state.app.videoFeedToken += 1;
    state.document.fullscreenElement = null;
    release(); await opening; await state.frame();
    assert.equal(state.app.mediaContextMenu.open, false);
    assert.deepEqual(state.events, []);
  });
}

test('fullscreen exit failure leaves a visible player error and does not open an inaccessible menu', async () => {
  const state = fixture();
  state.document.fullscreenElement = {};
  state.app.videoNote.open = true;
  state.app.exitFullscreenForOverlay = () => Promise.reject(new Error('denied'));
  await state.app.openMediaContextMenu(state.anchor, 300, 600);
  assert.equal(state.app.mediaContextMenu.open, false);
  assert.match(state.app.videoNote.error, /退出全屏/);
});

for (const changed of ['removed', 'recycled folder', 'recycled pair']) {
  test(`a menu whose source is ${changed} before its display frame cannot appear or steal focus`, async () => {
    const state = fixture();
    state.app.openMediaContextMenu(state.anchor, 300, 600);
    if (changed === 'removed') state.anchor.isConnected = false;
    if (changed === 'recycled folder') state.anchor.dataset.mediaMenuFolder = 'other';
    if (changed === 'recycled pair') state.anchor.dataset.mediaMenuPair = '2';
    await state.frame();
    assert.equal(state.app.mediaContextMenu.open, false);
    assert.deepEqual(state.events, []);
  });
}

for (const latest of ['source', 'menu']) {
  test(`the latest ${latest} intent wins when different overlays share one fullscreen exit`, async () => {
    const state = fixture();
    state.document.fullscreenElement = {};
    let release;
    const exit = new Promise(resolve => { release = resolve; });
    state.app.exitFullscreenForOverlay = () => exit;
    state.app.beginSourcePreviewMotion = () => 1;
    state.app.syncBodyLock = () => {};
    state.app.enterGalleryLayer = () => {};
    const menu = () => state.app.openMediaContextMenu(state.anchor, 300, 600);
    const source = () => state.app.openSourcePreviewLayer({ url: 'https://example.com/post', title: 'Post' }, state.anchor);
    const pending = latest === 'source' ? [menu(), source()] : [source(), menu()];
    state.document.fullscreenElement = null;
    release(); await Promise.all(pending); await state.frame();
    assert.equal(state.app.sourcePreview.open, latest === 'source');
    assert.equal(state.app.mediaContextMenu.open, latest === 'menu');
  });
}
