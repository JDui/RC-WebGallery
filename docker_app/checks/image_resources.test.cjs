const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createManager} = require('../app/static/progressive-images.js');
function harness({reduced = false, viewport = false, transition = true} = {}) {
  let serial = 0;
  const timers = new Map(), requests = [], animations = [];
  class Image {
    constructor() { this.attrs = {}; this.dataset = {}; this.style = {removeProperty(key) { delete this[key.replace(/-([a-z])/g, (_, c) => c.toUpperCase())]; }}; this.naturalWidth = 0; this.siblings = []; }
    set src(value) { this.attrs.src = value; requests.push(this); }
    get src() { return this.attrs.src || ''; }
    setAttribute(key, value) { this.attrs[key] = value; }
    removeAttribute(key) { delete this.attrs[key]; }
    closest() { return viewport ? {} : null; }
    before(node) { node.parent = this; this.siblings.push(node); }
    remove() { if (this.parent) this.parent.siblings = this.parent.siblings.filter(x => x !== this); }
    decode() { return this.failDecode || this.loading === 'lazy' ? Promise.reject(Error('bad decode')) : Promise.resolve(); }
    animate(frames, options) {
      const animation = {frames, options, cancel() { this.cancelled = true; }};
      animations.push(animation); return animation;
    }
  }
  let exposure;
  const env = {IntersectionObserver: viewport ? class { constructor(fn) { exposure=fn; } observe() {} unobserve() {} disconnect() {} } : null, Image, setTimeout(fn, ms) { const id = ++serial; timers.set(id, {fn,ms}); return id; }, clearTimeout(id) { timers.delete(id); }, getComputedStyle: () => ({objectFit:'cover',objectPosition:'center',filter:'none',transform:'none',borderRadius:'0px'}), matchMedia: () => ({matches: reduced})};
  const manager = createManager(env);
  const image = new Image();
  const controller = manager.attach(image, {transition});
  const tick = async () => { for (let i=0;i<10;i++) await Promise.resolve(); };
  async function loaded(url, ok=true) {
    const request = requests.find(node => node !== image && node.src === url && node.onload);
    assert.ok(request, `pending ${url}`);
    if (ok) { request.naturalWidth = 100; request.onload(); } else request.onerror();
    await tick(); return request;
  }
  async function show(url) { controller.setSource(url); await loaded(url); if (!reduced && transition) animations.at(-1).onfinish(); manager.flush(); }
  return {manager,image,controller,requests,animations,timers,tick,loaded,show,expose: value=>exposure([{target:image,isIntersecting:value}])};
}
test('old pixels survive fetching, overlap the decoded replacement, then retire', async () => {
  const h = harness(); await h.show('tiny');
  h.controller.setSource('small');
  assert.equal(h.image.src,'tiny');
  await h.loaded('small');
  assert.equal(h.image.src,'small');
  assert.equal(h.image.siblings.length,1);
  const old = h.image.siblings[0];
  assert.equal(old.src,'tiny');
  const fade = h.animations.at(-1);
  assert.equal(fade.options.duration,260);
  assert.deepEqual(fade.frames,[{opacity:0},{opacity:1}]);
  fade.onfinish();
  assert.equal(h.image.siblings.length,0);
  assert.equal(old.attrs['data-image-dirty'],'1');
  assert.equal(fade.cancelled,true);
  h.manager.flush();
  assert.equal(old.src,'');
  assert.equal(h.manager.dirty.size,0);
  assert.equal(h.image.style.willChange,undefined);
});
test('visible images decode without native lazy loading deferring the committed frame', async () => {
  const h = harness({viewport:true});
  h.image.loading = 'lazy';
  h.controller.setSource('visible');
  assert.equal(h.requests.length,0);
  h.expose(true);
  await h.loaded('visible');
  assert.equal(h.image.src,'visible');
  assert.equal(h.image.dataset.progressiveReady,'1');
  h.controller.dispose();h.manager.flush();
});
test('rapid upgrades during overlap coalesce to the latest level', async () => {
  const h=harness(); await h.show('tiny');
  h.controller.setSource('small'); await h.loaded('small');
  h.controller.setSource('thumb'); h.controller.setSource('original');
  assert.equal(h.image.siblings.length,1);
  assert.equal(h.requests.filter(x=>x.src==='thumb').length,0);
  h.animations.at(-1).onfinish();
  await h.loaded('original');
  assert.equal(h.image.src,'original');
  assert.equal(h.image.siblings.length,1);
  h.animations.at(-1).onfinish();h.manager.flush();
  assert.equal(h.image.siblings.length,0);
});
test('cancelled pending requests cannot overwrite a newer source', async () => {
  const h=harness(); await h.show('tiny');
  h.controller.setSource('slow');
  const stale=h.requests.find(x=>x.src==='slow');
  const callback=stale.onload;
  h.controller.setSource('thumb');
  await h.loaded('thumb');
  stale.naturalWidth=100; await callback(); await h.tick();
  assert.equal(h.image.src,'thumb');
  h.animations.at(-1).onfinish();h.manager.flush();
  assert.equal(stale.src,'');
});
test('failed loads and timeouts keep the last good frame without retry loops', async () => {
  const h=harness(); await h.show('tiny');
  h.controller.setSource('broken'); await h.loaded('broken',false);
  assert.equal(h.image.src,'tiny');assert.equal(h.image.siblings.length,0);
  h.controller.setSource('timeout');
  [...h.timers.values()].find(x=>x.ms===8000).fn(); await h.tick();
  h.manager.flush();
  assert.equal(h.image.src,'tiny');
  assert.equal(h.requests.filter(x=>x.src==='timeout').length,0);
});
test('unmount during a fade releases animations, outgoing frames and pending references', async () => {
  const h=harness(); await h.show('tiny');
  h.controller.setSource('small');await h.loaded('small');
  const old=h.image.siblings[0], animation=h.animations.at(-1);
  h.controller.dispose();h.manager.flush();
  assert.equal(animation.cancelled,true);assert.equal(old.src,'');
  assert.equal(h.image.src,'');assert.equal(h.image.siblings.length,0);
  assert.equal(h.manager.active.size,0);assert.equal(h.manager.dirty.size,0);
  assert.equal(h.timers.size,0);
});
test('suspending a hidden page ends overlap and resumes the latest queued quality', async () => {
  const h=harness();await h.show('tiny');
  h.controller.setSource('small');await h.loaded('small');
  h.controller.setSource('thumb');h.controller.suspend(true);h.manager.flush();
  assert.equal(h.image.siblings.length,0);
  assert.equal(h.image.src,'small');
  h.controller.suspend(false);await h.loaded('thumb');
  assert.equal(h.image.src,'thumb');
  h.controller.dispose();h.manager.flush();assert.equal(h.timers.size,0);
});
test('reduced motion promotes immediately and still clears transient resources', async () => {
  const h=harness({reduced:true});await h.show('tiny');await h.show('small');
  assert.equal(h.animations.length,0);assert.equal(h.image.siblings.length,0);
  assert.equal(h.manager.dirty.size,0);assert.equal(h.image.src,'small');
});
test('missing animation finish events have a bounded cleanup fallback', async () => {
  const h=harness();await h.show('tiny');
  h.controller.setSource('small');await h.loaded('small');
  [...h.timers.values()].find(x=>x.ms===340).fn();
  h.manager.flush();assert.equal(h.image.siblings.length,0);assert.equal(h.manager.dirty.size,0);
});
test('a decode failure while committing restores the old image and retires the snapshot', async () => {
  const h=harness();await h.show('tiny');
  h.image.failDecode=true;
  h.controller.setSource('bad-original');await h.loaded('bad-original');h.manager.flush();
  assert.equal(h.image.src,'tiny');assert.equal(h.image.siblings.length,0);
  assert.equal(h.manager.dirty.size,0);assert.equal(h.animations.length,1);
});
test('clearing a source during overlap cancels the animation and releases both frames', async () => {
  const h=harness();await h.show('tiny');
  h.controller.setSource('small');await h.loaded('small');
  h.controller.setSource('');h.manager.flush();
  assert.equal(h.image.src,'');assert.equal(h.image.siblings.length,0);
  assert.equal(h.manager.dirty.size,0);assert.equal(h.timers.size,0);
});
test('committing an image never reclaims card container backgrounds', async () => {
  const h=harness();
  let removed=0;
  const host={style:{backgroundImage:'url(tiny)',removeProperty(key){if(key==='background-image'){removed+=1;this.backgroundImage='';}}},closest(){return null;},setAttribute(){},removeAttribute(){}};
  h.image.closest=()=>host;
  h.controller.setSource('small');await h.loaded('small');
  h.animations.at(-1).onfinish();h.manager.flush();
  assert.equal(removed,0);
  assert.equal(host.style.backgroundImage,'url(tiny)');
  assert.equal(h.manager.dirty.size,0);
});
test('cache warming releases its decoded Image and handlers after completion', async () => {
  const h=harness();
  const pending=h.manager.warm('warm');const image=await h.loaded('warm');
  assert.equal(await pending,true);h.manager.flush();
  assert.equal(image.src,'');assert.equal(image.onload,null);assert.equal(image.onerror,null);
});


