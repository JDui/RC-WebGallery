const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../app/static');
function app(extra = {}) {
  const context = vm.createContext({localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, Date, Intl, URLSearchParams, console, XMLHttpRequest: class { open() {} send() {} abort() {} }, ...extra});
  vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), context);
  return context.galleryApp();
}

function wideGestureState(windowOverrides = {}) {
  const state=app({window:{innerWidth:1600,innerHeight:900,setTimeout:()=>1,clearTimeout(){},getSelection:()=>null,matchMedia:()=>({matches:false}),...windowOverrides},document:{body:{},getSelection:()=>null}});
  state.compactViewport=false;state.narrowViewport=false;
  state.currentFullscreenElement=()=>null;
  state.randomPullAllowed=()=>false;
  return state;
}


function responsiveViewerState(windowOverrides = {}) {
  let id=0;const timers=new Map();
  const state=app({window:{innerWidth:1400,innerHeight:900,setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key),requestAnimationFrame:fn=>fn(),...windowOverrides},document:{querySelector:()=>null,querySelectorAll:()=>[]}});
  for(const key of ['pauseCurrentViewerMedia','cancelDeleteViewerPair','setInteracting','syncBodyLock','scheduleIdleWork'])state[key]=()=>{};
  state.$nextTick=fn=>fn();
  state.viewerSequence=Array.from({length:4},(_,i)=>({folder:{folder_name:'demo'},pair:{item_key:`demo::${i+1}`,pair_index:i+1,image:{url:`original-${i+1}`,small_thumb_url:`thumb-${i+1}`}}}));
  const opened=state.openViewerEntry(state.viewerSequence[0]);
  return {state,timers,opened};
}

function pointerEvent(x,y,type='pointermove') {
  return {pointerId:1,pointerType:'touch',clientX:x,clientY:y,type,currentTarget:{setPointerCapture(){},releasePointerCapture(){}}};
}

test('wide trackpad back dismisses one layer per burst and preserves vertical scrolling', () => {
  let finish;
  const state=wideGestureState({setTimeout:fn=>{finish=fn;return 1;}});state.detail.open=true;
  let closed=0,prevented=0;
  state.closeDetail=()=>{closed++;state.detail.open=false;};
  const target={closest:()=>null};
  const wheel=(deltaX,deltaY=0)=>({target,deltaX,deltaY,cancelable:true,preventDefault(){prevented++;}});
  state.compactTrackpadWheel(wheel(0,160));
  assert.equal(closed,0);assert.equal(prevented,0);
  state.trackpadGesture=null;
  state.compactTrackpadWheel(wheel(-60));assert.equal(closed,0);
  state.compactTrackpadWheel(wheel(-60));assert.equal(closed,0);
  finish();assert.equal(closed,1);
  state.taskInspector.open=true;
  state.closeTaskInspector=()=>{throw Error('inertia must not dismiss another layer');};
  state.compactTrackpadWheel(wheel(-160));assert.equal(closed,1);
});


test('wide touch swipe works without a coarse pointer and expands a collapsed sidebar', () => {
  const state=wideGestureState();state.detail.open=true;
  let closed=0;state.closeDetail=()=>{closed++;state.detail.open=false;};
  const target={closest:()=>null};
  const first={identifier:1,clientX:300,clientY:200};
  const last={identifier:1,clientX:440,clientY:205};
  state.startTouchBack({target,touches:[first]});assert.ok(state.touchBackGesture);
  state.moveTouchBack({touches:[last],cancelable:true,preventDefault(){}});
  state.endTouchBack({touches:[],changedTouches:[last],cancelable:true,preventDefault(){}});
  assert.equal(closed,1);
  state.sidebarCollapsed=true;
  assert.equal(state.touchBackLayer(),'expand-sidebar');
});


test('wide back gestures leave editable fields, horizontal scrollers and pinch zoom alone', () => {
  const state=wideGestureState();state.detail.open=true;
  state.closeDetail=()=>{throw Error('excluded gesture must not close detail');};
  state.compactTrackpadWheel({ctrlKey:true,deltaX:-160});
  state.compactTrackpadWheel({target:{closest:()=>({})},deltaX:-160});
  const scroller={closest:()=>null,scrollWidth:400,clientWidth:200};
  const scrollerState=app({window:{innerWidth:1600,getComputedStyle:()=>({overflowX:'auto'})},document:{body:{}}});
  scrollerState.topOverlayLayer=()=> 'detail';
  scrollerState.compactTrackpadWheel({target:scroller,deltaX:-160});
  assert.equal(scrollerState.trackpadGesture,null);
  state.viewer.open=true;
  assert.equal(state.touchBackLayer(),'');
});


