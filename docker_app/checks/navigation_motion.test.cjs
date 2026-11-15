const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function state(reduced = true) {
  const timers = new Map(); let id = 0;
  const oldPlayer = { readyState: 4, paused: false, pause() { this.paused = true; }, removeAttribute() {}, load() {} };
  let pendingPlayer = null;
  const context = vm.createContext({ localStorage: {getItem:()=>null}, Date, Intl, URLSearchParams, console,
    window: {setTimeout(fn){timers.set(++id,fn);return id;},clearTimeout(key){timers.delete(key);},matchMedia:()=>({matches:reduced})},
    document: {querySelector: selector=>selector.includes(':not(.is-retiring)') ? pendingPlayer : oldPlayer} });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8'),context);
  const app=context.galleryApp();
  const folders=[1,2,3].map(i=>({folder_name:`video-${i}`,video_count:1,image_count:0}));
  app.videoNote={open:true,folder:folders[0],video:{url:'one'},error:''};
  app.videoFeed={active:true,items:folders,index:0,page:1,totalPages:1,busy:false};
  app.$nextTick=fn=>fn ? Promise.resolve(fn()) : Promise.resolve();
  app.cachedDetail=()=>null; app.cacheDetailPayload=()=>{}; app.startVideoNote=()=>{};
  app.api=async()=>({folder:folders[1],videos:[{url:'two'}]});
  return {app,timers,oldPlayer,pending:video=>{pendingPlayer=video;}};
}
test('video keeps old frame and index until the mounted next player is ready, then retains the old player during fade',async()=>{
  const {app,oldPlayer,pending}=state(); pending({readyState:0});
  await app.stepVideoFeed(1);
  assert.equal(app.videoFeed.index,0); assert.equal(app.videoNote.video.url,'one');
  assert.equal(oldPlayer.paused,false); assert.equal(app.videoFeed.busy,true);
  assert.equal(app.videoNotePlayers().length,2);
  await app.videoNotePlayerReady({key:app.videoNotePlayerKey(app.videoFeedPending.payload.folder,{url:'two'})});
  assert.equal(app.videoFeed.index,1); assert.equal(app.videoNote.video.url,'two');
  assert.equal(oldPlayer.paused,true); assert.equal(app.videoFeed.busy,false);
  assert.equal(app.videoFeedRetiring.video.url,'one'); assert.equal(app.videoFeedSwitching,false);
});
test('pending player error or timeout preserves the old video and unlocks retry',async()=>{
  const {app,pending}=state(); pending({readyState:0});
  await app.stepVideoFeed(1);
  const token=app.videoFeedToken;
  app.failPendingVideo(token-1); assert.ok(app.videoFeedPending);
  app.failPendingVideo(token);
  assert.equal(app.videoNote.video.url,'one'); assert.equal(app.videoFeed.index,0);
  assert.equal(app.videoFeed.busy,false); assert.equal(app.videoFeedPending,null);
  assert.match(app.videoFeedMessage,/加载失败/);
  await app.stepVideoFeed(1); assert.ok(app.videoFeedPending);
});
test('late readiness after close cannot switch or play a video',async()=>{
  const {app,pending}=state(); pending({readyState:0});
  await app.stepVideoFeed(1); const next=app.videoNotePlayers()[1];
  app.syncBodyLock=()=>{}; app.closeVideoNote();
  app.startVideoNote=()=>{throw Error('must not play after close');};
  await app.videoNotePlayerReady(next);
  assert.equal(app.videoFeed.index,0);
});
test('an older min-height release cannot collapse a newer gallery request',()=>{
  const {app,timers}=state(); app.galleryRenderToken=1; app.galleryMinHeight=800;
  app.releaseGalleryMinHeight(); app.galleryRenderToken=2;
  [...timers.values()][0](); assert.equal(app.galleryMinHeight,800);
  timers.clear(); app.galleryLoading=false; app.releaseGalleryMinHeight();
  [...timers.values()][0](); assert.equal(app.galleryMinHeight,0);
});

test('height lock waits for the same request batch queue to finish',()=>{
  const {app,timers}=state(); app.galleryRenderToken=3; app.galleryMinHeight=800;
  app.galleryLoading=false; app.galleryBatchQueue=[{folder_name:'remaining'}];
  app.releaseGalleryMinHeight(); const first=[...timers.values()][0]; timers.clear(); first();
  assert.equal(app.galleryMinHeight,800); assert.equal(timers.size,1);
  app.galleryBatchQueue=[]; [...timers.values()][0](); assert.equal(app.galleryMinHeight,0);
});
test('an unplayable entry fades the changed count while preserving the current source',async()=>{
  const {app}=state(); app.api=async()=>({videos:[]});
  await app.stepVideoFeed(1);
  assert.equal(app.videoNote.video.url,'one'); assert.equal(app.videoFeed.items.length,2);
  assert.equal(app.videoFeedPositionText(),'1 / 2'); assert.equal(app.videoFeed.busy,false);
  assert.equal(app.videoFeedSwitching,false);
});

