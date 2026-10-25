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
