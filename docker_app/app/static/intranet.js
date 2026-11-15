(() => {
  const config = JSON.parse(document.body.dataset.networkConfig || "{}");
  const current = new URL(window.location.href);
  const preflight = document.body.dataset.intranetPreflight === "1";
  const atTarget = config.origin && new URL(config.origin).origin === current.origin;
  const skipKey = "_gallery_skip_intranet";
  const httpKey = "_gallery_http_attempted";
  const sessionKey = "gallery-intranet-checked:" + config.origin;
  const httpSessionKey = "gallery-http-attempt:" + current.hostname + ":" + config.http_port;
  const read = (key) => {
    try { return window.sessionStorage.getItem(key); } catch {
      if (key === sessionKey && document.cookie.split(";").some(item => item.trim() === "gallery_intranet_checked=" + encodeURIComponent(config.origin))) return "1";
      return null;
    }
  };
  const write = (key, value) => { try { window.sessionStorage.setItem(key, value); } catch {} };
  const remember = () => {
    write(sessionKey, "1");
    try { document.cookie = "gallery_intranet_checked=" + encodeURIComponent(config.origin) + "; Path=/; SameSite=Lax"; } catch {}
  };
  const destination = (origin) => {
    const source = new URL(window.location.href);
    const url = new URL(origin);
    url.pathname = source.pathname;
    url.search = source.search;
    url.hash = source.hash;
    url.searchParams.delete(skipKey);
    url.searchParams.delete(httpKey);
    return url;
  };
  let cancelProbe = () => {};
  window.galleryNetwork = {
    connect(values) {
      const rawHost = String(values.intranet_use_proxy_host ? values.site_proxy_host || "" : values.intranet_redirect_host || "").trim();
      const host = rawHost.includes(":") && !rawHost.startsWith("[") ? `[${rawHost}]` : rawHost;
      const protocol = values.intranet_redirect_protocol;
      const port = Number(values.intranet_redirect_port);
      if (!host || /[\s/@?#\\]/.test(host) || !["http", "https"].includes(protocol) || !Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error("请填写有效的内网地址、协议和服务端口");
      }
      const target = destination(`${protocol}://${host}:${port}`);
      cancelProbe();
      remember();
      // VRB uses top-level navigation: no popup, and Back remains available on failure.
      window.location.assign(target.href);
    },
  };

  const skip = current.searchParams.get(skipKey) === "1";
  const httpAttempted = current.searchParams.get(httpKey) === "1";
  if (httpAttempted) write(httpSessionKey, String(Date.now()));
  const recentHttpAttempt = Date.now() - Number(read(httpSessionKey) || 0) < 60000;
  if (config.http_enabled && !atTarget && current.protocol === "https:" && !skip && !httpAttempted && !recentHttpAttempt) {
    const http = new URL(current);
    http.protocol = "http:";
    http.port = String(config.http_port);
    http.searchParams.set(httpKey, "1");
    write(httpSessionKey, String(Date.now()));
    window.location.replace(http.href);
    return;
  }

  const fallback = new URL(current);
  fallback.searchParams.set(skipKey, "1");
  const fallbackLink = document.getElementById("intranet-fallback");
  if (fallbackLink) fallbackLink.href = fallback.href;
  const canProbe = config.auto && config.origin && !atTarget;
  if (skip && canProbe) remember();
  if (!preflight && (skip || httpAttempted)) {
    const clean = new URL(current);
    clean.searchParams.delete(skipKey);
    clean.searchParams.delete(httpKey);
    window.history.replaceState(null, "", clean.href);
  }
  // A server cookie only avoids a loading screen; a new tab still probes once.
  if (!canProbe || skip || read(sessionKey) === "1") {
    if (preflight) window.location.replace(fallback.href);
    return;
  }
  remember();
  let finished = false;
  let timer;
  let imageTimer;
  let remaining = 2;
  const controller = new AbortController();
  const image = new Image();
  const cleanup = () => {
    window.clearTimeout(timer);
    window.clearTimeout(imageTimer);
    controller.abort();
    image.onload = image.onerror = null;
    image.src = "";
  };
  cancelProbe = () => { finished = true; cleanup(); };
  const finish = (reachable) => {
    if (finished) return;
    finished = true;
    cleanup();
    if (reachable) window.location.replace(destination(config.origin).href);
    else if (preflight) window.location.replace(fallback.href);
  };
  const report = (reachable) => {
    if (reachable) finish(true);
    else if (--remaining === 0) finish(false);
  };
  fallbackLink?.addEventListener("click", () => { cancelProbe(); remember(); });
  window.addEventListener("pagehide", cancelProbe, { once: true });
  timer = window.setTimeout(() => finish(false), 1500);
  // VRB's parallel GIF probe helps embedded browsers whose CORS fetch fails.
  const gif = new URL("/intranet/health.gif", config.origin);
  gif.searchParams.set("_", String(Date.now()));
  let imageDone = false;
  const finishImage = (ok) => {
    if (imageDone || finished) return;
    imageDone = true;
    window.clearTimeout(imageTimer);
    report(ok && image.naturalWidth === 1 && image.naturalHeight === 1);
  };
  image.referrerPolicy = "no-referrer";
  image.onload = () => finishImage(true);
  image.onerror = () => finishImage(false);
  imageTimer = window.setTimeout(() => finishImage(false), 1200);
  image.src = gif.href;
  const health = new URL("/intranet/health", config.origin);
  health.searchParams.set("_", String(Date.now()));
  fetch(health.href, {
    mode: "cors", credentials: "omit", cache: "no-store", redirect: "error",
    referrerPolicy: "no-referrer", signal: controller.signal, targetAddressSpace: "local",
  }).then(async (response) => {
    if (!response.ok) return false;
    const payload = await response.json();
    return payload.ok === true && payload.service === "biligalleryrc";
  }).then(report, () => report(false));
})();
