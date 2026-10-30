"""Isolated real-template image handoff regressions; no NAS or external services.

Run: python docker_app/checks/image_handoff_browser.py
Requires Playwright + installed Edge, Pillow, Jinja2 and ffmpeg. Evidence is
written to temp/flicker-fix-20261030-terra/; failures return a nonzero status.
"""
import argparse
import base64
import io
import json
import mimetypes
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "docker_app" / "app"
OUT = ROOT / "temp" / "flicker-fix-20261030-terra"
OUT.mkdir(parents=True, exist_ok=True)
REQUESTS = []
ERRORS = []

HTML = Environment(loader=FileSystemLoader(APP / "templates")).get_template("index.html").render(
    app_version="fixture", app_title="Image handoff fixture", ui_revision="fixture",
    theme_color="#eef2f6", theme_rev="fixture", intranet_access="",
    initial_ui_settings={"gallery_default_random": False})
HTML = HTML.replace('<script defer src="/static/vendor/alpinejs.min.js',
                    '<script defer src="/fixture.js"></script><script defer src="/static/vendor/alpinejs.min.js')

FIXTURE = r"""
const originalGalleryApp = galleryApp;
window.sampleItems = count => Array.from({length:count}, (_, i) => ({
  folder_name:`sample-${i}`, item_key:`sample-${i}`, title:`Sample ${i}`,
  preview_tiles:[{id:i+1,width:600,height:800,small_thumb_url:`/media/g${i}.svg`}],
  random_cover:{id:i+1,width:600,height:800,small_thumb_url:`/media/g${i}.svg`},
  width:600,height:800,small_thumb_url:`/media/g${i}.svg`,pair_index:1,
  has_images:true,has_livephoto:false,image_count:1,asset_count:1,
  subscription_name:'Sample creator',pub_time:'2026-10-30'
}));
galleryApp = function(settings) {
  const app = originalGalleryApp(settings);
  app.init = function() {
    this.api = async () => ({});
    this.scheduleFixedLayoutMetrics = () => {};
    this.scheduleRecentUpdatesMeasure = () => {};
    this.scheduleIdleWork = () => {};
    this.refreshGalleryImmediately = () => {};
    this.refreshSidebarCounts = async () => {};
    this.settings.wide_single_cover = true;
    this.subscriptionSectionCollapsed = false;
    this.gallery = {items:sampleItems(12),page:1,page_size:36,total_pages:1,total:12};
    this.galleryRenderEnd = 12;
    this.recentUpdates={groups:[{task_id:1,task_type:'pull',items:sampleItems(2)}],total_groups:1,total_items:2};
    this.subscriptions=[{uid:'1',uname:'Fixture icon',updated_at:'one',icon_url:'/storage/icon.svg'}];
    this.updateViewportMode();
    window.addEventListener('keydown', event => this.handleKeydown(event));
  };
  return app;
};
window.app = () => Alpine.$data(document.body);
window.frameInfo = selector => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const s=getComputedStyle(el), r=el.getBoundingClientRect();
  return {src:el.getAttribute('src'), naturalWidth:el.naturalWidth || 0,
    opacity:Number(s.opacity), visibility:s.visibility, display:s.display,
    top:r.top, height:r.height, ready:el.dataset.previewReady || el.dataset.progressiveReady || '',
    animations:el.getAnimations().map(a=>a.animationName || a.constructor.name)};
};
window.startFrames = (selector, duration=1400) => {
  window.frames=[]; const start=performance.now();
  function record() {
    const a=app(), v=document.querySelector('.viewer-video'), loading=document.querySelector('.viewer-original-loading');
    frames.push({t:performance.now()-start, element:frameInfo(selector),
      gallery:frameInfo('.gallery-layout'), section:frameInfo('[data-testid="section-gallery"]'),
      viewerOpen:a.viewer.open, index:a.viewerIndex, pair:a.viewer.pair?.pair_index,
      pending:a.viewerPendingEntry?.pair?.pair_index, token:a.viewerToken,
      offsetY:a.viewerOffsetY, dragging:a.viewerVerticalDragging, swipe:!!a.viewerSwipeFinish,
      showVideo:a.viewer.showVideo, video:v?{ready:v.readyState,paused:v.paused,src:v.getAttribute('src'),
        current:v.currentTime,duration:v.duration,opacity:Number(getComputedStyle(v).opacity),poster:v.getAttribute('poster')}:null,
      loading:loading?{display:getComputedStyle(loading).display,now:loading.getAttribute('aria-valuenow'),
        label:loading.getAttribute('aria-label'),indeterminate:!!loading.querySelector('.indeterminate')}:null});
    if (performance.now()-start < duration) requestAnimationFrame(record);
  }
  record();
};
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        REQUESTS.append({"path": self.path, "t": time.time()})
        cache = "no-store"
        status = 200
        if path == "/":
            body, mime = HTML.encode(), "text/html"
        elif path == "/fixture.js":
            body, mime = FIXTURE.encode(), "text/javascript"
        elif path.startswith("/media/") and path.endswith(".mp4"):
            if "slow" in path:
                time.sleep(1)
            if "fail" in path:
                status, body, mime = 404, b"missing", "text/plain"
            else:
                body, mime = (OUT / "sample.mp4").read_bytes(), "video/mp4"
                cache = "public, max-age=900"
        elif path.startswith(("/media/", "/storage/")):
            if "slow" in path:
                time.sleep(.65)
            if "original" in path:
                time.sleep(.25)
            if "fail" in path:
                status, body, mime = 404, b"missing", "text/plain"
            else:
                color = "#36a5da" if any(word in path for word in ("next", "blue", "third")) else "#dc5656"
                width, height = (800, 400) if 'wide-gesture' in path else (600, 800)
                body = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="{width}" height="{height}" fill="{color}"/></svg>'.encode()
                mime, cache = "image/svg+xml", "public, max-age=900"
        else:
            static_path = "/static/" + path.rsplit("/", 1)[-1] if path.startswith("/theme/") else path
            local = APP / static_path.lstrip("/")
            if local.is_file() and local.resolve().is_relative_to(APP.resolve()):
                body = local.read_bytes()
                mime = mimetypes.guess_type(str(local))[0] or "application/octet-stream"
            else:
                body, mime = b"{}", "application/json"
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def image_pixel(page, selector, name, fraction_y=.5):
    box = page.locator(selector).first.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0, (selector, box)
    x, y = round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] * fraction_y)
    data = page.screenshot(path=str(OUT / f"{name}.png"), animations="allow")
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert 0 <= x < image.width and 0 <= y < image.height, (x, y, image.size)
    return list(image.getpixel((x, y)))


def color_distance(pixel):
    """Distance from red-to-blue compositing segment; gray/black gaps fail."""
    old, new = (220, 86, 86), (54, 165, 218)
    v = [b-a for a,b in zip(old,new)]
    t = max(0, min(1, sum((p-a)*d for p,a,d in zip(pixel,old,v)) / sum(d*d for d in v)))
    return sum((p-(a+t*d))**2 for p,a,d in zip(pixel,old,v))**0.5


def assert_pixels(samples, name, tolerance=55):
    assert samples, f"{name}: no compositor/screenshot samples"
    bad = [s for s in samples if color_distance(s["pixel"]) > tolerance]
    assert not bad, f"{name}: blank/unexpected composed pixels: {bad[:5]}"


def capture(page, selector, name, change, duration=1250, fraction_y=.5):
    """Collect CDP compositor pixels plus sampled screenshots and RAF DOM states."""
    box = page.locator(selector).first.bounding_box()
    assert box and box["height"] > 0 and box["width"] > 0, (selector, box)
    x, y = round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] * fraction_y)
    session = page.context.new_cdp_session(page)
    pixels = []
    def on_frame(event):
        data = base64.b64decode(event["data"])
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if 0 <= x < im.width and 0 <= y < im.height:
            pixel = list(im.getpixel((x,y)))
            pixels.append({"timestamp":event["metadata"]["timestamp"],"pixel":pixel})
            if color_distance(pixel) > 55:
                (OUT / f"{name}-unexpected-{len(pixels):03}.jpg").write_bytes(data)
        try:
            session.send("Page.screencastFrameAck", {"sessionId":event["sessionId"]})
        except Exception as exc:
            if 'closed' not in str(exc).lower():
                ERRORS.append(f"CDP screencast acknowledgement: {exc}")
    session.on("Page.screencastFrame", on_frame)
    session.send("Page.startScreencast", {"format":"jpeg","quality":95,"everyNthFrame":1})
    page.evaluate("([selector, duration]) => startFrames(selector, duration)", [selector, duration])
    change()
    page.wait_for_timeout(180)
    waiting = image_pixel(page, selector, name + "-waiting", fraction_y)
    page.wait_for_timeout(350)
    near_end = image_pixel(page, selector, name + "-near-end", fraction_y)
    page.wait_for_timeout(max(1, duration - 530) + 120)
    after = image_pixel(page, selector, name + "-after", fraction_y)
    session.send("Page.stopScreencast")
    session.remove_listener("Page.screencastFrame", on_frame)
    page.wait_for_timeout(40)
    session.detach()
    result = {"name":name,"pixels":pixels,"waiting":waiting,"near_end":near_end,"after":after,
              "frames":page.evaluate("window.frames")}
    (OUT / (name + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert_pixels(pixels + [{"pixel":waiting},{"pixel":near_end},{"pixel":after}], name)
    return result


def check(condition, detail):
    assert condition, detail


def fresh(browser, base, width=1440, height=900, reduced=False, touch=False):
    page = browser.new_page(viewport={"width":width,"height":height},
                            reduced_motion="reduce" if reduced else "no-preference",
                            has_touch=touch, is_mobile=touch)
    page.on("pageerror", lambda exc: ERRORS.append(str(exc)))
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
    page.goto(base)
    page.wait_for_function("window.Alpine && document.querySelector('.gallery-item-card img')?.naturalWidth > 0")
    page.wait_for_timeout(550)
    return page


def gallery_case(browser, base, width, height, reduced):
    mode = f"{width}-{'reduced' if reduced else 'normal'}"
    page = fresh(browser, base, width, height, reduced)
    try:
        page.evaluate("""() => {window.savedImage=document.querySelector('.gallery-item-card img');
          startFrames('.gallery-item-card img',600);const a=app();a.navigationView='gallery';a.scrollViewTop();
          a.applyGalleryPagePayload({...a.gallery,items:a.gallery.items.map(i=>({...i,title:i.title+' refreshed'}))},1,'replace');}""")
        page.wait_for_timeout(700)
        frames = page.evaluate("window.frames")
        check(page.evaluate("savedImage===document.querySelector('.gallery-item-card img')"), f"{mode}: metadata replaced image node")
        opacities = [f["gallery"]["opacity"] * f["section"]["opacity"] for f in frames if f["gallery"] and f["section"]]
        check(opacities and min(opacities) >= .999, f"{mode}: metadata effective opacity {min(opacities)}")
        page.evaluate("window.savedImage=document.querySelector('.gallery-item-card img')")
        result = capture(page, '.gallery-item-card .photo-collage-tile', 'cover-'+mode, lambda: page.evaluate("""() => {
          const a=app();a.gallery.items[0].random_cover={id:999,width:600,height:800,small_thumb_url:'/media/slow-next-cover.svg'};
        }"""))
        check(page.evaluate("savedImage===document.querySelector('.gallery-item-card img')"), f"{mode}: cover id replaced image node")
        check(color_distance(result["waiting"]) < 30 and result["waiting"][0] > result["waiting"][2], f"{mode}: old cover not retained {result['waiting']}")
        check(result["after"][2] > result["after"][0], f"{mode}: new cover not displayed {result['after']}")
        return {"metadata_min_opacity":min(opacities),"same_node":True,"waiting":result["waiting"],"after":result["after"],"compositor_frames":len(result["pixels"])}
    finally:
        page.close()


def regular_images_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("app().currentView='recent-updates'")
        page.wait_for_function("document.querySelector('.recent-update-card img')?.naturalWidth > 0")
        page.wait_for_timeout(450)
        page.evaluate("window.savedImage=document.querySelector('.recent-update-card img')")
        recent = capture(page, '.recent-update-card .photo-collage-tile', 'recent-cover', lambda: page.evaluate("""() => {
          app().recentUpdates.groups[0].items[0].random_cover={id:999,width:600,height:800,small_thumb_url:'/media/slow-next-recent.svg'};
        }"""))
        check(page.evaluate("savedImage===document.querySelector('.recent-update-card img')"), "recent cover node replaced")
        check(recent['waiting'][0] > recent['waiting'][2] and recent['after'][2] > recent['after'][0], "recent cover did not change red to blue")

        page.evaluate("""() => {const a=app();a.currentView='duplicates';a.duplicateHasCache=true;
          a.duplicateGroups=[{signature:'fixture',group_key:'fixture',post_count:1,matched_image_count:1,similarity:99,
            items:sampleItems(1).map(i=>({...i,feature_tags:[]})),matches:[{left_url:'/media/match-left.svg',right_url:'/media/match-right.svg',similarity:99}]}];}""")
        page.wait_for_function("document.querySelector('.duplicate-post-card img')?.naturalWidth > 0 && document.querySelector('.duplicate-match-row img')?.naturalWidth > 0")
        page.wait_for_timeout(400)
        page.evaluate("window.savedImage=document.querySelector('.duplicate-post-card img')")
        duplicate = capture(page, '.duplicate-post-card .photo-collage-tile', 'duplicate-cover', lambda: page.evaluate("""() => {
          app().duplicateGroups[0].items[0].random_cover={id:999,width:600,height:800,small_thumb_url:'/media/slow-next-duplicate.svg'};
        }"""))
        check(page.evaluate("savedImage===document.querySelector('.duplicate-post-card img')"), "duplicate cover node replaced")
        page.locator('.duplicate-match-row img').first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        match = capture(page, '.duplicate-match-row img', 'duplicate-match', lambda: page.evaluate("app().duplicateGroups[0].matches[0].left_url='/media/slow-next-match.svg'"))
        check(match['waiting'][0] > match['waiting'][2] and match['after'][2] > match['after'][0], "match swap did not complete")

        page.evaluate("window.scrollTo(0,0);app().currentView='gallery'")
        page.wait_for_function("document.querySelector('.sidebar-subscription-list img')?.naturalWidth > 0")
        page.wait_for_timeout(200)
        page.evaluate("startFrames('.sidebar-subscription-list img',600);app().subscriptions[0].updated_at='new-version'")
        page.wait_for_timeout(700)
        icon_frames = [f['element'] for f in page.evaluate('window.frames') if f['element']]
        check(icon_frames and min(f['opacity'] for f in icon_frames) >= .999 and all(f['naturalWidth'] for f in icon_frames), "visible icon flashed on metadata version change")

        page.evaluate("""() => {const a=app();a.currentView='subscriptions';a.subscriptionPanel='xhs';a.xhsCreateExpanded=true;
          a.xhsLoginSession={};a.xhsQr={image_data_url:'/media/qr-one.svg',phase:'await_scan'};}""")
        page.wait_for_function("document.querySelector('.xhs-qr-image')?.naturalWidth > 0")
        page.locator('.xhs-qr-image').scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        qr = capture(page, '.xhs-qr-image', 'qr-refresh', lambda: page.evaluate("app().xhsQr={image_data_url:'/media/slow-next-qr.svg',phase:'refreshing'}"))
        check(qr['waiting'][0] > qr['waiting'][2] and qr['after'][2] > qr['after'][0], "QR swap did not complete")
        page.evaluate("""() => {const a=app();a.currentView='settings';a.settingsGroup='account';
          a.qr={image_data_url:'/media/bili-qr.svg',message:'fixture'};}""")
        page.wait_for_function("document.querySelector('.qr-box img')?.naturalWidth > 0")
        page.locator('.qr-box img').scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        bili_qr = capture(page, '.qr-box img', 'bili-qr-refresh', lambda: page.evaluate("app().qr.image_data_url='/media/slow-next-bili-qr.svg'"))
        check(bili_qr['waiting'][0] > bili_qr['waiting'][2] and bili_qr['after'][2] > bili_qr['after'][0], "Bilibili QR swap did not complete")
        return {"recent_frames":len(recent['pixels']),"duplicate_frames":len(duplicate['pixels']),"match_frames":len(match['pixels']),
                "icon_min_opacity":min(f['opacity'] for f in icon_frames),"icon_final_src":icon_frames[-1]['src'],"xhs_qr_frames":len(qr['pixels']),"bili_qr_frames":len(bili_qr['pixels'])}
    finally:
        page.close()


def replacement_failure_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("app().gallery.items[0].random_cover.small_thumb_url='/media/slow-fail-cover.svg'")
        page.wait_for_timeout(1000)
        check(page.evaluate("document.querySelector('.gallery-item-card img').getAttribute('src')==='/media/g0.svg'"), "failed replacement discarded old source")
        failed = image_pixel(page, '.gallery-item-card .photo-collage-tile', 'replacement-failure')
        check(failed[0] > failed[2] and color_distance(failed) < 30, f"failed replacement lost old frame: {failed}")
        page.evaluate("app().gallery.items[0].random_cover.small_thumb_url='/media/slow-stale-red.svg'")
        page.wait_for_timeout(140)
        page.evaluate("app().gallery.items[0].random_cover.small_thumb_url='/media/next-latest.svg'")
        page.wait_for_timeout(1100)
        src = page.evaluate("document.querySelector('.gallery-item-card img').getAttribute('src')")
        pixel = image_pixel(page, '.gallery-item-card .photo-collage-tile', 'replacement-cancelled')
        check(src == '/media/next-latest.svg' and pixel[2] > pixel[0], f"cancelled replacement overwrote new image: {src}, {pixel}")
        return {"failed_pixel":failed,"latest_src":src,"latest_pixel":pixel}
    finally:
        page.close()


def hidden_lazy_case(browser, base):
    page = fresh(browser, base)
    try:
        start = len(REQUESTS)
        page.evaluate("""() => {const a=app();a.duplicateHasCache=true;
          a.duplicateGroups=[{signature:'hidden',group_key:'hidden',post_count:1,matched_image_count:1,similarity:99,
            items:sampleItems(1).map(i=>({...i,feature_tags:[],random_cover:{id:999,width:600,height:800,small_thumb_url:'/media/hidden-lazy-cover.svg'}})),
            matches:[{left_url:'/media/hidden-lazy-match.svg',right_url:'/media/hidden-lazy-right.svg',similarity:99}]}];}""")
        page.wait_for_function("document.querySelector('.duplicate-post-card img') && document.querySelector('.duplicate-match-row img')")
        page.wait_for_timeout(400)
        hidden = page.evaluate("[...document.querySelectorAll('.duplicate-post-card img,.duplicate-match-row img')].map(e=>({src:e.getAttribute('src'),width:e.naturalWidth}))")
        hidden_requests = [r['path'] for r in REQUESTS[start:] if 'hidden-lazy' in r['path']]
        check(not hidden_requests and all(not item['src'] and item['width']==0 for item in hidden), f"hidden lazy resources loaded prematurely: {hidden}, {hidden_requests}")
        page.evaluate("app().currentView='duplicates'")
        page.wait_for_function("document.querySelector('.duplicate-post-card img')?.naturalWidth > 0 && document.querySelector('.duplicate-match-row img')?.naturalWidth > 0")
        return {"hidden_requests":hidden_requests,"loaded_after_reveal":True}
    finally:
        page.close()


def initial_viewer(page):
    page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'fixture'},
      pair:{pair_index:1,image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'}}}];
      a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0]);}""")
    page.wait_for_function("app().viewer.open && app().viewerImageReady")
    page.wait_for_timeout(500)