test('global stream limits concurrent decodes and cancelled queued sources never start', async () => {
  const h=harness(); const controllers=[];
  for(let index=0;index<10;index++) {const image=new h.image.constructor();const controller=h.manager.attach(image);controllers.push(controller);controller.setSource(`queued-${index}`);}
  assert.equal(h.requests.filter(node=>node.onload).length,6);
  controllers[7].dispose();
  await h.loaded('queued-0');
  assert.ok(h.requests.some(node=>node.src==='queued-6'));
  assert.equal(h.requests.filter(node=>node.onload).length,6);
  assert.equal(h.requests.some(node=>node.src==='queued-7'),false);
  h.manager.dispose(); h.manager.flush(); assert.equal(h.manager.active.size,0);assert.equal(h.timers.size,0);
});

test('offscreen gallery images release decoded sources and return without replaying fades', async () => {
  const h=harness({viewport:true});
  h.controller.setSource('visible'); assert.equal(h.requests.length,0);
  h.expose(true); await h.loaded('visible'); h.animations.at(-1).onfinish();h.manager.flush();
  h.expose(false); assert.equal(h.image.src,'');
  h.expose(true); await h.loaded('visible'); h.manager.flush();
  assert.equal(h.image.src,'visible'); assert.equal(h.animations.length,1);
  h.controller.dispose();h.manager.flush();assert.equal(h.timers.size,0);
});

