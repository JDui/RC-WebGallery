(function () {
  "use strict";

  const root = document.documentElement;
  const requestFrame = typeof window.requestAnimationFrame === "function"
    ? window.requestAnimationFrame.bind(window)
    : (callback) => window.setTimeout(callback, 16);
  const cancelFrame = typeof window.cancelAnimationFrame === "function"
    ? window.cancelAnimationFrame.bind(window)
    : window.clearTimeout.bind(window);
  const mediaQuery = (query) => typeof window.matchMedia === "function"
    ? window.matchMedia(query)
    : null;
  const reducedMotion = mediaQuery("(prefers-reduced-motion: reduce)");
  const finePointer = mediaQuery("(hover: hover) and (pointer: fine)");
  const switchVariables = [
    "--glass-active-x", "--glass-active-y", "--glass-active-width", "--glass-active-height",
  ];
  const pointerVariables = ["--glass-pointer-x", "--glass-pointer-y"];
  const pendingSwitches = new Set();
  const pendingPointers = new Map();
  const animations = new Map();
  const removers = [];
  const switchDisplays = new Map();
  let switches = [];
  let surfaces = [];
  let sections = [];
  let switchObserver = null;
  let visibilityObserver = null;
  let performanceObserver = null;
  let resizeObserver = null;
  let frame = null;
  let frameDeadline = null;
  let activeSection = null;
  let activeNavigationKey = null;
  let pendingNavigationKey = null;
  let entranceRequested = false;
  let started = false;
  let destroyed = false;

  function motionAllowed() {
    return !reducedMotion?.matches && root.dataset.performanceMode !== "low";
  }

  function visible(element) {
    if (!element.isConnected || !element.getClientRects().length) return false;
    const visibility = window.getComputedStyle(element).visibility;
    return visibility !== "hidden" && visibility !== "collapse";
  }

  function setVariable(element, name, value) {
    if (element.style.getPropertyValue(name) !== value) {
      element.style.setProperty(name, value);
    }
  }

  function measureSwitch(element) {
    if (!visible(element)) return { element, values: null };
    const active = Array.from(element.children).find((child) =>
      child.tagName === "BUTTON" && child.classList.contains("active") && visible(child));
    if (!active) return { element, values: null };
    const containerRect = element.getBoundingClientRect();
    const activeRect = active.getBoundingClientRect();
    if (!activeRect.width || !activeRect.height) return { element, values: null };
    const px = (value) => `${Math.round(value * 100) / 100}px`;
    return {
      element,
      values: [
        px(activeRect.left - containerRect.left - element.clientLeft + element.scrollLeft),
        px(activeRect.top - containerRect.top - element.clientTop + element.scrollTop),
        px(activeRect.width),
        px(activeRect.height),
      ],
    };
  }

  function currentSection() {
    // Alpine keeps the outgoing view visible until its leave transition ends.
    return sections.find(section => !section.inert && section.getAttribute("aria-hidden") !== "true" && visible(section)) || null;
  }

  function cancelAnimations() {
    animations.forEach((animation) => animation.cancel());
    animations.clear();
  }

  function animate(element, keyframes, options) {
    if (!element || !visible(element) || typeof element.animate !== "function") return;
    let animation;
    try { animation = element.animate(keyframes, options); } catch (_) { return; }
    animations.set(element, animation);
    const forget = () => {
      if (animations.get(element) === animation) animations.delete(element);
    };
    animation.addEventListener("finish", forget, { once: true });
    animation.addEventListener("cancel", forget, { once: true });
  }

  function animateEntrance(section) {
    const interrupted = new Map();
    animations.forEach((animation, element) => {
      const style = window.getComputedStyle(element);
      interrupted.set(element, { opacity: style.opacity, transform: style.transform });
    });
    cancelAnimations();
    if (!section || reducedMotion?.matches) return;
    const lowPerformance = root.dataset.performanceMode === "low";
    // The view's CSS transition already owns its opacity. A second animation
    // overrides it and can flash the outgoing or incoming panel.
    if (!section.hasAttribute('x-transition:enter')) {
      animate(section, [{ opacity: interrupted.get(section)?.opacity || 0.45 }, { opacity: 1 }], {
        duration: lowPerformance ? 180 : 280,
        easing: "cubic-bezier(.2,.75,.25,1)",
      });
    }
    const heading = section.querySelector(".section-heading > div:first-child");
    const copy = [
      document.querySelector(".topbar-label"),
      document.querySelector(".topbar-title-row > h2"),
      heading?.textContent.trim() ? heading : null,
    ];
    const copyFrames = lowPerformance ? [{ opacity: 0.55 }, { opacity: 1 }] : [
      { opacity: 0.55, transform: "translateY(6px)" },
      { opacity: 1, transform: "translateY(0)" },
    ];
    copy.forEach((element, index) => animate(element, [interrupted.get(element) || copyFrames[0], copyFrames[1]], {
      duration: lowPerformance ? 180 : 300,
      delay: lowPerformance || interrupted.has(element) ? 0 : index * 12,
      easing: "cubic-bezier(.2,.75,.25,1)",
      fill: "backwards",
    }));
  }

  function flush() {
    if (!started) return;
    const measurements = Array.from(pendingSwitches, measureSwitch);
    pendingSwitches.clear();
    const pointerMeasurements = [];
    pendingPointers.forEach((point, element) => {
      if (point.x !== undefined) { pointerMeasurements.push({ element, point }); return; }
      if (!element.isConnected) return;
      const rect = element.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const position = (value, size) => `${Math.max(0, Math.min(100, value / size * 100)).toFixed(1)}%`;
      pointerMeasurements.push({ element, point: {
        x: position(point.clientX - rect.left, rect.width),
        y: position(point.clientY - rect.top, rect.height),
      } });
    });
    pendingPointers.clear();
    measurements.forEach(({ element, values }) => {
      if (values) {
        values.forEach((value, index) => setVariable(element, switchVariables[index], value));
      }
      element.classList.toggle("glass-switch-ready", Boolean(values));
    });
    pointerMeasurements.forEach(({ point, element }) => {
      setVariable(element, pointerVariables[0], point.x);
      setVariable(element, pointerVariables[1], point.y);
    });
    if (entranceRequested) {
      const nextSection = currentSection();
      if (nextSection !== activeSection || pendingNavigationKey !== activeNavigationKey) {
        animateEntrance(nextSection);
      }
      activeSection = nextSection;
    }
    activeNavigationKey = pendingNavigationKey;
    entranceRequested = false;
  }

  function runFlush() {
    if (frame !== null) cancelFrame(frame);
    if (frameDeadline !== null) window.clearTimeout(frameDeadline);
    frame = null;
    frameDeadline = null;
    flush();
  }

  function scheduleFrame() {
    if (started && frame === null && frameDeadline === null) {
      frame = requestFrame(runFlush);
      frameDeadline = window.setTimeout(runFlush, 120);
    }
  }

  function refresh(options = {}) {
    if (typeof options?.navigationKey === "string") pendingNavigationKey = options.navigationKey;
    if (!started) return;
    switches.forEach((element) => pendingSwitches.add(element));
    entranceRequested = entranceRequested || Boolean(options?.viewChanged);
    scheduleFrame();
  }

  function listen(target, event, callback, options) {
    target.addEventListener(event, callback, options);
    removers.push(() => target.removeEventListener(event, callback, options));
  }

  function listenMedia(query, callback) {
    if (!query) return;
    if (typeof query.addEventListener === "function") {
      listen(query, "change", callback);
    } else if (typeof query.addListener === "function") {
      query.addListener(callback);
      removers.push(() => query.removeListener(callback));
    }
  }

  function resetPointer(element) {
    pendingPointers.set(element, { x: "50%", y: "35%" });
    scheduleFrame();
  }

  function updateMotionPreference() {
    if (reducedMotion?.matches) cancelAnimations();
    if (!motionAllowed() || !finePointer?.matches) {
      surfaces.forEach(resetPointer);
    }
  }

  function installSwitchObservers() {
    if (typeof window.MutationObserver === "function") {
      switchObserver = new MutationObserver((records) => {
        records.forEach((record) => {
          const element = record.target.closest?.(".mode-switch");
          if (!element || !switchDisplays.has(element)) return;
          if (record.target === element && record.type === "attributes") {
            if (record.attributeName === "class") return;
            if (record.attributeName === "style") {
              if (switchDisplays.get(element) === element.style.display) return;
              switchDisplays.set(element, element.style.display);
            }
          }
          pendingSwitches.add(element);
        });
        if (pendingSwitches.size) scheduleFrame();
      });
      visibilityObserver = new MutationObserver(() => refresh());
      const visibilityParents = new Set();
      switches.forEach((element) => {
        switchDisplays.set(element, element.style.display);
        switchObserver.observe(element, {
          attributes: true,
          attributeFilter: ["class", "style", "hidden", "x-cloak"],
          childList: true,
          subtree: true,
        });
        for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
          if (parent.hasAttribute("x-show")) visibilityParents.add(parent);
        }
      });
      visibilityParents.forEach((element) => {
        visibilityObserver.observe(element, {
          attributes: true,
          attributeFilter: ["style", "hidden", "x-cloak"],
        });
        const finishReveal = (event) => {
          if (event.target === element && !event.pseudoElement) refresh();
        };
        listen(element, "animationend", finishReveal);
        listen(element, "transitionend", finishReveal);
      });
      performanceObserver = new MutationObserver(updateMotionPreference);
      performanceObserver.observe(root, { attributes: true, attributeFilter: ["data-performance-mode"] });
    }
    if (typeof window.ResizeObserver === "function") {
      resizeObserver = new ResizeObserver((entries) => {
        entries.forEach(({ target }) => pendingSwitches.add(target));
        scheduleFrame();
      });
      switches.forEach((element) => resizeObserver.observe(element));
    }
  }

  function start() {
    if (started || destroyed) return;
    started = true;
    switches = Array.from(document.querySelectorAll(".mode-switch"));
    surfaces = Array.from(document.querySelectorAll(".sidebar, .topbar"));
    const workspace = document.querySelector(".workspace");
    const viewStack = workspace?.querySelector(".view-stack") || workspace;
    sections = viewStack ? Array.from(viewStack.children).filter((element) => element.tagName === "SECTION") : [];
    activeSection = currentSection();
    activeNavigationKey = pendingNavigationKey;
    installSwitchObservers();
    switches.forEach((element) => listen(element, "scroll", () => {
      pendingSwitches.add(element);
      scheduleFrame();
    }, { passive: true }));
    surfaces.forEach((element) => {
      listen(element, "pointermove", (event) => {
        if (!motionAllowed() || !finePointer?.matches || event.pointerType === "touch") return;
        pendingPointers.set(element, {
          clientX: event.clientX,
          clientY: event.clientY,
        });
        scheduleFrame();
      }, { passive: true });
      listen(element, "pointerleave", () => resetPointer(element), { passive: true });
      listen(element, "pointercancel", () => resetPointer(element), { passive: true });
    });
    listen(window, "resize", () => refresh(), { passive: true });
    listenMedia(reducedMotion, updateMotionPreference);
    listenMedia(finePointer, updateMotionPreference);
    document.fonts?.ready?.then(() => {
      if (started) refresh();
    });
    refresh();
    updateMotionPreference();
  }

  function stop() {
    if (!started) return;
    started = false;
    if (frame !== null) cancelFrame(frame);
    if (frameDeadline !== null) window.clearTimeout(frameDeadline);
    frame = null;
    frameDeadline = null;
    entranceRequested = false;
    pendingSwitches.clear();
    pendingPointers.clear();
    cancelAnimations();
    removers.splice(0).forEach((remove) => remove());
    [switchObserver, visibilityObserver, performanceObserver, resizeObserver].forEach((observer) => observer?.disconnect());
    switchObserver = visibilityObserver = performanceObserver = resizeObserver = null;
    switches.forEach((element) => {
      element.classList.remove("glass-switch-ready");
      switchVariables.forEach((name) => element.style.removeProperty(name));
    });
    surfaces.forEach((element) => pointerVariables.forEach((name) => element.style.removeProperty(name)));
    switchDisplays.clear();
    switches = [];
    surfaces = [];
    sections = [];
    activeSection = null;
    activeNavigationKey = null;
  }

  function pageHide() {
    stop();
  }

  function pageShow(event) {
    if (event.persisted) start();
  }

  function destroy() {
    destroyed = true;
    stop();
    document.removeEventListener("alpine:initialized", start);
    document.removeEventListener("DOMContentLoaded", start);
    window.removeEventListener("pagehide", pageHide);
    window.removeEventListener("pageshow", pageShow);
  }

  window.GalleryGlass = { refresh, destroy };
  document.addEventListener("alpine:initialized", start, { once: true });
  document.addEventListener("DOMContentLoaded", start, { once: true });
  window.addEventListener("pagehide", pageHide);
  window.addEventListener("pageshow", pageShow);
  if (document.readyState === "complete") start();
})();
