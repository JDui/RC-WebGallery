const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../app/static/intranet.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function start({url = 'https://gallery.example/?q=photo#album', config = {}, preflight = true, storage = new Map(), fetchImpl} = {}) {
  const navigations = [], requests = [], images = [], timers = new Map(), listeners = {};
  let timerId = 0;
  const location = {href: url, replace: url => navigations.push(['replace', new URL(url)]), assign: url => navigations.push(['assign', new URL(url)])};
  const window = {location, sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    history: {replaceState: (_, __, url) => {location.href = url;}}, addEventListener: (name, fn) => {listeners[name] = fn;},
    setTimeout: (fn, delay) => {timers.set(++timerId, {fn, delay}); return timerId;}, clearTimeout: id => timers.delete(id)};
  const fallback = {addEventListener: (name, fn) => {listeners['fallback:' + name] = fn;}};
  const document = {cookie: '', body: {dataset: {networkConfig: JSON.stringify({origin: 'http://192.168.1.10:8080', auto: true, http_enabled: false, http_port: 80, ...config}), intranetPreflight: preflight ? '1' : '0'}}, getElementById: () => preflight ? fallback : null};
  vm.runInNewContext(script, {window, document, URL, AbortController, Date,
    Image: class {constructor() {this.naturalWidth = this.naturalHeight = 1; images.push(this);}},
    fetch: (url, options) => {requests.push({url, options}); return fetchImpl ? fetchImpl() : new Promise(() => {});}});
  return {window, document, navigations, requests, images, timers, listeners, storage,
    expire(delay) {const entry = [...timers.values()].find(t => t.delay === delay); assert.ok(entry); entry.fn();}};
}
const healthy = () => ({ok: true, json: async () => ({ok: true, service: 'biligalleryrc'})});
test('HTTPS to HTTP uses explicit port and preserves location before probing', () => {
  const state = start({config: {http_enabled: true, http_port: 7860}});
  assert.equal(state.navigations[0][1].href, 'http://gallery.example:7860/?q=photo&_gallery_http_attempted=1#album');
  assert.equal(state.requests.length, 0);
});
test('proxy HTTPS bounce and storage-only fallback cannot loop', () => {
  const first = start({config: {http_enabled: true}});
  const marked = first.navigations[0][1]; marked.protocol = 'https:';
  for (const url of [marked.href, 'https://gallery.example/?q=photo#album']) {
    const bounced = start({url, storage: first.storage, config: {http_enabled: true, auto: false}});
    assert.equal(bounced.navigations[0][1].protocol, 'https:');
    assert.equal(bounced.navigations[0][1].searchParams.get('_gallery_skip_intranet'), '1');
  }
});
test('verified health redirects HTTPS to HTTP LAN and cancels companion probe', async () => {
  const state = start({fetchImpl: async () => healthy()}); await flush();
  assert.equal(state.navigations[0][1].href, 'http://192.168.1.10:8080/?q=photo#album');
  assert.equal(state.requests[0].options.credentials, 'omit');
  assert.equal(state.timers.size, 0);
  assert.equal(state.images[0].onload, null);
});
test('VRB image path succeeds when CORS fetch fails', async () => {
  const state = start({fetchImpl: async () => {throw Error('blocked');}});
  state.images[0].onload(); await flush();
  assert.equal(state.navigations[0][1].origin, 'http://192.168.1.10:8080');
});
test('wrong service and unrelated image fall back without claiming LAN availability', async () => {
  const state = start({fetchImpl: async () => ({ok: true, json: async () => ({ok: true, service: 'other'})})});
  state.images[0].naturalWidth = 200; state.images[0].onload(); await flush();
  assert.equal(state.navigations[0][1].origin, 'https://gallery.example');
  assert.equal(state.navigations[0][1].hash, '#album');
});
test('timeout ignores late success and refresh does not probe again', async () => {
  let resolve;
  const first = start({fetchImpl: () => new Promise(r => {resolve = r;})});
  first.expire(1500); resolve(healthy()); await flush();
  assert.equal(first.navigations.length, 1);
  const again = start({preflight: false, storage: first.storage});
  assert.equal(again.requests.length, 0); assert.equal(again.navigations.length, 0);
  const newTab = start({preflight: false}); assert.equal(newTab.requests.length, 1);
});
test('manual connection works with auto off, preserves latest SPA URL and Back', () => {
  const state = start({preflight: false, config: {auto: false}});
  state.window.location.href = 'https://gallery.example/?view=favorites#latest';
  state.window.galleryNetwork.connect({intranet_redirect_host: 'fd00::12', intranet_redirect_port: 7860, intranet_redirect_protocol: 'http'});
  assert.equal(state.navigations[0][0], 'assign');
  assert.equal(state.navigations[0][1].href, 'http://[fd00::12]:7860/?view=favorites#latest');
});
test('manual navigation cancels late automatic result and explicit fallback also cancels', async () => {
  for (const action of ['manual', 'fallback']) {
    let resolve;
    const state = start({fetchImpl: () => new Promise(r => {resolve = r;})});
    if (action === 'manual') state.window.galleryNetwork.connect({intranet_use_proxy_host: true, site_proxy_host: '192.168.1.11', intranet_redirect_port: 80, intranet_redirect_protocol: 'http'});
    else state.listeners['fallback:click']();
    resolve(healthy()); await flush();
    assert.equal(state.navigations.length, action === 'manual' ? 1 : 0);
  }
});
test('already at target or explicit bypass never probes, and bypass markers are cleaned', () => {
  const local = start({url: 'https://192.168.1.10:8443/', preflight: false,
    config: {origin: 'https://192.168.1.10:8443', http_enabled: true}});
  assert.equal(local.navigations.length, 0);
  assert.equal(local.requests.length, 0);
  for (const url of ['http://192.168.1.10:8080/', 'https://gallery.example/?q=1&_gallery_skip_intranet=1&_gallery_http_attempted=1#x']) {
    const state = start({url, preflight: false, config: {http_enabled: true}});
    assert.equal(state.requests.length, 0); assert.equal(state.navigations.length, 0);
    assert.equal(new URL(state.window.location.href).searchParams.has('_gallery_skip_intranet'), false);
  }
});
