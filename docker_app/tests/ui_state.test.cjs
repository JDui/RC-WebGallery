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

test('search count belongs to search conditions and stays hidden on other pages', () => {
  const state = app(); state.category = 'search'; state.searchQuery = '海风';
  state.searchResultSignature = state.searchSignature(); state.searchResultCount = 3;
  assert.equal(state.sidebarCount('search'), 3);
  state.category = 'all'; state.gallery.total = 2838; assert.equal(state.sidebarCount('search'), '');
  state.category = 'search'; assert.equal(state.sidebarCount('search'), 3);
  state.galleryViewMode = 'pair'; assert.equal(state.sidebarCount('search'), '');
});

test('Live Photo label and explicit timezone offsets are correct', () => {
  const state = app(); state.meta.counts = {all: 2838, livephoto: 167};
  assert.equal(state.compactStats()[1].label, 'Live Photo'); assert.equal(state.compactStats()[1].value, 167);
  assert.equal(state.parseDateValue('2026-10-18T07:00:00+08:00').getTime(), Date.parse('2026-10-17T23:00:00Z'));
  assert.equal(state.futureDate('2100-01-01'), true);
});

test('settings dirty state excludes transient authentication metadata', () => {
  const state = app(); state.settings = {app_title:'demo', auth:{ok:true}};
  state.settingsBaseline = state.settingsFingerprint();
  state.settings.auth = {ok:false}; assert.equal(state.settingsDirty(), false);
  state.settings.intranet_config_error = "file error"; assert.equal(state.settingsDirty(), false);
  state.settings.app_title = 'changed'; assert.equal(state.settingsDirty(), true);
});

test('viewer position uses its actual media sequence', () => {
  const state = app(); state.viewerSequence = [{pair:{item_key:'a'}}, {pair:{item_key:'b'}}];
  state.viewer.pair = {item_key:'b'}; state.viewerIndex = 1;
  assert.equal(state.viewerPositionText(), '2 / 2');
});

async function preflight({reachable=false, service='biligalleryrc', hang=false}={}) {
  let finishTimer, destination, fetchOptions;
  const context = vm.createContext({URL, Date, AbortController,
    document: {body:{dataset:{intranetOrigin:'http://192.168.31.221:7777'}},getElementById:()=>({})},
    window:{location:{href:'https://external.example/?q=test#photo',replace:value=>{destination=value;}},setTimeout:fn=>{finishTimer=fn;return 1;},clearTimeout(){}},
    fetch:(url,options)=>{fetchOptions=options;return hang ? new Promise(()=>{}) : Promise.resolve({ok:reachable,json:async()=>({ok:true,service})});},
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'intranet.js'),'utf8'), context);
  await new Promise(resolve=>setImmediate(resolve));
  return {destination, timeout:()=>{finishTimer();return destination;},fetchOptions};
}

test('intranet success preserves path/query/hash and sends no cookies', async () => {
  const result = await preflight({reachable:true});
  assert.equal(result.destination,'http://192.168.31.221:7777/?q=test#photo'); assert.equal(result.fetchOptions.credentials,'omit');
});

test('intranet wrong identity and timeout retain the original address without looping', async () => {
  const wrong = await preflight({reachable:true,service:'other-app'});
  assert.match(wrong.destination,/^https:\/\/external.example\/\?q=test&_gallery_skip_intranet=1#photo$/);
  const timeout = await preflight({hang:true}); assert.equal(timeout.destination,undefined);
  assert.match(timeout.timeout(), /_gallery_skip_intranet=1#photo$/);
});

test('returning through navigation preserves the last search', () => {
  const state = app(); state.searchQuery = '海风'; state.searchDraft = '海风';
  for (const key of ['closeSidebarDrawer','closeGalleryLayers','scrollViewTop','refreshGalleryImmediately','scheduleIdleWork']) state[key] = () => {};
  state.openGallery('all'); state.openGallery('search');
  assert.equal(state.searchQuery,'海风'); assert.equal(state.searchDraft,'海风');
});

test('a completed old request cannot repopulate a cleared search', async () => {
  const state = app(); state.category = 'search'; state.searchQuery = 'old';
  for (const name of ['cancelGalleryBatchWork','lockGalleryMinHeight','writeLocalGalleryCache']) state[name] = () => {};
  state.$nextTick = () => {}; state.preferredGalleryPageSize = () => 24;
  state.galleryLocalCacheKey = () => 'old-search'; state.readLocalGalleryCache = () => null;
  state.applyGalleryPagePayload = payload => { state.gallery = payload; };
  let complete;
  state.api = () => new Promise(resolve => { complete = resolve; });
  const pending = state.refreshGallery(true);
  state.clearSearch();
  complete({items:[{folder_name:'old-result'}],total:99});
  await pending;
  assert.equal(state.gallery.items.length,0);
  assert.equal(state.searchResultCount,null);
});


test('offscreen or stale preload completions cannot restore evicted quality metadata', async () => {
  let resolve;
  const state = app({window:{galleryImageResources:{warm:()=>new Promise(done=>{resolve=done;})}}});
  state.galleryRequestId=1;
  state.galleryWindowItems=()=>[{id:'visible'}];
  state.galleryItemKey=item=>item.id;
  let promoted=false;state.promoteGalleryQuality=()=>{promoted=true;};
  const pending=state.loadGallerySmallTask({key:'visible',preloadKey:'visible::small',urls:['small'],requestId:1});
  state.galleryRequestId=2;resolve(true);await pending;
  assert.equal(promoted,false);assert.equal(Object.keys(state.gallerySmallPreloadCache).length,0);
});

test('viewer retains the preview for overlap and ignores retirement from a superseded image', () => {
  let id=0;const timers=new Map();
  const state=app({window:{setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:key=>timers.delete(key)}});
  state.viewer={open:true,pair:{image:{}},showVideo:false};state.viewerToken=1;
  state.viewerMediaLoaded('image',{target:{dataset:{viewerToken:'1'}}});
  timers.get(1)();
  assert.equal(state.viewerImageReady,true);assert.equal(state.viewerPreviewRetired,false);
  state.viewerToken=2;timers.get(2)();assert.equal(state.viewerPreviewRetired,false);
  state.viewerMediaLoaded('image',{target:{dataset:{viewerToken:'2'}}});
  timers.get(3)();timers.get(4)();assert.equal(state.viewerPreviewRetired,true);
});
