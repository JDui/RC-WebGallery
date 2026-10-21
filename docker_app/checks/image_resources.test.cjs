const test = require('node:test');
const assert = require('node:assert/strict');
const {createManager} = require('../app/static/progressive-images.js');
function harness({reduced = false, viewport = false} = {}) {
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
    decode() { return this.failDecode ? Promise.reject(Error('bad decode')) : Promise.resolve(); }
    animate(frames, options) {
      const animation = {frames, options, cancel() { this.cancelled = true; }};
      animations.push(animation); return animation;
    }
  }
  let exposure;
  const env = {IntersectionObserver: viewport ? class { constructor(fn) { exposure=fn; } observe() {} unobserve() {} disconnect() {} } : null, Image, setTimeout(fn, ms) { const id = ++serial; timers.set(id, {fn,ms}); return id; }, clearTimeout(id) { timers.delete(id); }, getComputedStyle: () => ({objectFit:'cover',objectPosition:'center',filter:'none',transform:'none',borderRadius:'0px'}), matchMedia: () => ({matches: reduced})};
  const manager = createManager(env);
  const image = new Image();
  const controller = manager.attach(image);
  const tick = async () => { for (let i=0;i<10;i++) await Promise.resolve(); };
  async function loaded(url, ok=true) {
    const request = requests.find(node => node !== image && node.src === url && node.onload);
    assert.ok(request, `pending ${url}`);
    if (ok) { request.naturalWidth = 100; request.onload(); } else request.onerror();
    await tick(); return request;
  }
  async function show(url) { controller.setSource(url); await loaded(url); if (!reduced) animations.at(-1).onfinish(); manager.flush(); }
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