test('viewer loading indicator fades after reveal, stops on failure, and ignores stale loads', () => {
  let id=0;const timers=new Map();
  const state=app({window:{setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)}});
  state.viewer={open:true,pair:{image:{url:'original',small_thumb_url:'thumb'}},showVideo:false};
  state.viewerToken=1;state.viewerImageLoading=true;
  assert.equal(state.viewerPreviewUrl(),'thumb');
  assert.equal(state.viewerShouldShowOriginalLoading(),true);
  state.viewerMediaLoaded('image',{target:{dataset:{viewerToken:'1'}}});
  timers.get(1)();
  assert.equal(state.viewerImageReady,true);assert.equal(state.viewerShouldShowOriginalLoading(),false);
  state.viewerToken=2;state.viewerImageLoading=true;state.viewerImageReady=false;
  state.viewerMediaFailed('image',{target:{dataset:{viewerToken:'1'}}});
  assert.equal(state.viewerImageLoading,true);
  state.viewerMediaFailed('image',{target:{dataset:{viewerToken:'2'}}});
  assert.equal(state.viewerImageLoading,false);assert.equal(state.viewerPreviewRetired,false);
  const html=fs.readFileSync(path.join(root,'../templates/index.html'),'utf8');
  assert.match(html,/viewer-stage-preview" fetchpriority="high"/);
  assert.match(html,/data-testid="viewer-original-loading"[^>]+x-transition.opacity.duration.280ms/);
  assert.match(html,/@click="confirmDeleteViewerPair\(\)">确认删除<\/button>/);
  assert.doesNotMatch(html,/确认永久删除，不可恢复/);
});


test('trackpad back exposes progress and clears the hint after a partial or completed swipe', () => {
  let id=0;const timers=new Map();
  const target={closest:()=>null};
  const wheel=deltaX=>({target,deltaX,deltaY:0,cancelable:true,preventDefault(){}});
  const timerWindow={setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key),innerWidth:1600};
  const context=app({window:timerWindow,document:{body:{}}});
  context.currentFullscreenElement=()=>null;context.randomPullAllowed=()=>false;
  context.compactViewport=false;context.narrowViewport=false;context.detail.open=true;
  context.closeDetail=()=>{context.detail.open=false;};
  context.compactTrackpadWheel(wheel(-30));
  assert.ok(context.backGestureProgress()>0&&context.backGestureProgress()<1);
  assert.equal(context.backGestureLayer(),'detail');
  const first=context.trackpadBackHintTimer;
  context.compactTrackpadWheel(wheel(-30));assert.equal(timers.has(first),false);
  timers.get(context.trackpadBackHintTimer)();assert.equal(context.backGestureProgress(),0);
  context.trackpadGesture=null;
  context.compactTrackpadWheel(wheel(-120));
  assert.equal(context.detail.open,true);assert.equal(context.backGestureProgress(),1);
  timers.get(context.trackpadBackHintTimer)();
  assert.equal(context.detail.open,false);assert.equal(context.backGestureProgress(),0);
  context.touchBackProgress=.6;assert.equal(context.backGestureProgress(),.6);
});


test('viewer trackpad back shows the same hint before dismissing the viewer', () => {
  let id=0;const timers=new Map();
  const state=app({window:{setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)}});
  state.viewer.open=true;state.viewerZoom=1;let closed=0;state.closeViewer=()=>{closed++;};
  const wheel=deltaX=>({deltaX,deltaY:0,preventDefault(){}});
  state.viewerWheel(wheel(-30));
  assert.equal(closed,0);assert.equal(state.backGestureLayer(),'viewer');assert.ok(state.backGestureProgress()>0);
  state.viewerWheel(wheel(-60));assert.equal(closed,0);assert.equal(state.backGestureProgress(),1);
  state.viewerWheel(wheel(-60));assert.equal(closed,0);
  timers.get(state.trackpadBackHintTimer)();assert.equal(closed,1);assert.equal(state.backGestureProgress(),0);
  state.viewerWheel(wheel(-60));assert.equal(closed,1);
});


