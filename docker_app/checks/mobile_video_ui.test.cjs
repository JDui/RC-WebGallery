const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../app/static');
function app(extra = {}) {
  const context = vm.createContext({localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, Date, Intl, URLSearchParams, console, ...extra});
  vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), context);
  return context.galleryApp();
}


test('video feed follows gallery order and fetches subsequent pages with the same filters', async () => {
  const state = app();
  state.narrowViewport = true;
  state.category = 'video'; state.sortOrder = 'asc'; state.sourceKind = 'xhs';
  state.timeFilterApplied = {startMonth:'2026-01', endMonth:'2026-10'};
  state.gallery = {items:[{folder_name:'a',video_count:1},{folder_name:'b',video_count:1}], page:1, page_size:2, total_pages:3};
  state.prepareVideoFeed(state.gallery.items[1]);
  state.videoNote.open = true;
  state.$nextTick = async () => {};
  state.cachedDetail = () => null; state.cacheDetailPayload = () => {};
  state.openVideoNote = payload => { state.videoNote = {...payload,open:true}; };
  const calls = [];
  state.api = async url => {
    calls.push(url);
    if (url.includes('/items?')) {
      const params = new URLSearchParams(url.split('?')[1]);
      assert.equal(params.get('sort_order'), 'asc'); assert.equal(params.get('source_kind'), 'xhs');
      assert.equal(params.get('start_month'), '2026-01'); assert.equal(params.get('end_month'), '2026-10');
      return {items:params.get('page') === '2' ? [{folder_name:'photo'}] : [{folder_name:'c',video_count:1}], total_pages:3};
    }
    return {folder:{folder_name:'c'},videos:[{url:'/c.mp4'}]};
  };
  await state.stepVideoFeed(1);
  assert.equal(state.videoFeed.index, 2); assert.equal(state.videoNote.folder.folder_name, 'c');
  assert.equal(calls.length, 3);
  await state.stepVideoFeed(1);
  assert.equal(state.videoFeedMessage, '已经是最后一条视频');
  assert.equal(state.videoFeed.busy, false);
});

test('closing the video player invalidates an in-flight swipe without reopening playback', async () => {
  const state = app(); state.narrowViewport = true; state.videoNote.open = true;
  state.videoFeed = {active:true,items:[{folder_name:'a'},{folder_name:'b'}],index:0,page:1,totalPages:1,busy:false};
  state.cachedDetail = () => null; state.syncBodyLock = () => {}; state.$refs = {};
  let finish; state.api = () => new Promise(resolve => { finish = resolve; });
  state.openVideoNote = () => assert.fail('closed player must not reopen');
  const pending = state.stepVideoFeed(1);
  state.closeVideoNote();
  finish({folder:{folder_name:'b'},videos:[{url:'/b.mp4'}]});
  await pending;
  assert.equal(state.videoNote.open, false); assert.equal(state.videoFeed.busy, false);
});

test('vertical video swipes ignore horizontal gestures and native seek controls', () => {
  const state = app(); state.narrowViewport = true; state.videoFeed.active = true;
  state.$refs = {xhsNoteVideo:{getBoundingClientRect:()=>({bottom:800})}};
  const target = {closest:()=>null}, directions = [];
  state.stepVideoFeed = direction => directions.push(direction);
  state.startVideoFeedGesture({touches:[{clientX:100,clientY:400}],target});
  state.endVideoFeedGesture({changedTouches:[{clientX:105,clientY:250}]});
  state.startVideoFeedGesture({touches:[{clientX:100,clientY:400}],target});
  state.endVideoFeedGesture({changedTouches:[{clientX:280,clientY:360}]});
  state.startVideoFeedGesture({touches:[{clientX:100,clientY:770}],target});
  state.endVideoFeedGesture({changedTouches:[{clientX:100,clientY:500}]});
  assert.deepEqual(directions, [1]);
});

test('UP pull is scoped to its own subscription page and cannot inherit a previous gallery source', async () => {
  const state = app(); const calls = [];
  state.api = async url => { calls.push(url); return {message:'ok'}; };
  for (const method of ['setImmediateTaskFeedback','clearLocalGalleryCaches','scheduleStatusRefresh','refreshStatus','refreshTasks','applyMutationSidebarCounts','notify']) state[method] = () => {};
  await state.runPull(); assert.equal(calls.length,0);
  state.currentView = 'subscriptions'; state.subscriptionPanel = 'up';
  state.sourceKind = 'xhs'; state.selectedSubscriptionUids = ['site:9'];
  await state.runPull(); assert.deepEqual(calls,['/api/pull/run']);
  const html = fs.readFileSync(path.join(root, '../templates/index.html'),'utf8');
  const topbar = html.slice(html.indexOf('<header class="topbar"'),html.indexOf('</header>',html.indexOf('<header class="topbar"')));
  assert.doesNotMatch(topbar, /runPull|syncAll/);
  for (const type of ['up','site','xhs']) assert.match(html,new RegExp(`data-testid="pull-${type}-subscriptions"`));
});