def viewer_case(browser, base, width, height, reduced):
    mode = f"{width}-{'reduced' if reduced else 'normal'}"
    page = fresh(browser, base, width, height, reduced)
    try:
        initial_viewer(page)
        before = image_pixel(page, '.viewer-media.stage', 'viewer-'+mode+'-before')
        # Append after initial viewer preparation: the neighbor has not been prefetched.
        page.evaluate("""() => app().viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          image:{url:'/media/slow-next-original.svg',small_thumb_url:'/media/slow-next-thumb.svg'}}})""")
        result = capture(page, '.viewer-media.stage', 'viewer-'+mode, lambda: page.evaluate("() => {app().showNextPair();}"), 1900)
        check(result['waiting'][0] > result['waiting'][2], f"{mode}: cold viewer discarded previous frame")
        check(result['after'][2] > result['after'][0], f"{mode}: destination not displayed")
        pending = [f for f in result['frames'] if f['pending'] == 2]
        check(pending and all(f['pair'] == 1 and f['index'] == 0 for f in pending), f"{mode}: pending viewer exposed destination before preparation")
        indicator = [f['loading'] for f in pending if f['t'] > 100 and f['loading'] and f['loading']['display'] != 'none']
        check(indicator and all(s['now'] is None and s['indeterminate'] and s['label'] == '切换图片加载进度' for s in indicator),
              f"{mode}: pending image indicator did not expose indeterminate progress: {indicator[:2]}")
        check(page.evaluate("app().viewerIndex===1 && app().viewer.pair.pair_index===2 && !app().viewerPendingEntry"), f"{mode}: next index not committed")
        page.wait_for_function("app().viewerPreviewRetired && !app().viewerHandoff", timeout=2500)
        page.wait_for_timeout(120)
        page.evaluate("app().showPreviousPair()")
        page.wait_for_function("app().viewer.pair.pair_index===1")
        page.wait_for_timeout(350)
        warm = image_pixel(page, '.viewer-media.stage', 'viewer-'+mode+'-warm-previous')
        check(warm[0] > warm[2], f"{mode}: warm previous failed")
        return {"before":before,"waiting":result['waiting'],"after":result['after'],"pending_frames":len(pending),
                "compositor_frames":len(result['pixels']),"warm_previous":warm}
    finally:
        page.close()