test('viewer navigation shows the next thumb immediately and never waits for the original to unlock', () => {
  const {state}=responsiveViewerState();
  state.showNextPair();
  assert.equal(state.viewerIndex,1);assert.equal(state.viewerPreviewUrl(),'thumb-2');
  assert.equal(state.viewerSwapPending,false);assert.equal(state.viewerImageLoading,true);
  assert.equal(state.viewerImageReady,false);assert.equal(state.viewerSwitchDirection,'up');
  assert.ok(state.viewerImageRevealAfter<=Date.now());
  state.viewerNavigationLockedUntil=0;
  state.showNextPair();assert.equal(state.viewerIndex,2);assert.equal(state.viewerPreviewUrl(),'thumb-3');
});


test('vertical drag closely follows the finger and a short quick flick changes one image', () => {
  const {state}=responsiveViewerState();
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));
  state.viewerGesture.startedAt=Date.now()-70;
  state.viewerPointerMove(pointerEvent(203,360));
  assert.equal(state.viewerGesture.dismissAxis,'vertical');assert.equal(state.viewerOffsetY,-40);assert.equal(state.viewerOffsetX,0);
  state.viewerPointerUp(pointerEvent(203,360,'pointerup'));
  assert.equal(state.viewerIndex,1);assert.ok(state.viewerSyntheticTapUntil>Date.now());
});


test('small slow drags, cancellations and pinch releases do not accidentally change images', () => {
  const {state}=responsiveViewerState();
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));state.viewerGesture.startedAt=Date.now()-1000;
  state.viewerPointerMove(pointerEvent(200,365));state.viewerPointerUp(pointerEvent(200,365,'pointerup'));
  assert.equal(state.viewerIndex,0);assert.equal(state.viewerOffsetY,0);
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));state.viewerPointerMove(pointerEvent(200,300));
  state.viewerPointerUp(pointerEvent(200,300,'pointercancel'));assert.equal(state.viewerIndex,0);
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));state.viewerGesture.isPinch=true;
  state.setViewerZoomLevel=()=>{};
  state.viewerPointerUp(pointerEvent(200,300,'pointerup'));assert.equal(state.viewerIndex,0);
});


test('trackpad resists short scrolls and requires a deliberate swipe before changing the image', () => {
  const {state,timers}=responsiveViewerState();
  state.viewerNavigationLockedUntil=Date.now()+1000;
  const wheel=deltaY=>({deltaX:0,deltaY,preventDefault(){}});
  state.viewerWheel(wheel(50));
  assert.ok(state.viewerOffsetY<0 && state.viewerOffsetY>-50);
  assert.equal(state.viewerTransitionEnabled,false);
  timers.get(state.viewerEdgeWheelTimer)();assert.equal(state.viewerIndex,0);assert.equal(state.viewerOffsetY,0);
  state.trackpadGesture=null;
  state.viewerWheel(wheel(150));
  assert.ok(state.viewerOffsetY<0 && state.viewerOffsetY>-150);
  timers.get(state.viewerEdgeWheelTimer)();assert.equal(state.viewerIndex,1);
  state.viewerWheel(wheel(100));assert.equal(state.viewerIndex,1);
});

test('original image reports byte progress and cancels superseded requests and object URLs', () => {
  const requests=[],revoked=[];
  class Request {
    constructor() { requests.push(this); }
    open(method,url) { this.url=url; }
    send() {}
    abort() { this.aborted=true; }
  }
  const state=app({XMLHttpRequest:Request,URL:{createObjectURL:()=> 'blob:original',revokeObjectURL:url=>revoked.push(url)}});
  state.viewerToken=1;
  state.loadViewerOriginal('/image-1');
  requests[0].onprogress({lengthComputable:true,loaded:25,total:100});
  assert.equal(state.viewerImageProgress,25);
  assert.equal(state.viewerOriginalProgressLabel(),'25%');
  state.viewerToken=2;state.loadViewerOriginal('/image-2');
  assert.equal(requests[0].aborted,true);
  requests[0].onprogress({lengthComputable:true,loaded:90,total:100});
  assert.equal(state.viewerImageProgress,null);
  requests[1].onprogress({lengthComputable:false,loaded:50,total:0});
  assert.equal(state.viewerOriginalProgressLabel(),'…');
  requests[1].status=200;requests[1].response={};requests[1].onload();
  assert.equal(state.viewerImageProgress,100);assert.equal(state.viewerImageSource,'blob:original');
  state.loadViewerOriginal('/image-3');assert.deepEqual(revoked,['blob:original']);
  requests[2].onerror();assert.equal(state.viewerImageSource,'/image-3');assert.equal(state.viewerImageProgress,null);
  state.loadViewerOriginal('/image-4');state.cancelViewerOriginal();
  requests[3].status=200;requests[3].onload();assert.equal(state.viewerImageSource,'');
  assert.equal(requests[3].aborted,true);
});


