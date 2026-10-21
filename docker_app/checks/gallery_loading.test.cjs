const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../app/static');
function app(extra = {}, initialSettings = {}) {
  const context = vm.createContext({localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, Date, Intl, URLSearchParams, console, ...extra});
  vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), context);
  return context.galleryApp(initialSettings);
}

test('cache revalidation merges changed metadata without clearing keyed cards', () => {
  const state=app(); state.$nextTick=()=>{};
  const first={items:[{folder_name:'a',title:'cached',random_cover:{id:5}}],page_size:36,total_pages:1};
  state.applyGalleryPagePayload(first,1,'replace');
  const assignments=[]; let current=state.gallery;
  Object.defineProperty(state,'gallery',{get:()=>current,set:value=>{assignments.push(value.items.length);current=value;}});
  state.applyGalleryPagePayload({...first,items:[{folder_name:'a',title:'fresh',random_cover:{id:6}}]},1,'replace');
  assert.deepEqual(assignments,[1]); assert.equal(state.gallery.items[0].title,'fresh');
  assert.equal(state.gallery.items[0].random_cover.id,6); assert.equal(state.gallery.items[0].__appearing,false);
});

test('wide single covers and random selection preserve portrait and video behavior', () => {
  const state=app(); const item={preview_tiles:[{id:1,width:800,height:1200},{id:2}],random_cover:{id:9,width:900,height:1200}};
  state.settings={wide_single_cover:false,random_cover_enabled:true};
  assert.equal(state.galleryPreviewTiles(item).length,2);
  state.settings.wide_single_cover=true; assert.equal(state.galleryPreviewTiles(item)[0].id,9);
  assert.equal(state.galleryCoverStyle(item),'aspect-ratio: 0.75;');
  state.settings.random_cover_enabled=false; assert.equal(state.galleryPreviewTiles(item)[0].id,1);
  state.narrowViewport=true; state.settings.wide_single_cover=false; assert.equal(state.galleryPreviewTiles(item).length,1);
  state.settings.random_cover_enabled=true; assert.equal(state.galleryPreviewTiles({...item,video_count:1})[0].id,1);
});

test('compact detail header has stable scroll thresholds', () => {
  const state=app();
  state.updateDetailHeader({target:{scrollTop:170}});assert.equal(state.detailHeaderCompact,true);
  state.updateDetailHeader({target:{scrollTop:120}});assert.equal(state.detailHeaderCompact,true);
  state.updateDetailHeader({target:{scrollTop:90}});assert.equal(state.detailHeaderCompact,false);
});

test('pagination waits for in-flight revalidation and includes virtual bottom spacers', () => {
  const virtual={getBoundingClientRect:()=>({bottom:5000})};
  const state=app({window:{innerHeight:800,scrollY:0},document:{querySelector:selector=>selector==='.gallery-virtual-page'?virtual:{getBoundingClientRect:()=>({bottom:900})}}});
  state.gallery={items:[{folder_name:'a'}],page:1,page_size:36,total_pages:2};
  assert.equal(state.isNearLoadedBottom(),false);
  state.galleryFetching=true;assert.equal(state.galleryReadyForLoad(),false);
  state.galleryFetching=false;assert.equal(state.galleryReadyForLoad(),true);
});


test('persisted cover preferences are available before bootstrap and gallery paint', () => {
  const state=app({}, {wide_single_cover:true,random_cover_enabled:false});
  const item={preview_tiles:[{id:1},{id:2}],random_cover:{id:9}};
  assert.equal(state.galleryPreviewTiles(item)[0].id,1);
  assert.equal(state.galleryPreviewTiles(item).length,1);
});


test('jumping to the top restores the first virtual window without preserving an offscreen anchor', () => {
  const moves=[];
  const state=app({window:{scrollY:0,innerHeight:960,setTimeout:fn=>fn(),scrollBy:value=>moves.push(value)},document:{querySelectorAll:()=>[{getBoundingClientRect:()=>({top:20000})},{getBoundingClientRect:()=>({bottom:26000})}],querySelector:()=>null}});
  state.gallery={items:Array.from({length:240},(_,index)=>({folder_name:`item-${index}`})),page_size:36};
  state.galleryRenderStart=132;state.galleryRenderEnd=240;state.scrollDirection='up';state.$nextTick=fn=>fn();
  for(const method of ['measureGalleryWindow','observeGalleryCards','observeGalleryLoadSentinel'])state[method]=()=>{};
  state.shiftGalleryWindowIfNeeded();assert.equal(state.galleryRenderStart,0);assert.equal(state.galleryRenderEnd,108);assert.equal(moves.length,0);
});

test('random sorting uses continuous auto or manual pagination and finishes normally', () => {
  const state=app();state.timeSortMode='random';state.sortOrder='random';
  state.gallery={items:[{folder_name:'a'}],page:1,page_size:24,total_pages:3};
  state.settings.auto_load_enabled=true;assert.equal(state.hasMoreItems(),true);assert.equal(state.autoLoadEnabled(),true);
  state.settings.auto_load_enabled=false;assert.equal(state.showManualLoadMore(),true);
  state.gallery.page=3;assert.equal(state.galleryLoadFinished(),true);
  state.galleryRandomExpired=true;assert.equal(state.galleryLoadFinished(),false);assert.equal(state.hasMoreItems(),false);
});

test('compact gallery adapts between two and four columns using available content width', () => {
  const state=app({window:{innerWidth:1280}});state.compactViewport=true;state.galleryColumnCount=1;
  for(const [width,columns] of [[300,2],[390,2],[560,3],[680,3],[760,4],[1024,4],[1400,4]]) {
    state.galleryStageWidth=width;assert.equal(state.resolveGalleryColumns(),columns);
  }
  state.compactViewport=false;state.narrowViewport=false;state.$refs={galleryStage:{clientWidth:1400}};
  state.galleryColumnCount=5;assert.equal(state.resolveGalleryColumns(),5);
});

test('random pagination keeps a seed and page size across viewport changes', async () => {
  const state=app();state.timeSortMode='random';state.sortOrder='random';state.gallery.page_size=24;
  state.$nextTick=()=>{};
  for(const method of ['cancelGalleryBatchWork','lockGalleryMinHeight','releaseGalleryMinHeight','writeLocalGalleryCache']) state[method]=()=>{};
  state.applyGalleryPagePayload=(payload,page)=>{state.gallery={...payload,page};};
  const requests=[];state.api=async url=>{const params=new URLSearchParams(url.split('?')[1]);requests.push(params);return {items:[{folder_name:`page-${params.get('page')}`}],page_size:Number(params.get('page_size')),total_pages:4}};
  state.preferredGalleryPageSize=()=>24;
  await state.refreshGallery(true);
  const seed=state.galleryRandomSeed;state.preferredGalleryPageSize=()=>36;
  await state.refreshGallery(false,2,'next');
  assert.equal(requests[1].get('random_seed'),seed);assert.equal(requests[1].get('page_size'),'24');
  await state.refreshGallery(true);assert.notEqual(state.galleryRandomSeed,seed);
});