def viewer_cold_previous_case(browser, base, width, height, reduced):
    mode = f"{width}-{'reduced' if reduced else 'normal'}"
    page = fresh(browser, base, width, height, reduced)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'fixture'},pair:{pair_index:2,
          image:{url:'/media/next-first-original.svg',small_thumb_url:'/media/next-first-thumb.svg'}}}];a.viewerIndex=0;
          a.openViewerEntry(a.viewerSequence[0]);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.wait_for_timeout(500)
        page.evaluate("""() => {const a=app();a.viewerSequence.unshift({folder:{folder_name:'fixture'},pair:{pair_index:1,
          image:{url:'/media/slow-previous-original.svg',small_thumb_url:'/media/slow-previous-thumb.svg'}}});a.viewerIndex=1;}""")
        result = capture(page, '.viewer-media.stage', 'viewer-cold-previous-'+mode,
                         lambda: page.evaluate("() => {app().showPreviousPair();}"), 1900)
        check(result['waiting'][2] > result['waiting'][0] and result['after'][0] > result['after'][2], f"{mode}: cold previous did not retain then replace frame")
        check(page.evaluate("app().viewerIndex===0 && app().viewer.pair.pair_index===1 && !app().viewerPendingEntry"), f"{mode}: previous index not committed")
        return {"waiting":result['waiting'],"after":result['after'],"frames":len(result['pixels'])}
    finally:
        page.close()