test('trackpad back allows reversal and rejects delayed inertia across layers until 600ms of quiet', () => {
  let now=1000,id=0;const timers=new Map();
  const state=app({Date:{now:()=>now},window:{innerWidth:1600,setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)},document:{body:{}}});
  state.currentFullscreenElement=()=>null;state.randomPullAllowed=()=>false;state.detail.open=true;
  let closed=0;state.closeDetail=()=>{closed++;state.detail.open=false;state.taskInspector.open=true;};
  state.closeTaskInspector=()=>{closed++;state.taskInspector.open=false;};
  const wheel=x=>({target:{closest:()=>null},deltaX:x,deltaY:0,cancelable:true,preventDefault(){}});
  const finish=()=>{const fn=timers.get(state.trackpadBackHintTimer);now+=260;fn();};
  state.compactTrackpadWheel(wheel(-140));assert.equal(closed,0);
  state.compactTrackpadWheel(wheel(110));finish();assert.equal(closed,0);
  now+=300;state.compactTrackpadWheel(wheel(-140));finish();assert.equal(closed,1);
  now+=300;state.compactTrackpadWheel(wheel(-140));assert.equal(closed,1);
  now+=300;state.compactTrackpadWheel(wheel(-140));assert.equal(closed,1);
  now+=601;state.compactTrackpadWheel(wheel(-140));assert.equal(closed,1);
  finish();assert.equal(closed,2);
});


test('first and last image resist outward drags and spring back without changing the index', () => {
  const {state}=responsiveViewerState();
  let bounced=0;const spring=state.springViewerEdge.bind(state);
  state.springViewerEdge=()=>{bounced++;spring();};
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));
  state.viewerPointerMove(pointerEvent(200,800));
  assert.ok(state.viewerOffsetY>0 && state.viewerOffsetY<80);
  state.viewerPointerUp(pointerEvent(200,800,'pointerup'));
  assert.equal(state.viewerIndex,0);assert.equal(state.viewerOffsetY,0);assert.equal(bounced,1);
  state.viewerIndex=3;state.openViewerEntry(state.viewerSequence[3]);
  state.viewerPointerDown(pointerEvent(200,400,'pointerdown'));state.viewerPointerMove(pointerEvent(200,0));
  assert.ok(state.viewerOffsetY<0 && state.viewerOffsetY>-80);
  state.viewerPointerUp(pointerEvent(200,0,'pointerup'));
  assert.equal(state.viewerIndex,3);assert.equal(state.viewerOffsetY,0);assert.equal(bounced,2);
});


test('trackpad outward scroll at either boundary follows the gesture and settles back', () => {
  const {state,timers}=responsiveViewerState();
  const wheel=deltaY=>({deltaX:0,deltaY,preventDefault(){}});
  state.viewerWheel(wheel(-80));assert.ok(state.viewerOffsetY>0);assert.equal(state.viewerIndex,0);
  timers.get(state.viewerEdgeWheelTimer)();assert.equal(state.viewerOffsetY,0);
  state.trackpadGesture=null;state.viewerIndex=3;state.openViewerEntry(state.viewerSequence[3]);
  state.viewerWheel(wheel(80));assert.ok(state.viewerOffsetY<0);assert.equal(state.viewerIndex,3);
  timers.get(state.viewerEdgeWheelTimer)();assert.equal(state.viewerOffsetY,0);
});


test('creator source navigation selects its matching internal subscription and platform', () => {
  const state=app();const calls=[];state.selectSubscription=(...args)=>calls.push(args);
  state.settings={host_mid:'999'};
  for(const [folder,uid,kind] of [
    [{subscription_uid:'123',subscription_name:'UP'},'123','up'],
    [{folder_name:'xhs_note',subscription_uid:'site:7'},'site:7','xhs'],
    [{subscription_uid:'site:8'},'site:8','site'],
    [{source_kind:'xhs',subscription_uid:'site:9'},'site:9','xhs'],
    [{folder_name:'old-bili'},'999','up'],
  ]) {
    assert.equal(state.creatorSourceKind(folder),kind);state.openCreatorSubscription(folder);
    assert.deepEqual(calls.at(-1),[uid,kind]);
  }
  state.openCreatorSubscription({source_kind:'site'});assert.equal(calls.length,5);
});