test('atomic icon and QR replacement retains the old source until decode, without opacity animation', async () => {
  const h=harness({transition:false});await h.show('icon-one');
  h.controller.setSource('icon-two');assert.equal(h.image.src,'icon-one');
  await h.loaded('icon-two');assert.equal(h.image.src,'icon-two');
  assert.equal(h.image.style.opacity,undefined);assert.equal(h.animations.length,0);
  h.controller.setSource('broken');await h.loaded('broken',false);
  assert.equal(h.image.src,'icon-two');h.manager.dispose();assert.equal(h.timers.size,0);
});

test('cancelled viewer preparation cannot keep an in-flight slot or decoded image alive', async () => {
  const h=harness();const pending=h.manager.prepare('cancelled');pending.cancel();
  assert.equal(await pending.promise,false);h.manager.flush();
  const ready=h.manager.prepare('prepared');const image=await h.loaded('prepared');
  assert.equal(await ready.promise,true);h.manager.flush();assert.equal(image.src,'');
  assert.equal(h.timers.size,0);
});

test('a previously seen replacement still overlaps the old pixels smoothly', async () => {
  const h=harness();await h.show('first');await h.show('second');
  const count=h.animations.length;h.controller.setSource('first');await h.loaded('first');
  assert.equal(h.animations.length,count+1);assert.equal(h.image.siblings[0].src,'second');
  h.manager.dispose();assert.equal(h.timers.size,0);
});