def viewer_original_reveal_case(browser, base, width, height, reduced):
    mode = f"{width}-{'reduced' if reduced else 'normal'}"
    page = fresh(browser, base, width, height, reduced)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'original-reveal'},pair:{pair_index:1,
          image:{url:'/media/slow-next-original-reveal.svg',small_thumb_url:'/media/original-reveal-thumb.svg'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0]);}""")
        page.wait_for_function("app().viewer.open && document.querySelector('.viewer-stage-preview')?.dataset.previewReady==='1' && !app().viewerImageReady")
        page.wait_for_timeout(350)
        result = capture(page, '.viewer-media.stage', 'viewer-original-reveal-'+mode, lambda: None, 1350)
        check(result['waiting'][0] > result['waiting'][2] and result['after'][2] > result['after'][0], f"{mode}: original reveal did not replace thumb")
        blends = [s for s in result['pixels'] if 70 < s['pixel'][0] < 205 and 100 < s['pixel'][2] < 200]
        if not reduced:
            check(blends, f"{mode}: no observable thumb-to-original overlap frames")
        page.wait_for_function("app().viewerPreviewRetired && !app().viewerHandoff")
        return {"waiting":result['waiting'],"after":result['after'],"blend_frames":len(blends),"frames":len(result['pixels'])}
    finally:
        page.close()


def viewer_gesture_case(browser, base, width, height, gesture, reduced=False, wide=False):
    name = f"viewer-gesture-{gesture}-{width}" + ('-reduced' if reduced else '') + ('-wide' if wide else '')
    page = fresh(browser, base, width, height, reduced=reduced, touch=gesture == 'touch')
    try:
        initial_viewer(page)
        page.evaluate("""([thumb,original]) => {app().viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          image:{url:original,small_thumb_url:thumb}}});}""", [
            '/media/slow-next-wide-gesture-thumb.svg' if wide else '/media/slow-next-gesture-thumb.svg',
            '/media/slow-next-wide-gesture-original.svg' if wide else '/media/slow-next-gesture-original.svg'])
        x, y = width / 2, height * .65
        page.mouse.move(x, y)
        def perform():
            if gesture == 'pointer':
                page.mouse.down()
                page.mouse.move(x, y - 160, steps=5)
                page.mouse.up()
            elif gesture == 'touch':
                input_session = page.context.new_cdp_session(page)
                input_session.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[{'x':x,'y':y,'id':1}]})
                for step in range(1, 6):
                    input_session.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[{'x':x,'y':y-32*step,'id':1}]})
                input_session.send('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
                input_session.detach()
            else:
                page.mouse.wheel(0, 160)
        # Sample the stage center: the surrounding letterbox may naturally appear while dragging.
        result = capture(page, '.viewer-media.stage', name, perform, 1900)
        pending = [f for f in result['frames'] if f['pending'] == 2]
        check(pending and all(f['pair'] == 1 for f in pending), f"{name}: old entry not retained during pending navigation")
        check(page.evaluate("app().viewer.pair.pair_index===2"), f"{name}: gesture did not navigate")
        check(result['after'][2] > result['after'][0], f"{name}: new image did not appear")
        return {"waiting":result['waiting'],"after":result['after'],"pending_frames":len(pending),"frames":len(result['pixels'])}
    finally:
        page.close()


def viewer_gesture_edges_case(browser, base):
    def drag(page):
        page.mouse.move(720, 585)
        page.mouse.down()
        page.mouse.move(720, 425, steps=5)
        page.mouse.up()
    def add_next(page, thumb, original):
        page.evaluate("""([thumb,original]) => {app().viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          image:{url:original,small_thumb_url:thumb}}});}""", [thumb,original])
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        add_next(page, '/media/slow-fail-gesture-thumb.svg', '/media/slow-fail-gesture-original.svg')
        drag(page)
        page.wait_for_function("app().toast.open && !app().viewerPendingEntry", timeout=5000)
        page.wait_for_timeout(250)
        state = page.evaluate("({pair:app().viewer.pair?.pair_index,index:app().viewerIndex,offset:app().viewerOffsetY,dragging:app().viewerVerticalDragging,swipe:!!app().viewerSwipeFinish})")
        check(state == {'pair':1,'index':0,'offset':0,'dragging':False,'swipe':False}, f"failed gesture did not restore old image and position: {state}")
        pixel = image_pixel(page, '.viewer-media.stage', 'viewer-gesture-failure')
        check(pixel[0] > pixel[2], f"failed gesture lost old image: {pixel}")
    finally:
        page.close()
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        add_next(page, '/media/slow-next-superseded-thumb.svg', '/media/slow-next-superseded-original.svg')
        drag(page)
        page.wait_for_function("app().viewerPendingEntry?.pair?.pair_index===2", timeout=3000)
        page.keyboard.press('ArrowUp')
        page.wait_for_timeout(1400)
        superseded = page.evaluate("({pair:app().viewer.pair?.pair_index,index:app().viewerIndex,offset:app().viewerOffsetY,pending:app().viewerPendingEntry})")
        check(superseded == {'pair':1,'index':0,'offset':0,'pending':None}, f"gesture target was not superseded by previous command: {superseded}")
    finally:
        page.close()
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        add_next(page, '/media/slow-next-close-gesture-thumb.svg', '/media/slow-next-close-gesture-original.svg')
        drag(page)
        page.wait_for_function("app().viewerPendingEntry?.pair?.pair_index===2", timeout=3000)
        page.keyboard.press('Escape')
        page.wait_for_timeout(1400)
        check(page.evaluate("!app().viewer.open && !app().viewerPendingEntry"), "Escape allowed delayed gesture target to reopen viewer")
        return {"failed_target_restored":state,"superseded":superseded,"escape_cancelled":True}
    finally:
        page.close()


def viewer_cancel_failure_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push(
          {folder:{folder_name:'fixture'},pair:{pair_index:2,image:{url:'/media/slow-next-original.svg',small_thumb_url:'/media/slow-next-thumb.svg'}}},
          {folder:{folder_name:'fixture'},pair:{pair_index:3,image:{url:'/media/third-original.svg',small_thumb_url:'/media/third-thumb.svg'}}});
          a.showNextPair();}""")
        page.wait_for_timeout(120)
        page.evaluate("app().showNextPair()")
        page.wait_for_function("app().viewer.pair.pair_index === 3")
        page.wait_for_timeout(1100)
        current = page.evaluate("({pair:app().viewer.pair.pair_index,index:app().viewerIndex,pending:app().viewerPendingEntry})")
        check(current['pair'] == 3 and current['index'] == 2 and current['pending'] is None, f"stale rapid navigation committed: {current}")
        pixel = image_pixel(page, '.viewer-media.stage', 'viewer-rapid-final')
        check(pixel[2] > pixel[0], f"rapid latest frame missing: {pixel}")
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:4,
          image:{url:'/media/slow-fail-original.svg',small_thumb_url:'/media/slow-fail-thumb.svg'}}});a.showNextPair();}""")
        page.wait_for_timeout(2100)
        state = page.evaluate("({pair:app().viewer.pair.pair_index,index:app().viewerIndex,pending:app().viewerPendingEntry,toast:app().toast.title})")
        check(state['pair'] == 3 and state['index'] == 2 and state['pending'] is None and state['toast'] == '图片加载失败', f"failed preview/original was not retained/reported: {state}")
        page.evaluate("""() => {const a=app();a.viewerSequence[3].pair.image={url:'/media/slow-next-close-original.svg',small_thumb_url:'/media/slow-next-close-thumb.svg'};a.showNextPair();}""")
        page.wait_for_timeout(120)
        page.evaluate("app().closeViewer(true)")
        page.wait_for_timeout(1100)
        closed = page.evaluate("({open:app().viewer.open,pending:app().viewerPendingEntry})")
        check(not closed['open'] and closed['pending'] is None, f"pending image reopened closed viewer: {closed}")
        return {"rapid_final":current,"failed_final":state,"closed":closed}
    finally:
        page.close()


def viewer_fallback_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          image:{url:'/media/next-fallback-original.svg',small_thumb_url:'/media/slow-fail-thumb.svg'}}});a.showNextPair();}""")
        page.wait_for_timeout(250)
        waiting = image_pixel(page, '.viewer-media.stage', 'viewer-fallback-waiting')
        check(waiting[0] > waiting[2], f"thumbnail failure discarded displayed image: {waiting}")
        page.wait_for_function("app().viewer.pair.pair_index===2 && app().viewerImageReady", timeout=4000)
        page.wait_for_timeout(400)
        fallback = image_pixel(page, '.viewer-media.stage', 'viewer-fallback-original')
        check(fallback[2] > fallback[0], f"original fallback not displayed: {fallback}")
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:3,
          image:{url:'/media/fail-original.svg',small_thumb_url:'/media/next-good-thumb.svg'}}});a.showNextPair();}""")
        page.wait_for_function("app().viewer.pair.pair_index===3 && !app().viewerImageLoading", timeout=4000)
        page.wait_for_timeout(400)
        preview = image_pixel(page, '.viewer-media.stage', 'viewer-fallback-preview')
        check(preview[2] > preview[0], f"failed original removed usable preview: {preview}")
        return {"waiting":waiting,"fallback_original":fallback,"failed_original_preview":preview}
    finally:
        page.close()


def viewer_video_only_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {app().viewerSequence.push({folder:{folder_name:'fixture'},
          pair:{pair_index:2,livephoto:{url:'/media/slow-video-only.mp4'}}});}""")
        result = capture(page, '.viewer-media.stage', 'viewer-video-only',
                         lambda: page.evaluate("() => {app().showNextPair();}"), 2000)
        check(result['waiting'][0] > result['waiting'][2], f"no-cover video-only destination discarded previous still: {result['waiting']}")
        check(result['after'][2] > result['after'][0], f"video-only destination did not play: {result['after']}")
        return {"waiting":result['waiting'],"after":result['after'],"frames":len(result['pixels'])}
    finally:
        page.close()


