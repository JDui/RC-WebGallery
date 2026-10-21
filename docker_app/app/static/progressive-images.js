(function (root) {
  "use strict";
  function createManager(env) {
    const dirty = new Map();
    const active = new Set();
    let idle = null, sweepTimer = null;
    const clearImage = (image) => {
      image.onload = image.onerror = null;
      image.removeAttribute("src");
      image.removeAttribute("srcset");
    };
    function flush() {
      if (idle !== null) env.cancelIdleCallback?.(idle);
      if (sweepTimer !== null) env.clearTimeout(sweepTimer);
      idle = sweepTimer = null;
      for (const [node, dispose] of dirty) {
        dirty.delete(node);
        dispose();
        node.removeAttribute("data-image-dirty");
      }
    }
    function retire(node, dispose = () => clearImage(node)) {
      node.setAttribute("data-image-dirty", "1");
      dirty.set(node, dispose);
      if (sweepTimer === null) {
        sweepTimer = env.setTimeout(flush, 80);
        if (env.requestIdleCallback) idle = env.requestIdleCallback(flush, {timeout: 80});
      }
    }
    const waiting = [];
    const seenUrls = new Map();
    let inflight = 0;
    function pump() {
      while (inflight < 6 && waiting.length) {
        const job = waiting.shift();
        if (!job.cancelled) job.start();
      }
    }
    function remember(url) {
      seenUrls.delete(url);
      seenUrls.set(url, true);
      if (seenUrls.size > 512) seenUrls.delete(seenUrls.keys().next().value);
    }
    function preload(url) {
      const image = new env.Image();
      let resolve, settled = false, started = false, timer = null;
      const promise = new Promise((done) => { resolve = done; });
      const finish = (ok) => {
        if (settled) return;
        settled = true;
        if (timer !== null) env.clearTimeout(timer);
        job.cancelled = true;
        if (started) inflight--;
        else {
          const index = waiting.indexOf(job);
          if (index >= 0) waiting.splice(index, 1);
        }
        image.onload = image.onerror = null;
        resolve(ok ? image : null);
        if (!ok) retire(image);
        pump();
      };
      const job = {cancelled: false, start() {
        started = true;
        inflight++;
        timer = env.setTimeout(() => finish(false), 8000);
        image.decoding = "async";
        image.onload = async () => {
          try { if (image.decode) await image.decode(); finish(image.naturalWidth > 0); }
          catch (_) { finish(false); }
        };
        image.onerror = () => finish(false);
        image.src = url;
        if (image.complete && image.naturalWidth > 0) image.onload();
      }};
      waiting.push(job);
      pump();
      return {promise, cancel: () => finish(false)};
    }
    const controllers = new Map();
    const exposure = env.IntersectionObserver ? new env.IntersectionObserver(entries => {
      for (const entry of entries) controllers.get(entry.target)?.setVisible(entry.isIntersecting);
    }, {rootMargin: "100% 0px", threshold: 0}) : null;
    function attach(image) {
      let latest = "", committed = "", failed = "", epoch = 0, disposed = false, suspended = false, committing = false;
      let loading = false, pending = null, old = null, animation = null, finishTimer = null;
      let visible = true, pageHidden = false;
      image.setAttribute("data-progressive-image", "1");
      function finishFade() {
        if (finishTimer !== null) env.clearTimeout(finishTimer);
        finishTimer = null;
        committing = false;
        if (animation) {
          animation.onfinish = animation.oncancel = null;
          animation.cancel();
          animation = null;
        }
        if (old) { old.remove(); retire(old); old = null; }
        image.style.removeProperty("opacity");
        image.style.removeProperty("will-change");
        image.removeAttribute("data-image-transition");
        if (!disposed && !suspended && !loading && latest !== committed) loadLatest();
      }
      async function loadLatest() {
        if (disposed || suspended || loading || animation || latest === committed || latest === failed || !latest) return;
        loading = true;
        const token = ++epoch;
        const url = latest;
        const task = pending = preload(url);
        const candidate = await task.promise;
        if (pending === task) pending = null;
        if (disposed || token !== epoch || suspended) { if (candidate) retire(candidate); return; }
        if (!candidate) { loading = false; failed = url; return; }
        const previous = committed;
        let snapshot = null;
        if (previous) {
          snapshot = new env.Image();
          snapshot.className = "progressive-image-old";
          snapshot.alt = "";
          snapshot.setAttribute("aria-hidden", "true");
          const visual = env.getComputedStyle(image);
          for (const key of ["objectFit", "objectPosition", "filter", "transform", "borderRadius"]) snapshot.style[key] = visual[key];
          snapshot.src = previous;
          try { if (snapshot.decode) await snapshot.decode(); }
          catch (_) {
            retire(snapshot); retire(candidate);
            if (!disposed && token === epoch) { loading = false; failed = url; }
            return;
          }
        }
        if (disposed || token !== epoch || suspended) {
          retire(candidate); if (snapshot) retire(snapshot); return;
        }
        committing = true;
        old = snapshot;
        if (old) image.before(old);
        image.style.opacity = "0";
        // Visibility and concurrency are managed above; native lazy loading can defer decode.
        image.loading = "eager";
        image.src = url;
        try { if (image.decode) await image.decode(); }
        catch (_) {
          retire(candidate);
          if (disposed || token !== epoch) return;
          if (previous) image.src = previous; else image.removeAttribute("src");
          failed = url;
          loading = false;
          finishFade();
          return;
        }
        retire(candidate);
        if (disposed || token !== epoch || suspended) return;
        committed = url;
        loading = false;
        image.dataset.progressiveReady = "1";
        image.style.removeProperty("opacity");
        const duration = seenUrls.has(url) || env.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 0 : 260;
        remember(url);
        if (!duration || !image.animate) { finishFade(); return; }
        image.dataset.imageTransition = "1";
        image.style.willChange = "opacity";
        animation = image.animate([{opacity: 0}, {opacity: 1}], {duration, easing: "ease-out"});
        animation.onfinish = finishFade;
        // 页面隐藏或动画事件丢失时也必须完成淡入收尾。
        finishTimer = env.setTimeout(finishFade, duration + 80);
      }
      const controller = {
        setSource(value) {
          const url = String(value || "");
          if (disposed || latest === url) return;
          latest = url;
          failed = "";
          if (!url || (!animation && !committing)) {
            ++epoch;
            pending?.cancel(); pending = null;
            loading = false;
            if (!url) {
              committed = "";
              finishFade();
              clearImage(image);
              delete image.dataset.progressiveReady;
            } else loadLatest();
          }
        },
        setVisible(value) {
          visible = value;
          controller.suspend(pageHidden, !visible);
        },
        suspend(value, release = !visible) {
          pageHidden = value;
          suspended = value || !visible;
          if (suspended) {
            ++epoch;
            pending?.cancel(); pending = null; loading = false;
            finishFade();
            if (release) {
              committed = "";
              clearImage(image);
              delete image.dataset.progressiveReady;
            }
          } else loadLatest();
        },
        dispose() {
          if (disposed) return;
          disposed = true;
          ++epoch;
          pending?.cancel(); pending = null;
          loading = false;
          finishFade();
          clearImage(image);
          image.removeAttribute("data-progressive-image");
          delete image.dataset.progressiveReady;
          exposure?.unobserve(image);
          controllers.delete(image);
          active.delete(controller);
        },
      };
      active.add(controller);
      controllers.set(image, controller);
      if (exposure && image.closest?.(".gallery-item-card, .detail-card")) {
        controller.setVisible(false);
        exposure.observe(image);
      }
      return controller;
    }
    function dispose() { for (const controller of active) controller.dispose(); exposure?.disconnect(); flush(); }
    async function warm(url) {
      const candidate = await preload(url).promise;
      if (candidate) retire(candidate);
      return !!candidate;
    }
    return {attach, retire, warm, flush, dispose, dirty, active};
  }
  if (typeof module !== "undefined" && module.exports) { module.exports = {createManager}; return; }
  const manager = createManager(root);
  root.galleryImageResources = {warm: manager.warm};
  document.addEventListener("alpine:init", () => {
    root.Alpine.directive("progressive-src", (element, {expression}, {evaluateLater, effect, cleanup}) => {
      const controller = manager.attach(element);
      const evaluate = evaluateLater(expression);
      effect(() => evaluate((url) => controller.setSource(url)));
      cleanup(() => controller.dispose());
    });
    root.Alpine.directive("retire-video", (element, _, {cleanup}) => {
      cleanup(() => {
        element.pause();
        element.onended = element.onerror = element.onloadeddata = element.oncanplay = null;
        element.removeAttribute("src");
        element.removeAttribute("poster");
        element.load();
      });
    });
    root.Alpine.directive("retire-image", (element, _, {cleanup}) => {
      cleanup(() => manager.retire(element));
    });
  });
  document.addEventListener("visibilitychange", () => {
    for (const controller of manager.active) controller.suspend(document.hidden);
    if (document.hidden) manager.flush();
  });
  root.addEventListener("pagehide", () => {
    for (const controller of manager.active) controller.suspend(true);
    manager.flush();
  });
  root.addEventListener("pageshow", () => {
    if (!document.hidden) for (const controller of manager.active) controller.suspend(false);
  });
})(typeof window === "undefined" ? globalThis : window);