test('a failed animation API completes the replacement and never blocks later sources', async () => {
  const h=harness();await h.show('first');
  h.image.animate=()=>{throw Error('animation unavailable');};
  h.controller.setSource('second');await h.loaded('second');h.manager.flush();
  assert.equal(h.image.src,'second');assert.equal(h.image.siblings.length,0);
  assert.equal(h.image.style.opacity,undefined);assert.equal(h.image.style.willChange,undefined);
  h.controller.setSource('third');await h.loaded('third');h.manager.flush();
  assert.equal(h.image.src,'third');assert.equal(h.timers.size,0);
});

test('externally cancelled image fades release the old pixels and drain the newest queued source', async () => {
  const h=harness();await h.show('first');
  h.controller.setSource('second');await h.loaded('second');
  h.controller.setSource('third');h.animations.at(-1).oncancel();
  await h.loaded('third');assert.equal(h.image.src,'third');
  h.manager.dispose();assert.equal(h.image.siblings.length,0);assert.equal(h.timers.size,0);
});

test('page hiding during mounted decode retains the committed frame until resume', async () => {
  const h=harness();await h.show('first');
  const decode=h.image.decode;let finishDecode;
  h.image.decode=()=>new Promise(resolve=>{finishDecode=resolve;});
  h.controller.setSource('second');await h.loaded('second');
  assert.equal(h.image.src,'second');assert.equal(h.image.siblings.length,1);
  h.controller.suspend(true);h.manager.flush();
  assert.equal(h.image.src,'first','hidden-page suspension restores the last committed frame');
  assert.equal(h.image.siblings.length,0);
  finishDecode();await h.tick();
  assert.equal(h.image.src,'first','late mounted decode cannot overwrite the restored frame');
  h.image.decode=decode;h.controller.suspend(false);await h.loaded('second');
  assert.equal(h.image.src,'second');
  h.manager.dispose();assert.equal(h.timers.size,0);
});

test('the actual video teardown directive cancels hover retirement and releases removed feed players', () => {
  const listeners=new Map(),directives=new Map();
  const env={Alpine:{directive:(name,callback)=>directives.set(name,callback)},
    addEventListener(){},setTimeout,clearTimeout};
  const context=vm.createContext({window:env,document:{addEventListener:(name,callback)=>listeners.set(name,callback)},console});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static/progressive-images.js'),'utf8'),context);
  listeners.get('alpine:init')();
  const events=[];
  const video={__hoverPlayRequest:2,__playbackRequest:5,__hoverRetireCancel:()=>events.push('cancel-retirement'),
    pause:()=>events.push('pause'),removeAttribute:name=>events.push(`remove:${name}`),load:()=>events.push('release'),
    onended(){},onerror(){},onloadeddata(){},oncanplay(){},onpause(){}};
  let dispose;directives.get('retire-video')(video,{}, {cleanup:callback=>{dispose=callback;}});
  dispose();
  assert.deepEqual(events,['cancel-retirement','pause','remove:src','remove:poster','release']);
  assert.equal(video.__hoverPlayRequest,3);assert.equal(video.__playbackRequest,6);
  assert.equal(video.onpause,null);assert.equal(video.onloadeddata,null);
  const html=fs.readFileSync(path.join(__dirname,'../app/templates/index.html'),'utf8');
  const feed=html.match(/<video\b(?=[^>]*data-video-player)[^>]*>/)?.[0];
  assert.ok(feed?.includes('x-retire-video'),'feed players must use the tested teardown directive');
});