def viewer_video_url_preview_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},
          pair:{pair_index:2,preview_url:'/media/slow-video-preview.mp4',livephoto:{url:'/media/slow-video-preview.mp4'}}});a.showNextPair();}""")
        page.wait_for_function("app().viewer.pair?.pair_index===2 && app().viewer.showVideo", timeout=5000)
        pixel = image_pixel(page, '.viewer-media.stage', 'viewer-video-preview')
        check(pixel[2] > pixel[0], f"video URL preview did not play: {pixel}")
        return {"pixel":pixel}
    finally:
        page.close()


def viewer_video_only_edge_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push(
          {folder:{folder_name:'fixture'},pair:{pair_index:2,livephoto:{url:'/media/slow-video-cancel.mp4'}}},
          {folder:{folder_name:'fixture'},pair:{pair_index:3,image:{url:'/media/third-original.svg',small_thumb_url:'/media/third-thumb.svg'}}});
          a.showNextPair();}""")
        page.wait_for_timeout(100)
        page.evaluate("() => {app().showNextPair();}")
        page.wait_for_function("app().viewer.pair?.pair_index===3", timeout=5000)
        page.wait_for_timeout(1400)
        check(page.evaluate("app().viewer.pair?.pair_index===3 && app().viewerIndex===2"), "cancelled video-only target committed after later navigation")
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:4,
          livephoto:{url:'/media/slow-fail-video-only.mp4'}}});a.showNextPair();}""")
        page.wait_for_timeout(1800)
        state = page.evaluate("({pair:app().viewer.pair?.pair_index,index:app().viewerIndex,pending:app().viewerPendingEntry,toast:app().toast.title})")
        check(state['pair']==3 and state['index']==2 and state['pending'] is None and state['toast'], f"failed video-only target did not retain/report old image: {state}")
        page.evaluate("""() => {const a=app();a.viewerSequence[3].pair.livephoto={url:'/media/slow-video-close.mp4'};a.showNextPair();}""")
        page.wait_for_timeout(100)
        page.evaluate("app().closeViewer(true)")
        page.wait_for_timeout(1400)
        check(page.evaluate("!app().viewer.open && !app().viewerPendingEntry"), "cancelled video-only request reopened viewer")
        return {"rapid_target_preserved":True,"failed_target":state,"close_cancelled":True}
    finally:
        page.close()


def viewer_video_only_bad_cover_case(browser, base):
    page = fresh(browser, base)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          livephoto:{url:'/media/video-cover-fallback.mp4',cover_url:'/media/slow-fail-poster.svg'}}});a.showNextPair();}""")
        page.wait_for_function("app().viewer.pair?.pair_index===2 && app().viewer.showVideo", timeout=5000)
        return {"failed_cover_fell_back_to_video":True}
    finally:
        page.close()


