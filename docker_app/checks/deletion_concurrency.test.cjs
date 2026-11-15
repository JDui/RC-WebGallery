const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function setup() {
  let id = 0;
  const timers = new Map(), storage = new Map();
  const context = vm.createContext({
    Date, Intl, URLSearchParams, AbortController, console,
    localStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
      key: index => [...storage.keys()][index],
      get length() { return storage.size; },
    },
    window: { setTimeout: fn => { timers.set(++id, fn); return id; }, clearTimeout: key => timers.delete(key) },
    document: { querySelectorAll: () => [], querySelector: () => null },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  const state = context.galleryApp();
  state.$nextTick = () => {};
  state.galleryCardRectFor = () => null;
  state.notify = () => {};
  state.schedulePostMutationRefresh = () => {};
  state.scheduleIdleWork = fn => { timers.set(++id, fn); return id; };
  state.releaseGalleryMinHeight = state.lockGalleryMinHeight = () => {};
  state.galleryViewMode = state.displayedGalleryViewMode = 'pair';
  const folder = { folder_name: 'post', image_count: 3, asset_count: 3 };
  const pairs = [1, 2, 3].map(pair_index => ({ pair_index, image: { url: `image-${pair_index}` } }));
  state.detail = { open: true, folder, pairs, videos: [] };
  state.gallery = { items: pairs.map(pair => ({ ...pair, folder_name: 'post', item_key: `post::${pair.pair_index}` })), total: 3, page: 1, page_size: 24 };
  state.cacheDetailPayload('post', state.detail);
  return { state, timers, storage };
}

const liveKeys = state => Array.from(state.gallery.items.filter(item => !item.__deleted_placeholder), item => item.item_key);
const pairKeys = state => Array.from(state.detail.pairs, pair => pair.pair_index);
const tick = () => new Promise(resolve => setImmediate(resolve));

test('rapid pair deletes accept both clicks, serialize writes, and roll back only the failed image', async () => {
  const { state } = setup();
  const first = deferred(), second = deferred(), calls = [];
  state.api = url => { calls.push(url); return calls.length === 1 ? first.promise : second.promise; };
  state.askDeletePair('post', 1);
  const a = state.confirmDeletePair('post', 1);
  state.askDeletePair('post', 2);
  const b = state.confirmDeletePair('post', 2);
  assert.deepEqual(pairKeys(state), [3]);
  assert.deepEqual(liveKeys(state), ['post::3']);
  await tick();
  assert.equal(calls.length, 1);
  first.reject(new Error('first write failed'));
  await a;
  await tick();
  assert.equal(calls.length, 2);
  second.resolve({ ok: true });
  await b;
  assert.deepEqual(pairKeys(state), [1, 3]);
  assert.deepEqual(liveKeys(state), ['post::1', 'post::3']);
  assert.equal(state.detail.folder.image_count, 2);
  assert.equal(Object.keys(state.pairDeletingKeys).length, 0);
});

test('out-of-order post failure preserves later successful deletion and untouched image objects', async () => {
  const { state } = setup();
  state.detail = { open: false, folder: null, pairs: [] };
  const items = ['a', 'b', 'c'].map(folder_name => ({ folder_name, item_key: folder_name }));
  state.gallery.items = items;
  const first = deferred(), second = deferred();
  state.api = url => url.includes('/a/') ? first.promise : second.promise;
  state.askTrashFolder('a'); const a = state.trashFolderCard('a');
  state.askTrashFolder('b'); const b = state.trashFolderCard('b');
  await tick();
  second.resolve({ ok: true }); await b;
  first.reject(new Error('failed')); await a;
  assert.deepEqual(liveKeys(state), ['a', 'c']);
  assert.equal(state.gallery.total, 2);
  assert.equal(state.gallery.items.find(item => item.folder_name === 'c'), items[2]);
});

test('late failure never overwrites a newly opened detail or gallery', async () => {
  const { state } = setup();
  const response = deferred(); state.api = () => response.promise;
  state.askDeletePair('post', 1); const deletion = state.confirmDeletePair('post', 1);
  const other = { open: true, folder: { folder_name: 'other' }, pairs: [] };
  state.detail = other;
  state.galleryRequestId += 1;
  state.gallery.items = [{ folder_name: 'other', item_key: 'other' }];
  response.reject(new Error('failed')); await deletion;
  assert.equal(state.detail, other);
  assert.deepEqual(liveKeys(state), ['other']);
});

test('late gallery responses and idle cache writes cannot resurrect deleted content', async () => {
  const { state, timers, storage } = setup();
  const response = deferred(); state.api = () => response.promise;
  const payload = { items: [...state.gallery.items], total: 3, page_size: 24 };
  state.writeLocalGalleryCache('gallery-page:test', payload);
  const idleWrite = [...timers.values()][0];
  const refresh = state.refreshGallery(false);
  state.beginGalleryDeletion('post', 1);
  state.removePairFromGallery('post', 1);
  idleWrite();
  assert.equal(storage.has('gallery-page:test'), false);
  response.resolve(payload); await refresh;
  assert.deepEqual(liveKeys(state), ['post::2', 'post::3']);
  assert.equal(state.galleryFetching, false);
  state.applyGalleryPagePayload(payload, 1, 'replace');
  assert.deepEqual(liveKeys(state), ['post::2', 'post::3']);
});

test('queued rendering and detail cache reads filter deleted targets', () => {
  const { state } = setup();
  const original = { ...state.detail, pairs: [...state.detail.pairs] };
  const items = [...state.gallery.items];
  state.beginGalleryDeletion('post', 1);
  state.gallery.items = [];
  state.queueGalleryBatch(items, state.galleryRenderToken);
  assert.deepEqual(liveKeys(state), ['post::2', 'post::3']);
  state.cacheDetailPayload('post', original);
  assert.deepEqual(Array.from(state.cachedDetail('post').pairs, pair => pair.pair_index), [2, 3]);
});

test('task polling keeps the deletion session intact instead of reloading thumbnails', async () => {
  const { state } = setup();
  state.beginGalleryDeletion('post', 1);
  state.lastTaskId = 1;
  state.api = async () => ({ status: { running: false, last_run: { id: 2 } } });
  state.refreshGallery = () => { throw new Error('must not replace current gallery'); };
  for (const method of ['refreshMeta', 'loadSubscriptions']) state[method] = async () => {};
  state.isSitePanelActive = () => false;
  await state.refreshStatus();
  assert.equal(state.lastTaskId, 2);
});

test('placeholder collapse waits until the next delete confirmation has ended', () => {
  const { state, timers } = setup();
  state.beginGalleryDeletion('post', 1);
  state.removePairFromGallery('post', 1, { placeholder: true });
  const key = state.pairPlaceholderKey('post', 1);
  state.askDeletePair('post', 2);
  timers.get(state.deletedGalleryTimers[key])();
  assert.ok(state.gallery.items.some(item => item.__deleted_placeholder));
  state.cancelDeleteViewerPair();
  state.animateGalleryReflow = update => update();
  timers.get(state.deletedGalleryTimers[key])();
  assert.equal(state.gallery.items.some(item => item.__deleted_placeholder), false);
});

test('explicit navigation still finishes when an earlier deletion completes during its request', async () => {
  const { state } = setup();
  state.beginGalleryDeletion('post', 1);
  const response = deferred(); state.api = () => response.promise;
  const refresh = state.refreshGallery(true);
  state.clearLocalGalleryCaches(); // The delete request completes after navigation started.
  response.resolve({ items: [{ item_key: 'other', folder_name: 'other' }], total: 1 });
  await refresh;
  assert.deepEqual(liveKeys(state), ['other']);
  assert.equal(state.galleryFetching, false);
});