test('closing a still-loading video detail prevents the response from reopening it', async () => {
  const state = app();
  state.$refs = {}; state.$nextTick = async () => {};
  for (const method of ['syncBodyLock','setInteracting','cancelDeleteViewerPair','pauseCurrentViewerMedia']) state[method] = () => {};
  state.cachedDetail = () => null;
  state.openVideoNoteLoading = folder => {state.videoNote = {open:true,loading:true,folder};};
  state.openVideoNote = () => assert.fail('a closed detail request must stay closed');
  let complete; state.api = () => new Promise(resolve => {complete=resolve;});
  const pending = state.openFolder('a', {folder_name:'a',video_count:1});
  assert.equal(state.videoNote.loading,true);
  state.closeVideoNote();
  complete({folder:{folder_name:'a',video_count:1},videos:[{url:'/a.mp4'}]});
  await pending;
  assert.equal(state.videoNote.open,false);
});

test('video favorites update the gallery, feed, detail and cache together and prevent duplicate requests', async () => {
  const state = app();
  state.gallery.items = [{folder_name:'a',is_favorite:false}];
  state.videoFeed.items = [{folder_name:'a',is_favorite:false}];
  state.detail.folder = {folder_name:'a',is_favorite:false};
  state.videoNote.folder = {folder_name:'a',is_favorite:false};
  const cached = []; state.updateCachedDetailFavorite = (name,value) => cached.push([name,value]);
  state.refreshMeta = async () => {}; state.applyMutationSidebarCounts = async () => {}; state.notify = () => {};
  let complete, requests=0;
  state.api = () => {requests++;return new Promise(resolve => {complete=resolve;});};
  const pending = state.toggleFavorite(state.videoNote.folder);
  for (const folder of [state.gallery.items[0],state.videoFeed.items[0],state.detail.folder,state.videoNote.folder]) assert.equal(folder.is_favorite,true);
  await state.toggleFavorite(state.gallery.items[0]); assert.equal(requests,1);
  complete({favorite:true}); await pending;
  assert.equal(state.favoritePending.a,undefined);
  assert.deepEqual(cached.at(-1),['a',true]);
});

test('a failed video favorite restores every visible copy and clears the pending state', async () => {
  const state = app();
  state.videoNote.folder = {folder_name:'a',is_favorite:false};
  state.gallery.items = [{folder_name:'a',is_favorite:false}];
  state.updateCachedDetailFavorite = () => {}; state.refreshGallery = async () => {}; state.notify = () => {};
  state.api = async () => {throw new Error('offline');};
  await state.toggleFavorite(state.videoNote.folder);
  assert.equal(state.videoNote.folder.is_favorite,false);
  assert.equal(state.gallery.items[0].is_favorite,false);
  assert.equal(state.favoritePending.a,undefined);
});

test('desktop video navigation uses the same ordered feed', async () => {
  const state = app(); state.narrowViewport = false;
  state.gallery.items = [{folder_name:'a',video_count:1},{folder_name:'b',video_count:1}];
  state.prepareVideoFeed(state.gallery.items[0]);
  assert.equal(state.videoFeed.active,true);
  state.videoNote.open = true;
  state.cachedDetail = () => ({folder:{folder_name:'b',video_count:1},videos:[{url:'/b.mp4'}]});
  state.cacheDetailPayload = () => {}; state.$nextTick = async () => {};
  state.openVideoNote = payload => {state.videoNote={...payload,open:true};};
  await state.stepVideoFeed(1);
  assert.equal(state.videoNote.folder.folder_name,'b');
});

test('the swipe hint hides after two seconds and its timer is cleared on close', () => {
  let finish,cleared=0;
  const state = app({window:{setTimeout(fn,delay){assert.equal(delay,2000);finish=fn;return 7;},clearTimeout(){cleared++;}}});
  state.showVideoFeedHint(); assert.equal(state.videoFeedHintVisible,true);
  finish(); assert.equal(state.videoFeedHintVisible,false);
  state.showVideoFeedHint(); state.syncBodyLock=()=>{}; state.$refs={}; state.pendingTrashFolder='a';
  state.closeVideoNote();
  assert.equal(cleared,1); assert.equal(state.videoFeedHintVisible,false);
  assert.equal(state.pendingTrashFolder,null);
});