test('wheel gesture identity remains stable through a reactive proxy', () => {
  const raw=app();const cache=new WeakMap();
  const state=new Proxy(raw,{get(target,key,receiver){const value=Reflect.get(target,key,receiver);
    if(key!=='trackpadGesture'||!value)return value;
    if(!cache.has(value))cache.set(value,new Proxy(value,{}));return cache.get(value);
  }});
  const gesture=state.wheelGestureStep({deltaX:0,deltaY:30},'trackpadGesture','viewer');
  assert.equal(gesture,state.trackpadGesture);assert.equal(gesture.y,30);
  gesture.fired=true;assert.equal(state.trackpadGesture.fired,true);
});



test('original decode finishes before reveal and the preview remains until the overlap ends', async () => {
  let id=0,decode;const timers=new Map();
  const state=app({window:{setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)}});
  state.viewer={open:true,pair:{image:{url:'original'}}};state.viewerToken=7;state.viewerImageLoading=true;
  const target={dataset:{viewerToken:'7'},decode:()=>new Promise(resolve=>decode=resolve)};
  state.viewerMediaLoaded('image',{target});
  assert.equal(state.viewerImageReady,false);assert.equal(timers.size,0);
  decode();await Promise.resolve();timers.get(state.viewerImageRevealTimer)();
  assert.equal(state.viewerImageReady,true);assert.equal(state.viewerPreviewRetired,false);
  state.viewerOriginalTransitionEnded({propertyName:'transform',target});assert.equal(state.viewerPreviewRetired,false);
  state.viewerOriginalTransitionEnded({propertyName:'opacity',target:{dataset:{viewerToken:'6'}}});assert.equal(state.viewerPreviewRetired,false);
  state.viewerOriginalTransitionEnded({propertyName:'opacity',target});assert.equal(state.viewerPreviewRetired,true);
});

test('back hint keeps its identity while fading after a completed menu gesture', () => {
  let finish;const state=wideGestureState({setTimeout:fn=>{finish=fn;return 1;}});
  state.compactViewport=true;state.touchBackLayer=()=> 'open-sidebar';
  state.navigateTouchBack=()=>{};
  state.compactTrackpadWheel({target:{closest:()=>null},deltaX:-150,deltaY:0,cancelable:true,preventDefault(){}});
  assert.equal(state.backGestureLayer(),'open-sidebar');assert.equal(state.backGestureProgress(),1);
  finish();assert.equal(state.backGestureProgress(),0);assert.equal(state.backGestureLayer(),'open-sidebar');
});


test('a decoded handoff stays visible until the mounted preview has decoded and reached a frame', async () => {
  const frames=[];const token=7;
  let decoded;const target={dataset:{viewerToken:String(token)},decode:()=>new Promise(resolve=>decoded=resolve)};
  // Use a separate state to control frame delivery without relying on browser timers.
  const managed=app({window:{requestAnimationFrame:fn=>frames.push(fn)}});
  managed.viewer={open:true};managed.viewerToken=token;managed.viewerHandoff={key:'demo::2',url:'thumb-2'};
  managed.viewerPreviewLoaded({target});assert.equal(managed.viewerHandoff.url,'thumb-2');
  decoded();await Promise.resolve();assert.equal(target.dataset.previewReady,'1');
  assert.equal(managed.viewerHandoff.url,'thumb-2');
  frames.shift()();assert.equal(managed.viewerHandoff,null);
});

test('cold viewer navigation retains the displayed entry until a cancellable thumbnail preparation completes', async () => {
  const requests=[];
  const resources={warm(){},prepare(url){let resolve;const promise=new Promise(done=>resolve=done);
    const task={url,resolve,promise,cancel(){this.cancelled=true;resolve(false);}};requests.push(task);
    return {promise,cancel:()=>task.cancel()};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});
  requests[0].resolve(true);await opened;const token=state.viewerToken;
  const changing=state.showNextPair();assert.equal(state.viewer.pair.pair_index,1);assert.equal(state.viewerIndex,0);
  assert.equal(state.viewerToken,token);assert.equal(state.viewerPendingEntry.pair.pair_index,2);
  requests[1].resolve(true);await changing;
  assert.equal(state.viewer.pair.pair_index,2);assert.equal(state.viewerIndex,1);
  assert.equal(state.viewerHandoff.url,'thumb-2');assert.equal(state.viewerImageReady,false);
});