test('animated close keeps a paused frame until the backdrop finishes fading',()=>{
  const {app,timers,oldPlayer}=state(false); app.syncBodyLock=()=>{};
  app.closeVideoNote();
  assert.equal(app.videoNote.open,false); assert.equal(app.videoNoteClosing,true);
  assert.equal(app.videoNote.video.url,'one'); assert.equal(app.videoNotePlayers().length,1);
  assert.equal(oldPlayer.paused,true);
  timers.get(app.videoNoteCloseTimer)();
  assert.equal(app.videoNoteClosing,false); assert.equal(app.videoNote.video,null);
});

test('panel loading distinguishes first load, failure and empty success while keeping cached rows', async()=>{
  const {app}=state(); let resolve;
  app.api=()=>new Promise(done=>{resolve=done;});
  assert.equal(app.panelEmptyReady('review'),false);
  const pending=app.refreshReview();
  assert.equal(app.panelLoading.review,true);
  assert.equal(app.panelEmptyReady('review'),false);
  resolve({items:[{id:1}]}); await pending;
  app.api=async()=>{throw Error('offline');};
  await assert.rejects(app.refreshReview(),/offline/);
  assert.equal(app.reviewItems[0].id,1);
  assert.equal(app.panelLoading.review,false);
  assert.equal(app.panelEmptyReady('review'),false);
  app.api=async()=>({items:[]}); await app.refreshReview();
  assert.equal(app.panelErrors.review,'');
  assert.equal(app.panelEmptyReady('review'),true);
  app.api=()=>new Promise(done=>{resolve=done;});
  const refresh=app.refreshReview();
  assert.equal(app.panelEmptyReady('review'),true);
  resolve({items:[]}); await refresh;
});

test('out-of-order panel responses cannot restore stale rows or end the latest loading state',async()=>{
  const {app}=state(); const pending=[];
  app.api=()=>new Promise(resolve=>pending.push(resolve));
  const first=app.refreshReview(), second=app.refreshReview();
  pending[0]({items:[{id:1}]}); await first;
  assert.equal(app.panelLoading.review,true);
  pending[1]({items:[{id:2}]}); await second;
  assert.equal(app.reviewItems[0].id,2);
  const third=app.refreshReview(), fourth=app.refreshReview();
  pending[3]({items:[{id:4}]}); await fourth;
  pending[2]({items:[{id:3}]}); await third;
  assert.equal(app.reviewItems[0].id,4);
});

test('closing task details invalidates both successful and failed late responses',async()=>{
  for (const fail of [false,true]) {
    const {app}=state(); let finish;
    app.syncBodyLock=()=>{};
    app.api=()=>new Promise((resolve,reject)=>{finish=fail ? reject : resolve;});
    const pending=app.openTaskInspector({id:1,task_type:'pull'});
    app.closeTaskInspector();
    finish(fail ? Error('offline') : {item:{id:1}}); await pending;
    assert.equal(app.taskInspector.open,false);
    assert.equal(app.taskInspectorLoading,false);
  }
});

test('switching task inspector to another task or live status discards the older request',async()=>{
  const {app}=state(); const pending=[];
  app.syncBodyLock=()=>{}; app.formatTaskDetail=item=>String(item.id);
  app.api=()=>new Promise(resolve=>pending.push(resolve));
  const first=app.openTaskInspector({id:1,task_type:'pull'});
  const second=app.openTaskAddedItems({id:2});
  pending[0]({item:{id:1}}); await first;
  assert.match(app.taskInspector.title,/#2/);
  assert.equal(app.taskInspectorLoading,true);
  app.openCurrentTaskInspector(); const live=app.taskInspector;
  pending[1]({item:{id:2,details:{added_items:[]}}}); await second;
  assert.equal(app.taskInspector,live);
});

test('settings refresh preserves edits made while the request is pending',async()=>{
  const {app}=state(); let finish;
  app.settings={download_sleep:1}; app.keywordText='';
  app.settingsBaseline=app.settingsFingerprint(); app.lazyLoaded.settings=true;
  app.api=url=>url==='/api/settings' ? new Promise(resolve=>{finish=resolve;}) : Promise.resolve({});
  app.checkDataUpgrade=async()=>{}; app.applyThemeSpec=()=>{};
  const pending=app.loadSettings(); app.settings.download_sleep=7;
  finish({download_sleep:2}); await pending;
  assert.equal(app.settings.download_sleep,7);
  assert.equal(app.settingsDirty(),true);
});
