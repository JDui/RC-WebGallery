(function () {
  "use strict";

  const active = new Map();
  const owners = new WeakMap();
  const identityAttributes = ["data-gallery-key", "data-detail-key", "data-media-menu-folder", "data-media-menu-pair"];
  const identitySelector = identityAttributes.map(name => `[${name}]`).join(",");
  const easing = "cubic-bezier(.2,.78,.2,1)";
  const fullTransform = "translate3d(0px, 0px, 0) scale(1)";

  function rectOf(element) {
    if (!element || element.isConnected === false || typeof element.getBoundingClientRect !== "function") return null;
    const box = element.getBoundingClientRect();
    if (![box.left, box.top, box.width, box.height].every(Number.isFinite) || box.width <= 0 || box.height <= 0) return null;
    return { left: box.left, top: box.top, width: box.width, height: box.height,
      right: box.left + box.width, bottom: box.top + box.height };
  }

  function styleOf(element) {
    try { return window.getComputedStyle?.(element) || {}; } catch (_) { return {}; }
  }

  function visibleRect(element) {
    const box = rectOf(element);
    if (!box || (element.getClientRects && !element.getClientRects().length)) return null;
    const ownStyle = styleOf(element);
    if (ownStyle.display === "none" || ownStyle.visibility === "hidden" || ownStyle.visibility === "collapse") return null;
    const viewportWidth = Number(window.innerWidth || document.documentElement?.clientWidth) || 0;
    const viewportHeight = Number(window.innerHeight || document.documentElement?.clientHeight) || 0;
    if (!viewportWidth || !viewportHeight) return null;
    let left = Math.max(0, box.left), top = Math.max(0, box.top);
    let right = Math.min(viewportWidth, box.right);
    let bottom = Math.min(viewportHeight, box.bottom);
    // A thumbnail can be inside the viewport yet outside its scrolled detail pane.
    // Read clipping ancestors once per transition, never from an animation frame.
    for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const style = styleOf(parent);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return null;
      const clipX = /hidden|clip|scroll|auto/.test(style.overflowX || style.overflow || "");
      const clipY = /hidden|clip|scroll|auto/.test(style.overflowY || style.overflow || "");
      if (!clipX && !clipY) continue;
      const bounds = rectOf(parent);
      if (!bounds) return null;
      if (clipX) { left = Math.max(left, bounds.left); right = Math.min(right, bounds.right); }
      if (clipY) { top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom); }
    }
    if (right - left < 2 || bottom - top < 2) return null;
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }

  function fingerprint(element) {
    const anchor = element?.closest?.(identitySelector) || element;
    return identityAttributes.map(name => `${name}:${anchor?.getAttribute?.(name) ?? ""}`).join("|");
  }

  function capture(source, resolverOrOptions = {}) {
    const element = source?.currentTarget || source;
    const options = typeof resolverOrOptions === "function" ? { resolve: resolverOrOptions } : resolverOrOptions || {};
    const rect = visibleRect(element);
    let identity;
    try { identity = options.identity?.(element); } catch (_) { identity = false; }
    return { element, rect: identity === false ? null : rect, resolve: options.resolve,
      identity: options.identity, expectedIdentity: identity, fingerprint: fingerprint(element) };
  }

  function returnRect(origin) {
    if (!origin) return null;
    let element;
    try {
      element = typeof origin.resolve === "function" ? origin.resolve() : origin.element;
      if (!element) return null;
      if (origin.identity) {
        const current = origin.identity(element);
        if (current === false || (typeof origin.expectedIdentity === "string" && current !== origin.expectedIdentity)) return null;
      } else if (!origin.resolve && fingerprint(element) !== origin.fingerprint) {
        // Virtualized rows may retain their element while changing their item.
        return null;
      }
    } catch (_) { return null; }
    // Inert source layers remain valid return destinations below the active dialog.
    return visibleRect(element);
  }

  function originTransform(origin, destination, pivot) {
    // Uniform scale preserves media proportions throughout the transition.
    const scale = Math.max(.035, Math.min(1, origin.width / destination.width, origin.height / destination.height));
    const center = pivot || { x: destination.width / 2, y: destination.height / 2 };
    const x = origin.left + origin.width / 2 - destination.left - scale * destination.width / 2 - (1 - scale) * center.x;
    const y = origin.top + origin.height / 2 - destination.top - scale * destination.height / 2 - (1 - scale) * center.y;
    return `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) scale(${scale.toFixed(5)})`;
  }

  function stablePivot(key, destination, interrupted, previous) {
    if (key !== "detail" && key !== "task") return null;
    // These shells are top-aligned and grow when their payload arrives. A
    // percentage pivot would move the entire scaled shell as its height grows.
    const sampled = interrupted?.transformOrigin?.trim().split(/\s+/);
    if (sampled?.length >= 2 && sampled.slice(0, 2).every(value => /^-?[\d.]+px$/.test(value))) {
      const x = Number.parseFloat(sampled[0]), y = Number.parseFloat(sampled[1]);
      if (Number.isFinite(x) && Number.isFinite(y)) return { x, y };
    }
    return (interrupted && previous?.pivot) || { x: destination.width / 2, y: destination.height / 2 };
  }

  function preference(phase) {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    const low = document.documentElement?.dataset?.performanceMode === "low";
    return { spatial: !reduced && !low, duration: reduced ? (phase === "enter" ? 80 : 60)
      : low ? (phase === "enter" ? 140 : 120) : (phase === "enter" ? 300 : 240) };
  }

  function sample(element) {
    const style = styleOf(element);
    const opacity = Number.parseFloat(style.opacity);
    return { transform: style.transform && style.transform !== "none" ? style.transform : fullTransform,
      transformOrigin: style.transformOrigin,
      opacity: Number.isFinite(opacity) ? opacity : 1 };
  }

  function release(record) {
    if (record.released) return;
    record.released = true;
    if (record.timer !== null) window.clearTimeout(record.timer);
    if (record.releaseFrame !== null) {
      if (record.releaseWithRaf) window.cancelAnimationFrame?.(record.releaseFrame);
      else window.clearTimeout(record.releaseFrame);
    }
    record.animations.forEach(animation => {
      // A later animate() call may have thrown before Promise.all was installed.
      animation.finished?.catch?.(() => {});
      try { animation.cancel(); } catch (_) {}
    });
    [record.element, record.backdrop].forEach(element => {
      if (element && owners.get(element) === record) {
        element.removeAttribute?.("data-layer-motion-active");
        owners.delete(element);
      }
    });
    if (active.get(record.key) === record) active.delete(record.key);
  }

  function settle(record, completed) {
    if (record.settled) { if (!completed) release(record); return; }
    record.settled = true;
    if (record.timer !== null) { window.clearTimeout(record.timer); record.timer = null; }
    if (!completed || record.phase === "enter") {
      release(record);
      record.resolve(completed);
      return;
    }
    // Keep the exit's transparent final frame until Alpine has hidden the layer.
    // Promise continuations and Alpine's microtasks run before this next frame.
    record.resolve(true);
    const done = () => { record.releaseFrame = null; release(record); };
    record.releaseWithRaf = typeof window.requestAnimationFrame === "function";
    record.releaseFrame = record.releaseWithRaf ? window.requestAnimationFrame(done) : window.setTimeout(done, 0);
  }

  function cancel(key) {
    const record = active.get(key);
    if (record) settle(record, false);
  }

  function play(phase, options) {
    const { key, element, backdrop, origin } = options || {};
    const previous = active.get(key);
    const interrupted = previous && previous.element === element ? sample(element) : null;
    const backdropInterrupted = previous && previous.backdrop === backdrop && backdrop ? sample(backdrop) : null;
    cancel(key);
    const destination = rectOf(element);
    if (!destination || typeof element.animate !== "function") return Promise.resolve(true);
    const settings = preference(phase);
    const source = settings.spatial ? (phase === "enter" ? origin?.rect : returnRect(origin)) : null;
    const pivot = stablePivot(key, destination, interrupted, previous);
    const transformOrigin = pivot ? `${pivot.x}px ${pivot.y}px` : "50% 50%";
    const local = source ? originTransform(source, destination, pivot) : fullTransform;
    const opening = phase === "enter";
    const start = interrupted ? { ...interrupted, transform: settings.spatial ? interrupted.transform : fullTransform }
      : { transform: opening ? local : fullTransform, opacity: opening ? (source ? .18 : 0) : 1 };
    const end = { transform: opening ? fullTransform : local, opacity: opening ? 1 : 0 };
    let resolve;
    const result = new Promise(done => { resolve = done; });
    const record = { key, element, backdrop, phase, pivot, animations: [], resolve,
      timer: null, releaseFrame: null, releaseWithRaf: false, settled: false, released: false };
    active.set(key, record);
    [element, backdrop].forEach(target => {
      if (target) { owners.set(target, record); target.setAttribute?.("data-layer-motion-active", phase); }
    });
    try {
      record.animations.push(element.animate([
        { ...start, transformOrigin }, { ...end, transformOrigin },
      ], { duration: settings.duration, easing, fill: "both" }));
      if (backdrop && backdrop !== element && typeof backdrop.animate === "function") {
        record.animations.push(backdrop.animate([
          { opacity: backdropInterrupted?.opacity ?? (opening ? 0 : 1) }, { opacity: opening ? 1 : 0 },
        ], { duration: settings.duration, easing: "ease-out", fill: "both" }));
      }
      const completions = record.animations.map(animation => animation.finished);
      if (completions.every(promise => promise && typeof promise.then === "function")) {
        // Explicit cancellation settles the record before cancelling its players.
        // An independent player cancellation must still complete the caller's
        // close lifecycle, otherwise its lock and mounted content remain forever.
        Promise.all(completions).then(() => settle(record, true), () => settle(record, true));
      }
      // A browser can omit animation completion while backgrounding or replacing a layer.
      record.timer = window.setTimeout(() => settle(record, true), settings.duration + 120);
    } catch (_) {
      // Unsupported animation properties must never strand an open/closing layer.
      settle(record, true);
    }
    return result;
  }

  window.galleryLayerMotion = {
    capture,
    enter: options => play("enter", options),
    exit: options => play("exit", options),
    cancel,
    cancelAll() { [...active.keys()].forEach(cancel); },
  };
})();