test('rapid pending navigation cancels stale work, and close prevents a late frame reopening the viewer', async () => {
  const requests=[];const resources={warm(){},prepare(url){let resolve;const promise=new Promise(done=>resolve=done);
    const task={url,resolve,cancelled:false};requests.push(task);return{promise,cancel(){task.cancelled=true;resolve(false);}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});
  requests[0].resolve(true);await opened;
  state.showNextPair();const changing=state.showNextPair();assert.equal(requests[1].cancelled,true);
  assert.equal(state.viewerPendingEntry.pair.pair_index,3);assert.equal(state.viewer.pair.pair_index,1);
  requests[2].resolve(true);await changing;
  assert.equal(state.viewer.pair.pair_index,3);
  state.viewerNavigationLockedUntil=0;const closing=state.showNextPair();state.closeViewer(true);
  requests[3].resolve(true);await closing;
  assert.equal(state.viewer.open,false);assert.equal(state.viewerPendingEntry,null);
});

test('failed preview and original preparation leave the last displayed frame and navigation index intact', async () => {
  const requests=[];const resources={warm(){},prepare(url){let resolve;const promise=new Promise(done=>resolve=done);
    requests.push({url,resolve});return{promise,cancel(){resolve(false);}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});
  requests[0].resolve(true);await opened;state.notify=()=>{};const changing=state.showNextPair();
  requests[1].resolve(false);await new Promise(setImmediate);requests[2].resolve(false);await changing;
  assert.equal(state.viewer.pair.pair_index,1);assert.equal(state.viewerIndex,0);
  assert.equal(state.viewerPendingEntry,null);
});

test('virtual remount consumes entrance animation even if the first mount ended early', () => {
  const state=app();const item={__appearing:true};
  state.claimCardAppearance(item);assert.equal(item.__appearing,true);
  state.claimCardAppearance(item);assert.equal(item.__appearing,false);
});

test('Live Photo keeps the static frame until current video data is ready and ignores stale readiness', () => {
  const state=app();state.viewerToken=3;state.playbackMode='once';
  state.viewer={open:true,showVideo:false,pair:{image:{small_thumb_url:'still'},livephoto:{url:'video'}}};
  const video={dataset:{viewerToken:'3',forward:'video'},readyState:0};
  state.viewerVideoReady(video);assert.equal(state.viewer.showVideo,false);
  assert.equal(state.livephotoViewerPoster(),'still');
  video.readyState=2;state.viewerVideoReady(video);assert.equal(state.viewer.showVideo,true);
  state.viewerVideoFailed(video);assert.equal(state.viewer.showVideo,false);
  video.dataset.viewerToken='2';state.viewerVideoReady(video);assert.equal(state.viewer.showVideo,false);
});

test('Escape during the first pending viewer entry cancels loading without a late reopen', async () => {
  let resolve,cancelled=false;
  const resources={warm(){},prepare(){return {promise:new Promise(done=>resolve=done),cancel(){cancelled=true;resolve(false);}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});
  state.currentFullscreenElement=()=>null;state.handleEscape();
  resolve(true);await opened;assert.equal(cancelled,true);assert.equal(state.viewer.open,false);
  assert.equal(state.viewerPendingEntry,null);
});

test('pending navigation keeps the displayed position and shows indeterminate progress', async () => {
  const requests=[];const resources={warm(){},prepare(){let resolve;const promise=new Promise(done=>resolve=done);
    requests.push(resolve);return{promise,cancel(){resolve(false);}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});
  requests[0](true);await opened;state.viewerImageProgress=100;state.viewerImageLoading=false;
  const changing=state.showNextPair();assert.equal(state.viewerPositionText(),'1 / 4');
  assert.equal(state.viewerShouldShowOriginalLoading(),true);assert.equal(state.viewerLoadingProgress(),null);
  requests[1](true);await changing;assert.equal(state.viewerPositionText(),'2 / 4');
});

test('a video URL preview waits for a decoded first-frame still before changing the displayed entry', async () => {
  const prepared=[];const resources={warm(){},prepare(url){prepared.push(url);return{promise:Promise.resolve(true),cancel(){}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});await opened;
  state.viewerSequence[1].pair={pair_index:2,preview_url:'/video.mp4',livephoto:{url:'/video.mp4'}};
  let ready;state.prepareViewerVideoFrame=url=>{assert.equal(url,'/video.mp4');return{promise:new Promise(resolve=>ready=resolve),cancel(){ready('');}};};
  const changing=state.showNextPair();assert.equal(state.viewer.pair.pair_index,1);
  assert.equal(state.viewerPendingEntry.pair.pair_index,2);assert.deepEqual(prepared,['thumb-1']);
  ready('data:image/jpeg;base64,frame');await changing;
  assert.equal(state.viewer.pair.pair_index,2);assert.equal(state.viewerIndex,1);
  assert.equal(state.viewerPreviewUrl(),'data:image/jpeg;base64,frame');
  assert.equal(state.livephotoViewerPoster(),'data:image/jpeg;base64,frame');
  assert.deepEqual(prepared,['thumb-1','data:image/jpeg;base64,frame']);
});

test('failed cover falls back to video, and failed or cancelled video preparation preserves the old entry', async () => {
  const resources={warm(){},prepare(url){return{promise:Promise.resolve(url!=='bad-cover'),cancel(){}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});await opened;
  state.viewerSequence[1].pair={pair_index:2,livephoto:{url:'video',cover_url:'bad-cover'}};
  const frames=[];state.prepareViewerVideoFrame=()=>{let ready;const promise=new Promise(resolve=>ready=resolve);
    const frame={ready,cancelled:false};frames.push(frame);return{promise,cancel(){frame.cancelled=true;ready('');}};};
  let failed=0;state.notify=()=>failed++;
  const failure=state.showNextPair();await new Promise(setImmediate);frames[0].ready('');await failure;
  assert.equal(failed,1);assert.equal(state.viewer.pair.pair_index,1);assert.equal(state.viewerIndex,0);
  const cancelled=state.showNextPair();await new Promise(setImmediate);
  const next=state.showNextPair();assert.equal(frames[1].cancelled,true);
  await next;frames[1].ready('late-frame');await cancelled;
  assert.equal(state.viewer.pair.pair_index,3);assert.equal(state.viewerIndex,2);
});

test('temporary video preparation releases decoding resources on readiness, cancellation, error and timeout', async () => {
  const videos=[],timers=new Map();let id=0,draws=0;
  const state=app({window:{setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)},
    document:{createElement(kind){
      if(kind==='canvas')return{getContext:()=>({drawImage(video,x,y,width,height){draws++;assert.equal(width,1600);assert.equal(height,900);}}),toDataURL:()=> 'data:image/jpeg;base64,frame'};
      const video={readyState:0,videoWidth:3840,videoHeight:2160,pause(){this.paused=true;},removeAttribute(){this.removed=true;},load(){this.loads=(this.loads||0)+1;}};
      videos.push(video);return video;
    }}});
  const ready=state.prepareViewerVideoFrame('video');videos[0].onloadeddata();assert.equal(draws,0);
  videos[0].readyState=2;videos[0].onloadeddata();assert.equal(await ready.promise,'data:image/jpeg;base64,frame');
  const cancelled=state.prepareViewerVideoFrame('cancel');cancelled.cancel();assert.equal(await cancelled.promise,'');
  const error=state.prepareViewerVideoFrame('error');videos[2].onerror();assert.equal(await error.promise,'');
  const timeout=state.prepareViewerVideoFrame('timeout');timers.values().next().value();assert.equal(await timeout.promise,'');
  assert.equal(timers.size,0);assert.equal(draws,1);
  for(const video of videos){assert.equal(video.paused,true);assert.equal(video.removed,true);assert.equal(video.onloadeddata,null);assert.equal(video.onerror,null);assert.equal(video.loads,2);}
});

test('gesture exit waits for its decoded neighbor and closing during the slide cannot reopen the viewer', async () => {
  const requests=[];const resources={warm(){},prepare(url){let resolve;const promise=new Promise(done=>resolve=done);
    requests.push({url,resolve});return{promise,cancel(){resolve(false);}};}};
  const {state,opened}=responsiveViewerState({galleryImageResources:resources});requests[0].resolve(true);await opened;
  const slides=[];state.animatePreparedViewerSwipe=(direction,url)=>new Promise(resolve=>{
    slides.push({direction,url});state.viewerSwipeResolve=resolve;state.viewerSwipeFinish=()=>state.cancelViewerSwipe();
  });
  state.viewerOffsetY=-160;const changing=state.finishViewerVerticalSwipe(1);
  assert.equal(state.viewerOffsetY,0);assert.equal(state.viewer.pair.pair_index,1);assert.equal(slides.length,0);
  requests[1].resolve(true);await new Promise(setImmediate);
  assert.deepEqual(slides,[{direction:1,url:'thumb-2'}]);assert.equal(state.viewer.pair.pair_index,1);
  state.viewerSwipeFinish();await changing;assert.equal(state.viewer.pair.pair_index,2);
  const closing=state.finishViewerVerticalSwipe(1);requests[2].resolve(true);await new Promise(setImmediate);
  state.closeViewer(true);await closing;assert.equal(state.viewer.open,false);assert.equal(state.viewerPendingEntry,null);
});

test('pingpong holds the displayed video frame across a cold source change and releases it on pause', async () => {
  const order=[];const canvas={getContext:()=>({drawImage(){order.push('capture');}})};
  const state=app({document:{querySelector:()=>canvas}});state.viewerToken=1;state.playbackMode='pingpong';
  state.viewer={open:true,showVideo:true,pair:{image:{url:'still'},livephoto:{url:'forward'}}};
  const video={dataset:{viewerToken:'1',forward:'forward',reverse:'reverse',hasImage:'1'},src:'forward',readyState:4,
    videoWidth:3840,videoHeight:2160,play:()=>Promise.resolve(),load(){order.push(`load:${this.src}`);this.readyState=0;},
    pause(){},removeAttribute(){this.src='';},getAttribute:()=> 'still',setAttribute(){}};
  state.applyPlaybackMode(video);await Promise.resolve();video.onended();
  assert.deepEqual(order,['capture','load:reverse']);assert.equal(state.viewerVideoFrameReady,true);
  assert.equal(canvas.width,1600);assert.equal(canvas.height,900);assert.equal(state.viewer.showVideo,false);
  state.viewerVideoReady(video);assert.equal(state.viewer.showVideo,false);
  video.readyState=2;state.viewerVideoReady(video);assert.equal(state.viewer.showVideo,true);
  state.playbackMode='pause';state.applyPlaybackMode(video);assert.equal(state.viewerVideoFrameReady,false);
  assert.equal(canvas.width,0);assert.equal(canvas.height,0);
});

test('a rejected superseded play promise cannot hide a newer ready video', async () => {
  const state=app();state.viewerToken=1;state.playbackMode='once';
  state.viewer={open:true,showVideo:false,pair:{livephoto:{url:'video'}}};
  const plays=[];const video={dataset:{viewerToken:'1',forward:'video'},readyState:2,
    play(){return new Promise((resolve,reject)=>plays.push({resolve,reject}));}};
  state.playReadyViewerVideo(video);state.playReadyViewerVideo(video);
  plays[1].resolve();await new Promise(setImmediate);assert.equal(state.viewer.showVideo,true);
  plays[0].reject(Error('superseded'));await new Promise(setImmediate);assert.equal(state.viewer.showVideo,true);
});

test('preview retirement never removes the underlay while the original is still transparent', () => {
  let opacity='0';const state=app({window:{getComputedStyle:()=>({opacity,visibility:'visible'}),clearTimeout(){}}});
  state.viewer.open=true;state.viewerToken=1;state.viewerImageReady=true;
  state.viewerHandoff={url:'ready-thumb'};
  const target={complete:true,naturalWidth:600,isConnected:true,dataset:{viewerToken:'1'}};
  state.retireViewerPreview(target,1);assert.equal(state.viewerPreviewRetired,false);
  opacity='0.6';state.retireViewerPreview(target,1);assert.equal(state.viewerPreviewRetired,false);
  opacity='1';state.retireViewerPreview(target,1);assert.equal(state.viewerPreviewRetired,true);
  assert.equal(state.viewerHandoff,null);
});

test('trackpad commits on deceleration without waiting through the inertial tail', () => {
  const {state}=responsiveViewerState();
  const wheel=y=>({deltaX:0,deltaY:y,preventDefault(){}});
  state.viewerWheel(wheel(70));state.viewerWheel(wheel(20));
  assert.equal(state.viewerIndex,0);
  state.viewerWheel(wheel(10));assert.equal(state.viewerIndex,1);
  state.viewerWheel(wheel(5));assert.equal(state.viewerIndex,1);
});

test('position hint appears on first open and gesture navigation and cancels the older hide timer', () => {
  const {state,timers}=responsiveViewerState();
  assert.equal(state.viewerPositionHintVisible,true);
  const oldTimer=state.viewerPositionHintTimer;
  state.showNextPair(true);
  assert.equal(state.viewerIndex,1);
  assert.equal(state.viewerPositionHintVisible,true);
  assert.equal(timers.has(oldTimer),false);
  timers.get(state.viewerPositionHintTimer)();
  assert.equal(state.viewerPositionHintVisible,false);
});
