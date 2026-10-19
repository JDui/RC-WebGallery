(() => {
  const current = new URL(window.location.href);
  // 脚本在 <head> 中先于样式表执行，此时 body 还不存在，优先读自身的 data 属性。
  const script = document.currentScript;
  const body = document.body;
  const origin = (script && script.dataset.intranetOrigin) || (body && body.dataset.intranetOrigin);
  const target = new URL(origin);
  const fallback = new URL(current);
  fallback.searchParams.set("_gallery_skip_intranet", "1");
  // 探测结果按「当前地址 → 目标地址」缓存在本标签页：成功 5 分钟内直接跳转，
  // 失败 60 秒内直接放行，避免反复渲染检测页并白等超时。
  const cacheKey = "intranet-probe:" + target.origin;
  const okLifetime = 300000;
  const failLifetime = 60000;
  let storage = null;
  try {
    storage = window.sessionStorage;
  } catch (error) {
    storage = null;
  }
  const readCache = () => {
    if (!storage) return null;
    try {
      const value = JSON.parse(storage.getItem(cacheKey));
      if (!value || typeof value.ok !== "boolean" || typeof value.at !== "number") return null;
      const lifetime = value.ok ? okLifetime : failLifetime;
      return Date.now() - value.at < lifetime ? value : null;
    } catch (error) {
      return null;
    }
  };
  const controller = new AbortController();
  let timer = 0;
  let finished = false;
  const finish = (reachable, fromCache) => {
    if (finished) return;
    finished = true;
    window.clearTimeout(timer);
    controller.abort();
    if (!fromCache && storage) {
      try {
        storage.setItem(cacheKey, JSON.stringify({ ok: reachable, at: Date.now() }));
      } catch (error) {
        // 隐私模式下写入可能抛错，忽略即可，不影响跳转。
      }
    }
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
  const cached = readCache();
  if (cached) {
    finish(cached.ok, true);
    return;
  }
  timer = window.setTimeout(() => finish(false), 1500);
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
