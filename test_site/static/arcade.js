(function () {
  "use strict";

  // ============================================================================
  // Session bookkeeping — deliberately NOT sharing collector.js. /arcade needs
  // unthrottled pointer capture; collector.js throttles mousemove to one sample
  // per 50ms (MOUSEMOVE_MIN_INTERVAL_MS), which would destroy exactly the
  // resolution these games exist to capture. Self-contained here instead of
  // patching that shared throttle, so the storefront/`/play` flow's behavior
  // (and its already-tested data shape) doesn't change under it.
  // ============================================================================

  const FLUSH_INTERVAL_MS = 2000;
  const JANK_THRESHOLD_MS = 20; // worse than 50fps — mirrored server-side in arcade_metrics.py

  // Bump this whenever the stage roster or capture logic changes materially.
  // Stamped on the session at creation (sessions.build_version) so audit_dataset.py
  // can group/filter by it instead of hand-inspecting which stage_results exist —
  // that's how we discovered 5 of 9 arcade sessions predated three stages this
  // audit pass added. v5 = b2_draw_shape (new stage), reset-on-wrong-attempt for
  // A1/A2/A4 (each now logs a stage_result per wrong attempt before the final
  // one — see arcade_metrics._stage_result()'s "last match, not first" fix),
  // per-wrong-attempt score penalties, and a live suspicion predictor polling
  // /api/verdict during play. v4 = trackPageLoadSignals() — /arcade never sent a
  // page_load_signals event at all, leaving webdriver_artifacts.py's entire
  // signal set (webdriver_flag/suspicious_webgl_renderer/page_load_count)
  // structurally blind on the primary data-collection surface; found while
  // investigating why agent_raw_cdp's Bot catch rate was so low. v3 = the
  // stale_frame_offset_ms/click_offset_scatter/coalesced_event_ratio fixes plus
  // stale_element_interaction/has_pointerrawupdate; v2 = the original 7-stage
  // roster before those capture fixes (unstamped in the DB — inferred from
  // field absence, not a real recorded value).
  const ARCADE_BUILD_VERSION = "v5-2026-08-19";

  // Generator scripts (run_playwright_raw.py, run_browser_use.py) hit
  // /arcade?label=agent_raw_cdp|agent_llm_cdp to get a pre-labeled, trusted
  // session — same convention collector.js already uses for the storefront.
  // Real players get no query param and stay "pending" until they self-report
  // at reveal. A generator session never needs to submit the reveal form at
  // all: label/trust are already correct from session-start (server.py's
  // /api/session/start sets trust='verified' for any non-"pending" label).
  const GENERATOR_LABELS = new Set(["agent_raw_cdp", "agent_llm_cdp", "agent_stealth_cdp", "agent_stealth_typing_cdp"]);

  function resolveLabel() {
    const fromQuery = new URLSearchParams(location.search).get("label");
    return GENERATOR_LABELS.has(fromQuery) ? fromQuery : "pending";
  }

  const sessionId = crypto.randomUUID();
  const sessionLabel = resolveLabel();
  const queue = [];
  let currentStageId = null;

  function track(type, payload) {
    queue.push({ type, page: "/arcade", client_ts: performance.now() + performance.timeOrigin, payload: payload || {} });
  }

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
    return fetch("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId, label: sessionLabel, user_agent: navigator.userAgent,
        first_page: "/arcade", build_version: ARCADE_BUILD_VERSION,
      }),
    }).catch(() => {});
  }

  // Ported from collector.js's identical helper — webdriver_artifacts.py's
  // webdriver_flag/suspicious_webgl_renderer/page_load_count all read off a
  // page_load_signals event that only collector.js (storefront/`/play`) was
  // ever sending. /arcade never sent one at all, which meant that whole signal
  // track — the ONLY thing that told apart a raw-CDP session that patches
  // nothing (webdriver_flag=true, WebGL falls back to SwiftShader) from one
  // that does — was silently blind on the surface that's now the primary data
  // collection path. Found by checking why agent_raw_cdp's catch rate was so
  // low: every current-build session had webdriver_flag/suspicious_webgl_renderer
  // reading false regardless of how the browser was actually launched, simply
  // because the event that would have carried that information never existed.
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

  function trackPageLoadSignals() {
    track("page_load_signals", {
      webdriver: navigator.webdriver === true,
      webgl: getWebglInfo(),
      viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
      languages: navigator.languages,
      plugins_length: navigator.plugins ? navigator.plugins.length : null,
      hardware_concurrency: navigator.hardwareConcurrency,
      referrer: document.referrer,
    });
  }

  window.addEventListener("pagehide", () => flush(true));
  window.addEventListener("beforeunload", () => flush(true));
  setInterval(() => flush(false), FLUSH_INTERVAL_MS);

  // ============================================================================
  // Ambient telemetry — captured identically regardless of which stage is
  // running, so even a pure-fun stage produces real data. Attached once, not
  // per-stage; each sample is tagged with currentStageId so signal extraction
  // can window by stage.
  // ============================================================================

  let lastPointerType = "mouse";
  let lastPressure = 0;
  let lastTiltX = 0;
  let lastTiltY = 0;
  let batchSeq = 0; // groups samples from the same pointerHandler call, so
                     // extra-samples-from-coalescing can be summed once per
                     // batch server-side instead of overcounted per-sample

  function samplePointer(e, coalesced, batchSize, seq) {
    track("pointer_sample", {
      stage_id: currentStageId,
      x: e.clientX,
      y: e.clientY,
      pointer_type: e.pointerType || lastPointerType,
      pressure: e.pressure ?? lastPressure,
      tilt_x: e.tiltX ?? lastTiltX,
      tilt_y: e.tiltY ?? lastTiltY,
      movement_x: e.movementX ?? 0,
      movement_y: e.movementY ?? 0,
      // "coalesced" means getCoalescedEvents() actually returned >1 entries —
      // it near-always returns a 1-length array containing just the event
      // itself even with zero real merging, so ">0" (the old check) is always
      // true and meaningless. batch_size/batch_seq let the server sum real
      // extra samples per delivered event without double-counting a batch
      // once per one of its own samples.
      coalesced: !!coalesced,
      batch_size: batchSize,
      batch_seq: seq,
    });
  }

  function pointerHandler(e) {
    lastPointerType = e.pointerType || lastPointerType;
    if (e.pressure !== undefined) lastPressure = e.pressure;
    if (e.tiltX !== undefined) lastTiltX = e.tiltX;
    if (e.tiltY !== undefined) lastTiltY = e.tiltY;

    const seq = batchSeq++;
    if (typeof e.getCoalescedEvents === "function") {
      const coalesced = e.getCoalescedEvents();
      if (coalesced.length > 1) {
        for (const ce of coalesced) samplePointer(ce, true, coalesced.length, seq);
        return;
      }
    }
    samplePointer(e, false, 1, seq);
  }

  // pointerrawupdate (Chrome/Edge, unthrottled, no coalescing needed — every raw
  // sample already delivered) falls back to pointermove (broader support, use
  // getCoalescedEvents() on it to recover sub-frame resolution) if unavailable.
  // Confirmed false in Playwright-bundled headless Chromium — logged once per
  // session (not inferred from pointer_sample_density after the fact) so this
  // stops being a guess: it's both a diagnostic for capture-path audits and,
  // if real end-user Chrome has it while a given automation stack doesn't, a
  // free binary fingerprint of that stack.
  const hasPointerRawUpdate = "onpointerrawupdate" in window;
  track("arcade_capabilities", { has_pointerrawupdate: hasPointerRawUpdate });
  if (hasPointerRawUpdate) {
    document.addEventListener("pointerrawupdate", pointerHandler);
  } else {
    document.addEventListener("pointermove", pointerHandler);
  }

  // Click-target rect comes from the GAME's own state, not e.target — reading
  // e.target.getBoundingClientRect() was returning (0,0)-origin garbage
  // whenever the click landed on page background (no specific element) or on
  // an element that had already been removed from the DOM by the time this
  // ambient listener ran (bubble order means an element's OWN handler, which
  // can synchronously remove it, always fires before this one). Each stage's
  // own per-element click handler calls setClickTargetRect() as its first
  // action — before any removal — so the rect is always captured while valid.
  // No call = the click hit background/no game element at all, and is
  // correctly excluded from offset stats rather than inflating them.
  let pendingClickTargetRect = null;
  let pendingStaleInteraction = null; // null = no stage registered a target at all (ambient miss); true/false = it did

  function setClickTargetRect(rect, staleInteraction) {
    pendingClickTargetRect = rect;
    pendingStaleInteraction = staleInteraction === undefined ? false : staleInteraction;
  }

  // Per-game reaction: clones the ACTUAL clicked element's rect/color into a
  // fixed-position ghost that pops and fades, then removes itself on its own
  // timer. Each stage calls this with its own element and its own hit/miss
  // call — a mole squashes mole-colored, a box flashes box-colored, etc. —
  // rather than one generic ring shared everywhere. Purely cosmetic and
  // fire-and-forget: reads the element's rect/style synchronously (so it must
  // be called BEFORE the caller removes that element), then never touches
  // real game state again. Deliberately NOT wired into any stage's
  // click-to-advance path: C4's round budget (MOLE_VISIBLE_MS=850 + 120ms
  // gap, x10 rounds) is already close to the external generator scripts'
  // poll-window ceiling, so adding even a short delay to the actual
  // advancement logic risks breaking their timing assumptions. This effect
  // exists alongside that path, not inside it.
  function reactAt(el, hit) {
    if (!el || !el.isConnected) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    const style = getComputedStyle(el);
    const ghost = document.createElement("div");
    ghost.className = "arcade-reaction " + (hit ? "hit" : "miss");
    ghost.style.left = rect.left + "px";
    ghost.style.top = rect.top + "px";
    ghost.style.width = rect.width + "px";
    ghost.style.height = rect.height + "px";
    ghost.style.background = style.backgroundColor;
    ghost.style.borderRadius = style.borderRadius;
    document.body.appendChild(ghost);
    setTimeout(() => ghost.remove(), 340);
  }

  document.addEventListener("click", (e) => {
    const rect = pendingClickTargetRect;
    const staleInteraction = pendingStaleInteraction;
    pendingClickTargetRect = null;
    pendingStaleInteraction = null;
    const targetCx = rect ? rect.left + rect.width / 2 : null;
    const targetCy = rect ? rect.top + rect.height / 2 : null;
    track("click_detail", {
      stage_id: currentStageId,
      x: e.clientX,
      y: e.clientY,
      target_cx: targetCx,
      target_cy: targetCy,
      offset_x: targetCx !== null ? e.clientX - targetCx : null,
      offset_y: targetCy !== null ? e.clientY - targetCy : null,
      // See b1_layout_shift.js et al.'s click handlers: true means the game
      // element this click landed on was already disconnected from the DOM
      // (el.isConnected === false) at the moment ITS OWN handler ran — a real
      // human/CDP click can never target an element that isn't live on
      // screen; only a stale cached reference (screenshot-loop automation
      // clicking on something it saw N steps ago) produces this.
      stale_element_interaction: staleInteraction,
      is_trusted: e.isTrusted,
      pointer_type: e.pointerType || lastPointerType,
      pressure: e.pressure ?? lastPressure,
    });
  });

  // Frame timing — a dropped frame corrupts the timing data these games exist to
  // capture, so it's tracked as a per-stage correctness signal, not just polish.
  let frameStats = null;
  let rafId = null;

  function startFrameTracking() {
    frameStats = { frameCount: 0, droppedFrames: 0, maxFrameMs: 0, sumFrameMs: 0, lastTs: null };
    const loop = (ts) => {
      if (frameStats.lastTs !== null) {
        const delta = ts - frameStats.lastTs;
        frameStats.frameCount += 1;
        frameStats.sumFrameMs += delta;
        if (delta > frameStats.maxFrameMs) frameStats.maxFrameMs = delta;
        if (delta > JANK_THRESHOLD_MS) frameStats.droppedFrames += 1;
      }
      frameStats.lastTs = ts;
      rafId = requestAnimationFrame(loop);
    };
    rafId = requestAnimationFrame(loop);
  }

  function stopFrameTrackingAndReport(stageId) {
    if (rafId !== null) cancelAnimationFrame(rafId);
    rafId = null;
    if (!frameStats || frameStats.frameCount === 0) return;
    track("frame_stats", {
      stage_id: stageId,
      frame_count: frameStats.frameCount,
      dropped_frames: frameStats.droppedFrames,
      mean_frame_ms: frameStats.sumFrameMs / frameStats.frameCount,
      max_frame_ms: frameStats.maxFrameMs,
    });
    frameStats = null;
  }

  // ============================================================================
  // Stage harness
  // ============================================================================

  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function popTick(el, text) {
    el.textContent = text;
    // Retrigger the CSS keyframes animation on a reused element: toggling the
    // class alone is a no-op if it's already present, so force a reflow between
    // remove and re-add.
    el.classList.remove("pop");
    void el.offsetWidth;
    el.classList.add("pop");
  }

  function countdown(container, seconds) {
    return new Promise((resolve) => {
      const el = document.createElement("div");
      el.className = "arcade-countdown";
      container.appendChild(el);
      let n = seconds;
      popTick(el, String(n));
      const iv = setInterval(() => {
        n -= 1;
        if (n <= 0) {
          clearInterval(iv);
          el.remove();
          resolve();
        } else {
          popTick(el, String(n));
        }
      }, 700);
    });
  }

  async function runStage(stage, container) {
    currentStageId = stage.id;
    container.innerHTML = "";
    await countdown(container, 2);
    startFrameTracking();

    const stageResult = await new Promise((resolve) => {
      stage.mount(container, {
        track,
        onDone: resolve,
        prefersReducedMotion,
        setClickTargetRect,
        reactAt,
      });
    });

    stopFrameTrackingAndReport(stage.id);
    if (stage.cleanup) stage.cleanup(container);

    track("stage_result", {
      stage_id: stage.id,
      stage_tier: stage.tier,
      duration_ms: stageResult.duration_ms,
      correct: stageResult.correct,
      extra: stageResult.extra || {},
    });

    return { stageId: stage.id, points: stageResult.player_points || 0, extra: stageResult.extra || {}, correct: stageResult.correct };
  }

  async function runArcade(container, onComplete) {
    await registerSession();
    trackPageLoadSignals();

    const startTs = performance.now();
    let playerScore = 0;
    const stageResults = {};
    const stageOrder = window.ARCADE_STAGES.map((s) => s.id);

    for (const stage of window.ARCADE_STAGES) {
      const result = await runStage(stage, container);
      playerScore += result.points;
      stageResults[result.stageId] = result;
    }

    const totalDurationMs = performance.now() - startTs;
    track("arcade_complete", {
      total_duration_ms: totalDurationMs,
      stage_order: stageOrder,
      reduced_motion: prefersReducedMotion,
    });
    await flush(false);

    onComplete({ sessionId, playerScore, totalDurationMs, stageResults });
  }

  window.ARCADE = { sessionId, label: sessionLabel, track, flush, runArcade };
})();
