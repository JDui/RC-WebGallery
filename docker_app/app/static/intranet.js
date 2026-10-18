(() => {
  const current = new URL(window.location.href);
  const target = new URL(document.body.dataset.intranetOrigin);
  const fallback = new URL(current);
  fallback.searchParams.set("_gallery_skip_intranet", "1");
  document.getElementById("intranet-fallback").href = fallback.href;
  const controller = new AbortController();
  let finished = false;
  const finish = (reachable) => {
    if (finished) return;
    finished = true;
    window.clearTimeout(timer);
    controller.abort();
    if (reachable && target.origin !== current.origin) {
      target.pathname = current.pathname;
      target.search = current.search;
      target.searchParams.delete("_gallery_skip_intranet");
      target.hash = current.hash;
      window.location.replace(target.href);
    } else {
      window.location.replace(fallback.href);
    }
  };
  const timer = window.setTimeout(() => finish(false), 1500);
  const probe = new URL("/intranet/health", target);
  probe.searchParams.set("_", String(Date.now()));
  fetch(probe.href, {
    mode: "cors", credentials: "omit", cache: "no-store",
    referrerPolicy: "no-referrer", signal: controller.signal,
    targetAddressSpace: "local",
  }).then(async (response) => {
    if (!response.ok) return false;
    const payload = await response.json();
    return payload.ok === true && payload.service === "biligalleryrc";
  }).then(finish, () => finish(false));
})();
