(() => {
  const tooltip = document.createElement("div");
  tooltip.id = "ui-help-tooltip";
  tooltip.className = "ui-tooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.append(tooltip);

  let anchor = null;
  let previousDescription = null;
  let hideTimer;
  const anchorObserver = new MutationObserver(() => {
    if (anchor && (!anchor.isConnected || anchor.closest("[inert]") || !anchor.getClientRects().length
        || window.getComputedStyle(anchor).visibility === "hidden")) hide();
  });

  const hide = () => {
    clearTimeout(hideTimer);
    anchorObserver.disconnect();
    if (anchor) {
      if (previousDescription === null) anchor.removeAttribute("aria-describedby");
      else anchor.setAttribute("aria-describedby", previousDescription);
    }
    anchor = null;
    tooltip.hidden = true;
  };

  const show = (target) => {
    const text = target?.getAttribute("data-tooltip")?.trim();
    if (!text) { hide(); return; }
    clearTimeout(hideTimer);
    if (anchor !== target) {
      hide();
      anchor = target;
      previousDescription = anchor.getAttribute("aria-describedby");
      anchor.setAttribute("aria-describedby", [previousDescription, tooltip.id].filter(Boolean).join(" "));
      // Observe only the source chain while open. Media transforms elsewhere in
      // the page must not force a tooltip visibility measurement each frame.
      for (let element = anchor; element; element = element.parentElement) {
        anchorObserver.observe(element, { attributes: true, childList: true,
          attributeFilter: ["style", "class", "inert", "hidden"] });
      }
    }
    tooltip.textContent = text;
    tooltip.hidden = false;
    const box = anchor.getBoundingClientRect();
    const size = tooltip.getBoundingClientRect();
    const left = Math.max(12, Math.min(box.left + (box.width - size.width) / 2, window.innerWidth - size.width - 12));
    const below = box.bottom + 8;
    const top = below + size.height <= window.innerHeight - 12 ? below : Math.max(12, box.top - size.height - 8);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };

  const targetFor = (event) => event.target?.closest?.("[data-tooltip]");
  const deferHide = (relatedTarget) => {
    if (anchor?.contains(relatedTarget) || tooltip.contains(relatedTarget)) return;
    hideTimer = setTimeout(hide, 100);
  };

  document.addEventListener("pointerover", (event) => {
    if (tooltip.contains(event.target)) clearTimeout(hideTimer);
    else {
      const target = targetFor(event);
      if (target && target !== anchor) show(target);
      else if (target) clearTimeout(hideTimer);
    }
  });
  document.addEventListener("pointerout", (event) => {
    if (anchor?.contains(event.target) || tooltip.contains(event.target)) deferHide(event.relatedTarget);
  });
  document.addEventListener("focusin", (event) => {
    const target = targetFor(event);
    if (target) show(target);
    else hide();
  });
  document.addEventListener("focusout", (event) => deferHide(event.relatedTarget));
  document.addEventListener("click", (event) => {
    const target = targetFor(event);
    if (target) show(target);
    else if (!tooltip.contains(event.target)) hide();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !tooltip.hidden) {
      hide();
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener("scroll", (event) => {
    if (!tooltip.contains(event.target)) hide();
  }, { capture: true, passive: true });
  window.addEventListener("resize", hide, { passive: true });
})();
