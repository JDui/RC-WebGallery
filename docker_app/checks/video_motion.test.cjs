const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup({ reduced = false, low = false } = {}) {
  const timers = new Map(); let timerId = 0;
  const oldPlayer = { readyState: 4, paused: false, pause() { this.paused = true; },
    removeAttribute() {}, load() {}, getBoundingClientRect: () => ({ bottom: 800 }) };
  let mountedPending = null;
  const context = vm.createContext({
    Date, Intl, URLSearchParams, console,
    localStorage: { getItem: () => null },
    window: { innerHeight: 800, matchMedia: () => ({ matches: reduced }),
      setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
      clearTimeout(id) { timers.delete(id); },
      getComputedStyle: () => ({ transform: 'matrix(1, 0, 0, 1, 0, -24)' }) },
    document: { documentElement: { dataset: { performanceMode: low ? 'low' : 'full' } },
      querySelector: selector => selector.includes(':not(.is-retiring)') ? mountedPending : oldPlayer },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  const app = context.galleryApp();
  const folders = ['before', 'current', 'after'].map(folder_name => ({ folder_name, video_count: 1, image_count: 0 }));
  app.videoNote = { open: true, loading: false, folder: folders[1], video: { url: 'current.mp4' }, error: '' };
  app.videoFeed = { active: true, items: folders, index: 1, page: 1, totalPages: 1, busy: false };
  app.$nextTick = fn => Promise.resolve(fn?.());
  app.cachedDetail = () => null;
  app.cacheDetailPayload = () => {};
  const played = [];
  app.startVideoNote = () => played.push(app.videoNote.video.url);
  app.api = async url => {
    const folder = folders.find(item => url.endsWith(`/${item.folder_name}`));
    return { folder, videos: [{ url: `${folder.folder_name}.mp4` }] };
  };
  function runDelay(delay) {
    const timer = [...timers].find(([, value]) => value.delay === delay);
    assert.ok(timer, `expected a ${delay}ms timer`);
    timers.delete(timer[0]); timer[1].fn();
  }
  async function commit(direction) {
    await app.stepVideoFeed(direction);
    const next = app.videoNotePlayers().find(player => player.key !== app.videoNotePlayerKey());
    const promise = app.videoNotePlayerReady(next, { currentTarget: { readyState: 4 } });
    if (!reduced) runDelay(70);
    await promise;
    return next;
  }
  return { app, oldPlayer, timers, played, runDelay, commit, mount: player => { mountedPending = player; } };
}

for (const direction of [1, -1]) {
  test(`video direction ${direction} retains outgoing media and blocks overlap until incoming animation ends`, async () => {
    const { app, oldPlayer, played, commit } = setup();
    const next = await commit(direction);
    assert.equal(app.videoFeedDirection, direction);
    assert.equal(app.videoFeed.index, 1 + direction);
    assert.equal(app.videoFeedMotionKind, 'slide');
    assert.equal(app.videoFeedMotionFromY, -24, 'continue from the rendered drag/spring position');
    assert.equal(app.videoFeedRetiring.video.url, 'current.mp4');
    assert.equal(oldPlayer.paused, true);
    assert.equal(app.videoFeedAnimating, true);
    assert.equal(app.videoFeed.busy, true);
    assert.deepEqual(played, [direction > 0 ? 'after.mp4' : 'before.mp4']);
    const token = app.videoFeedToken;
    await app.stepVideoFeed(-direction);
    assert.equal(app.videoFeedToken, token, 'rapid repeats must not replace an in-flight transition');
    app.finishVideoFeedMotion(token, 'wrong-player', { animationName: 'video-feed-current-in' });
    assert.equal(app.videoFeed.busy, true);
    app.finishVideoFeedMotion(token, next.key, { animationName: 'unrelated-animation' });
    assert.equal(app.videoFeed.busy, true);
    app.finishVideoFeedMotion(token, next.key, { animationName: 'video-feed-current-in' });
    assert.equal(app.videoFeed.busy, false);
    assert.equal(app.videoFeedAnimating, false);
    assert.equal(app.videoFeedRetiring, null);
  });
}

test('a ready current video cannot be mistaken for an unmounted pending player', async () => {
  const { app, oldPlayer, played } = setup({ reduced: true });
  await app.stepVideoFeed(1);
  assert.ok(app.videoFeedPending);
  assert.equal(app.videoNote.video.url, 'current.mp4');
  assert.equal(oldPlayer.paused, false);
  assert.equal(app.videoFeed.index, 1);
  assert.deepEqual(played, []);
  const next = app.videoNotePlayers()[1];
  await app.videoNotePlayerReady(next, { currentTarget: { readyState: 1 } });
  assert.equal(app.videoNote.video.url, 'current.mp4');
  await app.videoNotePlayerReady(next, { currentTarget: { readyState: 4 } });
  assert.equal(app.videoNote.video.url, 'after.mp4');
});

test('low performance crossfades while reduced motion commits without a slide or delay', async () => {
  const low = setup({ low: true });
  await low.commit(1);
  assert.equal(low.app.videoFeedMotionKind, 'fade');
  assert.equal(low.app.videoFeedMotionFromY, 0);
  assert.equal(low.app.videoFeed.busy, true);
  low.runDelay(240);
  assert.equal(low.app.videoFeed.busy, false);
  assert.equal(low.app.videoFeedRetiring, null);
  const reduced = setup({ reduced: true });
  await reduced.commit(1);
  assert.equal(reduced.app.videoFeedMotionKind, 'none');
  assert.equal(reduced.app.videoFeedAnimating, false);
  assert.equal(reduced.app.videoFeed.busy, false);
  assert.equal([...reduced.timers.values()].some(timer => timer.delay === 70), false);
  reduced.runDelay(0);
  assert.equal(reduced.app.videoFeedRetiring, null);
});

test('cancelled readiness and obsolete completion cannot alter a later video session', async () => {
  const first = setup();
  await first.app.stepVideoFeed(1);
  const next = first.app.videoNotePlayers()[1];
  const readiness = first.app.videoNotePlayerReady(next);
  first.app.cancelVideoFeedMotion();
  first.app.videoFeedToken += 1;
  first.app.videoNote.open = false;
  first.runDelay(70);
  await readiness;
  assert.equal(first.app.videoNote.video.url, 'current.mp4');
  assert.deepEqual(first.played, []);

  const second = setup();
  await second.commit(1);
  const completion = second.timers.get(second.app.videoFeedRetireTimer).fn;
  second.app.cancelVideoFeedMotion();
  second.app.videoFeedToken += 1;
  second.app.videoFeedMotionToken = second.app.videoFeedToken;
  second.app.videoFeed.busy = true;
  second.app.videoFeedAnimating = true;
  completion();
  assert.equal(second.app.videoFeed.busy, true);
  assert.equal(second.app.videoFeedAnimating, true);
});

test('a reduced-motion reversal retires the old handoff before preparing the same video again', async () => {
  const { app, commit } = setup({ reduced: true });
  await commit(1);
  assert.equal(app.videoFeedRetiring.video.url, 'current.mp4');
  await app.stepVideoFeed(-1);
  assert.equal(app.videoFeedRetiring, null);
  assert.ok(app.videoFeedPending);
  assert.equal(app.videoNotePlayers().length, 2);
  const next = app.videoNotePlayers()[1];
  assert.equal(next.video.url, 'current.mp4');
  await app.videoNotePlayerReady(next);
  assert.equal(app.videoFeed.index, 1);
  assert.equal(app.videoNote.video.url, 'current.mp4');
});

test('vertical dragging follows the touch, reverses, and settles without stealing horizontal back gestures', () => {
  const { app } = setup();
  app.compactViewport = true;
  const event = (x, y) => ({ touches: [{ identifier: 7, clientX: x, clientY: y }],
    target: { closest: () => null }, cancelable: true, preventDefault() { this.prevented = true; } });
  app.startVideoFeedGesture(event(200, 300));
  const up = event(202, 230); app.moveVideoFeedGesture(up);
  assert.ok(app.videoFeedDragY < 0); assert.equal(up.prevented, true);
  app.moveVideoFeedGesture(event(201, 325));
  assert.ok(app.videoFeedDragY > 0, 'reversing the finger reverses the preview');
  let stepped = null; app.stepVideoFeed = direction => { stepped = direction; };
  app.endVideoFeedGesture({ changedTouches: [{ identifier: 7, clientX: 201, clientY: 325 }], touches: [], cancelable: true, preventDefault() {} });
  assert.equal(stepped, null); assert.equal(app.videoFeedDragY, 0);
  app.startVideoFeedGesture(event(200, 300));
  const horizontal = event(270, 306); app.moveVideoFeedGesture(horizontal);
  assert.equal(horizontal.prevented, undefined); assert.equal(app.videoFeedGesture, null);
  app.startVideoFeedGesture(event(200, 300));
  app.moveVideoFeedGesture(event(200, 220));
  app.endVideoFeedGesture({ changedTouches: [{ identifier: 7, clientX: 200, clientY: 220 }], touches: [], cancelable: true, preventDefault() {} });
  assert.equal(stepped, 1); assert.equal(app.videoFeedDragY, 0);
});