def viewer_video_only_playback_case(browser, base, reduced):
    mode = 'reduced' if reduced else 'normal'
    page = fresh(browser, base, reduced=reduced)
    try:
        initial_viewer(page)
        page.evaluate("""() => {const a=app();a.viewerSequence.push({folder:{folder_name:'fixture'},pair:{pair_index:2,
          livephoto:{url:'/media/video-only-playback.mp4'}}});a.showNextPair();}""")
        page.wait_for_function("app().viewer.pair?.pair_index===2 && app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        page.wait_for_timeout(350)
        paused = capture(page, '.viewer-media.stage', 'viewer-video-only-pause-'+mode,
                         lambda: page.evaluate("app().toggleViewerPlayback()"), 700)
        check(paused['after'][2] > paused['after'][0] and page.evaluate("!app().viewer.showVideo && document.querySelector('.viewer-video').paused"),
              f"{mode}: video-only pause lost its frame: {paused['after']}")
        replay = capture(page, '.viewer-media.stage', 'viewer-video-only-replay-'+mode,
                         lambda: page.evaluate("app().toggleViewerPlayback()"), 700)
        check(replay['after'][2] > replay['after'][0] and page.evaluate("app().viewer.showVideo && !document.querySelector('.viewer-video').paused"),
              f"{mode}: video-only replay failed: {replay['after']}")
        return {"pause":paused['after'],"replay":replay['after'],"pause_frames":len(paused['pixels']),"replay_frames":len(replay['pixels'])}
    finally:
        page.close()


def virtual_case(browser, base, early):
    page = fresh(browser, base)
    try:
        page.evaluate("""() => {const a=app();a.detail={open:true,folder:{folder_name:'virtual',title:'Virtual scrolling'},videos:[],
          pairs:a.normalizeDetailPairs(Array.from({length:160},(_,i)=>({pair_index:i+1,image:{small_thumb_url:`/media/d${i}.svg`}})))};
          a.resetDetailWindow();a.syncBodyLock();}""")
        page.wait_for_function("document.querySelector('.detail-card')")
        first_appearance = None
        if not early:
            page.wait_for_timeout(800)
        else:
            page.wait_for_function("document.querySelector('.detail-card')?.classList.contains('is-appearing')")
            first_appearance = page.evaluate("""() => {const e=document.querySelector('.detail-card');return {
              appearing:e.classList.contains('is-appearing'),opacity:Number(getComputedStyle(e).opacity),
              animations:e.getAnimations().map(a=>a.animationName)};}""")
            check('card-fade-in' in first_appearance['animations'], f"early test did not unmount during first entrance: {first_appearance}")
        page.evaluate("""() => {window.firstCard=document.querySelector('.detail-card');
          const a=app();a.$refs.detailShell.scrollTop=6000;a.shiftDetailWindowIfNeeded();}""")
        page.wait_for_timeout(450)
        away = page.evaluate("({start:app().detailRenderStart,end:app().detailRenderEnd,disconnected:!firstCard.isConnected})")
        check(away['start'] > 0 and away['disconnected'], f"virtual window did not unmount first card: {away}")
        page.evaluate("""() => {startFrames('.detail-card',800);const a=app();a.$refs.detailShell.scrollTop=0;a.shiftDetailWindowIfNeeded();}""")
        page.wait_for_timeout(900)
        frames = page.evaluate('window.frames')
        cards = [f['element'] for f in frames if f['element'] and f['element']['top'] < 900]
        check(cards and min(c['opacity'] for c in cards) >= .999, f"virtual return replayed appearance: min {min(c['opacity'] for c in cards)}")
        check(page.evaluate("app().detailRenderStart===0 && firstCard!==document.querySelector('.detail-card')"), "virtual return did not remount first card")
        return {"early_unmount":early,"first_appearance":first_appearance,"away":away,"minimum_card_opacity":min(c['opacity'] for c in cards),"animations":sorted({a for c in cards for a in c['animations']})}
    finally:
        page.close()


def live_viewer_case(browser, base, reduced):
    mode = 'reduced' if reduced else 'normal'
    page = fresh(browser, base, reduced=reduced)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},
          livephoto:{url:'/media/slow-video.mp4',reverse_url:'/media/next-reverse.mp4'}}}];a.viewerIndex=0;
          a.openViewerEntry(a.viewerSequence[0],false,false);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.wait_for_timeout(500)
        poster = page.locator('.viewer-video').get_attribute('poster')
        check(poster == '/media/first-thumb.svg', f"missing video cover did not fall back to still image: {poster}")
        result = capture(page, '.viewer-media.stage', 'live-viewer-'+mode,
                         lambda: page.evaluate("() => {app().scheduleViewerPlaybackStart(0);}"), 1800)
        check(result['waiting'][0] > result['waiting'][2] and result['near_end'][0] > result['near_end'][2], f"static image disappeared while video loading: {result['waiting']}, {result['near_end']}")
        check(result['after'][2] > result['after'][0], f"ready video not shown: {result['after']}")
        bad = [f for f in result['frames'] if f['showVideo'] and (not f['video'] or f['video']['ready'] < 2)]
        check(not bad, f"video exposed before first frame: {bad[:2]}")
        check(page.evaluate("app().viewerVisibleMedia()?.tagName === 'VIDEO'"), "visible media hit-testing did not prefer the playing video")
        page.wait_for_function("!app().viewer.showVideo", timeout=4500)
        page.wait_for_timeout(350)
        stopped = image_pixel(page, '.viewer-media.stage', 'live-viewer-'+mode+'-once-ended')
        check(stopped[0] > stopped[2], f"once playback did not return to still: {stopped}")
        page.evaluate("app().replayViewerOnce()")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused")
        page.evaluate("""() => {const a=app();a.playbackMode='pause';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_timeout(350)
        check(page.evaluate("!app().viewer.showVideo && document.querySelector('.viewer-video').paused"), "pause did not retain still")
        page.evaluate("""() => {const a=app();a.playbackMode='loop';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused")
        page.wait_for_timeout(2300)
        check(page.evaluate("app().viewer.showVideo && document.querySelector('.viewer-video').loop && !document.querySelector('.viewer-video').paused"), "loop playback stopped")
        page.evaluate("""() => {const a=app();a.playbackMode='pingpong';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_function("document.querySelector('.viewer-video').getAttribute('src')?.endsWith('/media/next-reverse.mp4')", timeout=5000)
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        return {"poster":poster,"waiting":result['waiting'],"after":result['after'],"compositor_frames":len(result['pixels']),
                "early_visible_video_frames":len(bad),"once_ended":stopped,"once_replay":True,"pause":True,"loop":True,"pingpong_reverse":True}
    finally:
        page.close()


def live_fail_hover_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live-fail'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},livephoto:{url:'/media/slow-fail-video.mp4'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0],false,false);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.evaluate("app().scheduleViewerPlaybackStart(0)")
        page.wait_for_timeout(1500)
        failed = image_pixel(page, '.viewer-media.stage', 'live-viewer-failure')
        check(not page.evaluate('app().viewer.showVideo') and failed[0] > failed[2], f"failed live video lost still image: {failed}")
        page.evaluate("""() => {const a=app();a.closeViewer(true);a.hoverPreviewEnabled=true;
          a.detail={open:true,folder:{folder_name:'hover',title:'Hover'},videos:[],pairs:a.normalizeDetailPairs([
            {pair_index:1,image:{small_thumb_url:'/media/hover-static.svg'},livephoto:{url:'/media/slow-hover-video.mp4'}}])};
          a.resetDetailWindow();a.syncBodyLock();}""")
        page.wait_for_function("document.querySelector('.detail-card img')?.naturalWidth > 0")
        page.wait_for_timeout(500)
        result = capture(page, '.detail-card .detail-thumb', 'live-hover',
                         lambda: page.locator('.detail-card').hover(), 1900)
        check(result['waiting'][0] > result['waiting'][2] and result['near_end'][0] > result['near_end'][2], "hover exposed video before first frame")
        check(result['after'][2] > result['after'][0], f"hover did not play video: {result['after']}")
        page.mouse.move(0, 0)
        page.wait_for_timeout(350)
        stopped = image_pixel(page, '.detail-card .detail-thumb', 'live-hover-stopped')
        check(stopped[0] > stopped[2], f"hover did not restore still: {stopped}")
        # A stale rejection from a prior hover must not cancel a newer hover on the same card.
        page.evaluate("""() => {const a=app(),card=document.querySelector('.detail-card'),v=card.querySelector('video');
          window.nativeHoverPlay=v.play.bind(v);v.play=()=>new Promise((resolve,reject)=>window.rejectOldHover=reject);
          a.loadHoverPreview(card);}""")
        page.wait_for_function("typeof window.rejectOldHover === 'function'")
        page.evaluate("""() => {const a=app(),card=document.querySelector('.detail-card'),v=card.querySelector('video');
          a.stopHoverPreviewByCard(card);v.play=window.nativeHoverPlay;a.loadHoverPreview(card);}""")
        page.wait_for_function("!document.querySelector('.hover-preview-video').classList.contains('hidden') && !document.querySelector('.hover-preview-video').paused")
        page.evaluate("window.rejectOldHover(new Error('intentional stale hover rejection'))")
        page.wait_for_timeout(150)
        check(page.evaluate("!document.querySelector('.hover-preview-video').classList.contains('hidden') && !document.querySelector('.hover-preview-video').paused"), "stale hover rejection stopped newer preview")
        page.evaluate("app().stopHoverPreviewByCard(document.querySelector('.detail-card'))")
        page.evaluate("app().detail.pairs[0].livephoto.url='/media/slow-fail-hover-video.mp4'")
        page.locator('.detail-card').hover()
        page.wait_for_timeout(1800)
        failed_hover = image_pixel(page, '.detail-card .detail-thumb', 'live-hover-failure')
        check(failed_hover[0] > failed_hover[2] and page.evaluate("document.querySelector('.hover-preview-video').classList.contains('hidden')"), f"failed hover video lost still: {failed_hover}")
        return {"failed_video_pixel":failed,"hover_waiting":result['waiting'],"hover_after":result['after'],"hover_stopped":stopped,
                "failed_hover":failed_hover,"stale_hover_rejection_ignored":True,"hover_frames":len(result['pixels'])}
    finally:
        page.close()


def live_play_promise_race_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live-race'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},livephoto:{url:'/media/video-race.mp4'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0]);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        # Hold an earlier play promise, pause, then resolve it after the user has paused.
        page.evaluate("""() => {const a=app(),v=document.querySelector('.viewer-video');window.nativePlay=v.play.bind(v);
          v.play=()=>new Promise(resolve=>window.resolveStalePlay=resolve);a.playReadyViewerVideo(v);
          a.pauseCurrentViewerMedia(false);window.resolveStalePlay();v.play=window.nativePlay;}""")
        page.wait_for_timeout(200)
        check(page.evaluate("!app().viewer.showVideo && document.querySelector('.viewer-video').paused"), "late play resolution undid pause")
        # Hold a failing older request, restart the same real video, then reject the old request.
        page.evaluate("""() => {const a=app(),v=document.querySelector('.viewer-video');
          v.play=()=>new Promise((resolve,reject)=>window.rejectStalePlay=reject);a.playReadyViewerVideo(v);
          a.pauseCurrentViewerMedia(false);v.play=window.nativePlay;a.applyPlaybackMode(v,true);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        page.evaluate("window.rejectStalePlay(new Error('intentional stale play rejection'))")
        page.wait_for_timeout(250)
        check(page.evaluate("app().viewer.showVideo && !document.querySelector('.viewer-video').paused"), "old play rejection cancelled newer replay")
        return {"late_success_after_pause":True,"late_failure_after_replay":True}
    finally:
        page.close()


def live_pingpong_transition_case(browser, base, reduced=False):
    page = fresh(browser, base, reduced=reduced)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live-transition'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},
          livephoto:{url:'/media/video-forward.mp4',reverse_url:'/media/slow-next-reverse.mp4'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0],false,false);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.evaluate("""() => {const a=app();a.playbackMode='pingpong';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        page.wait_for_timeout(300)
        suffix = '-reduced' if reduced else ''
        before = image_pixel(page, '.viewer-media.stage', 'live-pingpong-before'+suffix)
        check(before[2] > before[0], f"forward video is not visible: {before}")
        result = capture(page, '.viewer-media.stage', 'live-pingpong-reverse'+suffix,
                         lambda: page.evaluate("document.querySelector('.viewer-video').currentTime = document.querySelector('.viewer-video').duration - .15"), 3200)
        red = [s for s in result['pixels'] if s['pixel'][0] > s['pixel'][2]+25]
        check(not red and result['waiting'][2] > result['waiting'][0] and result['near_end'][2] > result['near_end'][0],
              f"reverse source exposed static still during playback: {len(red)} red compositor frames; waiting={result['waiting']}, near={result['near_end']}")
        check(page.evaluate("document.querySelector('.viewer-video').getAttribute('src')?.endsWith('/media/slow-next-reverse.mp4') && app().viewer.showVideo"), "reverse video did not start")
        return {"before":before,"waiting":result['waiting'],"near":result['near_end'],"after":result['after'],"red_flash_frames":len(red),"frames":len(result['pixels'])}
    finally:
        page.close()


def live_restart_transition_case(browser, base, reduced=False):
    page = fresh(browser, base, reduced=reduced)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live-restart'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},livephoto:{url:'/media/slow-video-restart.mp4'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0],false,false);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.evaluate("""() => {const a=app();a.playbackMode='loop';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        page.wait_for_timeout(350)
        result = capture(page, '.viewer-media.stage', 'live-mode-restart'+('-reduced' if reduced else ''),
                         lambda: page.evaluate("app().cyclePlayback()"), 1800)
        red = [s for s in result['pixels'] if s['pixel'][0] > s['pixel'][2]+25]
        check(not red and result['waiting'][2] > result['waiting'][0] and result['after'][2] > result['after'][0],
              f"switching playback mode exposed static still: {len(red)} red frames; waiting={result['waiting']}, after={result['after']}")
        return {"waiting":result['waiting'],"after":result['after'],"red_flash_frames":len(red),"frames":len(result['pixels'])}
    finally:
        page.close()


def live_frame_hit_test_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("""() => {const a=app();a.viewerSequence=[{folder:{folder_name:'live-hit'},pair:{pair_index:1,
          image:{url:'/media/first-original.svg',small_thumb_url:'/media/first-thumb.svg'},
          livephoto:{url:'/media/video-hit-forward.mp4',reverse_url:'/media/slow-reverse-hit.mp4'}}}];
          a.viewerIndex=0;a.openViewerEntry(a.viewerSequence[0],false,false);}""")
        page.wait_for_function("app().viewer.open && app().viewerImageReady")
        page.evaluate("""() => {const a=app();a.playbackMode='pingpong';a.applyPlaybackMode(document.querySelector('.viewer-video'),true);}""")
        page.wait_for_function("app().viewer.showVideo && !document.querySelector('.viewer-video').paused", timeout=5000)
        page.wait_for_function("app().viewerVideoFrameReady && !app().viewer.showVideo && document.querySelector('.viewer-video').getAttribute('src')?.endsWith('/media/slow-reverse-hit.mp4')", timeout=5000)
        dimensions = page.evaluate("""() => {const a=app(),s=document.querySelector('.viewer-media.stage'),m=a.viewerVisibleMedia(),r=a.viewerMediaDisplayRect(s,m);
          return {tag:m?.tagName,width:m?.width,height:m?.height,left:r.left,right:r.right};}""")
        check(dimensions['tag'] == 'CANVAS' and dimensions['width'] == 240 and dimensions['height'] == 320,
              f"did not inspect the held video canvas: {dimensions}")
        page.mouse.click(80, 450)
        page.wait_for_timeout(400)
        check(not page.evaluate('app().viewer.open'), f"click on letterbox did not close viewer while video frame held: {dimensions}")
        return {"canvas_rect":dimensions,"letterbox_click_closed":True}
    finally:
        page.close()


def initial_pending_cancel_case(browser, base):
    page = fresh(browser, base)
    try:
        page.evaluate("""() => {const a=app();a.detail={open:true,folder:{folder_name:'pending'},pairs:[],videos:[]};
          a.viewerSource='detail';a.viewerSequence=[{folder:{folder_name:'pending'},pair:{pair_index:1,
            image:{url:'/media/slow-pending-original.svg',small_thumb_url:'/media/slow-pending-thumb.svg'}}}];
          a.openViewerEntry(a.viewerSequence[0]);}""")
        page.wait_for_timeout(100)
        check(page.evaluate("!!app().viewerPendingEntry && !app().viewer.open"), "initial entry was not pending")
        page.keyboard.press('Escape')
        page.wait_for_timeout(1000)
        state = page.evaluate("({open:app().viewer.open,pending:app().viewerPendingEntry,detail:app().detail.open})")
        check(not state['open'] and state['pending'] is None and state['detail'], f"Escape did not cancel initial pending viewer while retaining detail: {state}")
        page.evaluate("""() => {const a=app();a.viewerSequence[0].pair.image={url:'/media/slow-pending-two-original.svg',small_thumb_url:'/media/slow-pending-two-thumb.svg'};
          a.openViewerEntry(a.viewerSequence[0]);}""")
        page.wait_for_timeout(100)
        page.evaluate("app().closeDetail(true)")
        page.wait_for_timeout(1000)
        closed = page.evaluate("({open:app().viewer.open,pending:app().viewerPendingEntry,detail:app().detail.open})")
        check(not closed['open'] and closed['pending'] is None and not closed['detail'], f"closing detail did not cancel pending viewer: {closed}")
        return {"escape":state,"detail_closed":closed}
    finally:
        page.close()


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='', help='run case names containing this text')
    args = parser.parse_args()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x36a5da:s=240x320:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(OUT / "sample.mp4")], check=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        def case(name, fn):
            if args.only and not any(text in name for text in args.only.split(',')):
                return
            print("RUN", name, flush=True)
            try:
                detail = fn()
                results.append({"case":name,"pass":True,"detail":detail})
                print("PASS", name, json.dumps(detail, ensure_ascii=False), flush=True)
            except Exception as exc:
                results.append({"case":name,"pass":False,"error":str(exc),"traceback":traceback.format_exc()})
                print("FAIL", name, str(exc), flush=True)
        for width,height in ((1440,900),(390,844)):
            for reduced in (False,True):
                case(f"gallery-{width}-{'reduced' if reduced else 'normal'}", lambda w=width,h=height,r=reduced: gallery_case(browser,base,w,h,r))
        case("recent-duplicate-icon-qr", lambda: regular_images_case(browser,base))
        case("failed-and-cancelled-replacements", lambda: replacement_failure_case(browser,base))
        case("hidden-lazy-resources", lambda: hidden_lazy_case(browser,base))
        for width,height in ((1440,900),(390,844)):
            for reduced in (False,True):
                case(f"viewer-{width}-{'reduced' if reduced else 'normal'}", lambda w=width,h=height,r=reduced: viewer_case(browser,base,w,h,r))
        case("viewer-cold-previous-1440-normal", lambda: viewer_cold_previous_case(browser,base,1440,900,False))
        case("viewer-cold-previous-390-reduced", lambda: viewer_cold_previous_case(browser,base,390,844,True))
        case("viewer-original-reveal-1440-normal", lambda: viewer_original_reveal_case(browser,base,1440,900,False))
        case("viewer-original-reveal-390-reduced", lambda: viewer_original_reveal_case(browser,base,390,844,True))
        case("viewer-gesture-pointer-1440", lambda: viewer_gesture_case(browser,base,1440,900,'pointer'))
        case("viewer-gesture-pointer-390", lambda: viewer_gesture_case(browser,base,390,844,'pointer'))
        case("viewer-gesture-touch-390", lambda: viewer_gesture_case(browser,base,390,844,'touch'))
        case("viewer-gesture-touch-390-wide", lambda: viewer_gesture_case(browser,base,390,844,'touch',False,True))
        case("viewer-gesture-wheel-1440", lambda: viewer_gesture_case(browser,base,1440,900,'wheel'))
        case("viewer-gesture-pointer-390-reduced", lambda: viewer_gesture_case(browser,base,390,844,'pointer',True))
        case("viewer-gesture-wheel-1440-reduced", lambda: viewer_gesture_case(browser,base,1440,900,'wheel',True))
        case("viewer-gesture-edges", lambda: viewer_gesture_edges_case(browser,base))
        case("viewer-rapid-failure-close", lambda: viewer_cancel_failure_case(browser,base))
        case("viewer-fallback", lambda: viewer_fallback_case(browser,base))
        case("viewer-video-only", lambda: viewer_video_only_case(browser,base))
        case("viewer-video-url-preview", lambda: viewer_video_url_preview_case(browser,base))
        case("viewer-video-only-edges", lambda: viewer_video_only_edge_case(browser,base))
        case("viewer-video-only-bad-cover", lambda: viewer_video_only_bad_cover_case(browser,base))
        case("viewer-video-only-playback-normal", lambda: viewer_video_only_playback_case(browser,base,False))
        case("viewer-video-only-playback-reduced", lambda: viewer_video_only_playback_case(browser,base,True))
        case("virtual-return", lambda: virtual_case(browser,base,False))
        case("virtual-early-unmount-return", lambda: virtual_case(browser,base,True))
        case("live-viewer-normal", lambda: live_viewer_case(browser,base,False))
        case("live-viewer-reduced", lambda: live_viewer_case(browser,base,True))
        case("live-fail-hover", lambda: live_fail_hover_case(browser,base))
        case("live-play-promise-race", lambda: live_play_promise_race_case(browser,base))
        case("live-pingpong-transition", lambda: live_pingpong_transition_case(browser,base))
        case("live-pingpong-transition-reduced", lambda: live_pingpong_transition_case(browser,base,True))
        case("live-restart-transition", lambda: live_restart_transition_case(browser,base))
        case("live-restart-transition-reduced", lambda: live_restart_transition_case(browser,base,True))
        case("live-frame-hit-test", lambda: live_frame_hit_test_case(browser,base))
        case("initial-pending-cancel", lambda: initial_pending_cancel_case(browser,base))
        browser.close()
    server.shutdown()
    summary = {"results":results,"browser_errors":ERRORS,"requests":REQUESTS}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed":sum(r['pass'] for r in results),"failed":sum(not r['pass'] for r in results),"browser_errors":ERRORS}, ensure_ascii=False), flush=True)
    if ERRORS or any(not r["pass"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    run()
