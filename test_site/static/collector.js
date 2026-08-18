(function () {
  "use strict";

  const VALID_LABELS = new Set(["human", "agent_raw_cdp", "agent_llm_cdp", "pending"]);
  const FLUSH_INTERVAL_MS = 2000;
  const MOUSEMOVE_MIN_INTERVAL_MS = 50;
  const SCROLL_MIN_INTERVAL_MS = 150;

  function getOrCreateSessionId() {
    let id = sessionStorage.getItem("atl_session_id");
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem("atl_session_id", id);
    }
    return id;
  }

  function getOrCreateLabel() {
    let label = sessionStorage.getItem("atl_label");
    if (!label) {
      const fromQuery = new URLSearchParams(location.search).get("label");
      label = VALID_LABELS.has(fromQuery) ? fromQuery : "unknown";
      sessionStorage.setItem("atl_label", label);
    }
    return label;
  }

  function describeElement(el) {
    if (!el || !el.tagName) return null;
    let desc = el.tagName.toLowerCase();
    if (el.id) desc += "#" + el.id;
    else if (el.className && typeof el.className === "string") {
      desc += "." + el.className.trim().split(/\s+/).slice(0, 2).join(".");
    }
    return desc.slice(0, 80);
  }

  function getWebglInfo() {
    try {
      const canvas = document.createElement("canvas");
      const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      if (!gl) return { supported: false };
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      return {
        supported: true,
        vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
        renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      };
    } catch (e) {
      return { supported: false, error: String(e) };
    }
  }

  const sessionId = getOrCreateSessionId();
  const label = getOrCreateLabel();
  const page = location.pathname;
  const queue = [];

  function track(type, payload) {
    queue.push({ type, page, client_ts: performance.now() + performance.timeOrigin, payload: payload || {} });
  }
  window.ATL = { track, sessionId, label, flushNow: () => flush(false) };

  function flush(useBeacon) {
    if (queue.length === 0) return Promise.resolve();
    const events = queue.splice(0, queue.length);
    const body = JSON.stringify({ session_id: sessionId, events });
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon("/api/telemetry", new Blob([body], { type: "application/json" }));
      return Promise.resolve();
    }
    return fetch("/api/telemetry", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(() => {});
  }

  function registerSession() {
    if (sessionStorage.getItem("atl_session_registered")) return;
    sessionStorage.setItem("atl_session_registered", "1");
    fetch("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        label,
        user_agent: navigator.userAgent,
        first_page: page,
      }),
    }).catch(() => {});
  }

  registerSession();

  track("page_load_signals", {
    webdriver: navigator.webdriver === true,
    webgl: getWebglInfo(),
    viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
    languages: navigator.languages,
    plugins_length: navigator.plugins ? navigator.plugins.length : null,
    hardware_concurrency: navigator.hardwareConcurrency,
    referrer: document.referrer,
  });

  let lastMouseTs = 0;
  document.addEventListener("mousemove", (e) => {
    const t = performance.now();
    if (t - lastMouseTs < MOUSEMOVE_MIN_INTERVAL_MS) return;
    lastMouseTs = t;
    track("mousemove", { x: e.clientX, y: e.clientY });
  });

  document.addEventListener("click", (e) => {
    track("click", { x: e.clientX, y: e.clientY, target: describeElement(e.target) });
  });

  document.addEventListener("keydown", (e) => {
    track("keydown", { target: describeElement(e.target) });
  });
  document.addEventListener("keyup", (e) => {
    track("keyup", { target: describeElement(e.target) });
  });

  let lastScrollTs = 0;
  document.addEventListener("scroll", () => {
    const t = performance.now();
    if (t - lastScrollTs < SCROLL_MIN_INTERVAL_MS) return;
    lastScrollTs = t;
    track("scroll", { scroll_y: window.scrollY });
  });

  document.addEventListener("visibilitychange", () => {
    track("visibility", { state: document.visibilityState });
    if (document.visibilityState === "hidden") flush(true);
  });

  window.addEventListener("pagehide", () => flush(true));
  window.addEventListener("beforeunload", () => flush(true));

  setInterval(() => flush(false), FLUSH_INTERVAL_MS);
})();
