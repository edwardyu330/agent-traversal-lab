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

  // Generator scripts (run_playwright_raw.py, run_browser_use.py) hit
  // /arcade?label=agent_raw_cdp|agent_llm_cdp to get a pre-labeled, trusted
  // session — same convention collector.js already uses for the storefront.
  // Real players get no query param and stay "pending" until they self-report
  // at reveal. A generator session never needs to submit the reveal form at
  // all: label/trust are already correct from session-start (server.py's
  // /api/session/start sets trust='verified' for any non-"pending" label).
  const GENERATOR_LABELS = new Set(["agent_raw_cdp", "agent_llm_cdp"]);

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
      body: JSON.stringify({ session_id: sessionId, label: sessionLabel, user_agent: navigator.userAgent, first_page: "/arcade" }),
    }).catch(() => {});
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

  function samplePointer(e, coalesced) {
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
      coalesced: !!coalesced,
    });
  }

  function pointerHandler(e) {
    lastPointerType = e.pointerType || lastPointerType;
    if (e.pressure !== undefined) lastPressure = e.pressure;
    if (e.tiltX !== undefined) lastTiltX = e.tiltX;
    if (e.tiltY !== undefined) lastTiltY = e.tiltY;

    if (typeof e.getCoalescedEvents === "function") {
      const coalesced = e.getCoalescedEvents();
      if (coalesced.length > 0) {
        for (const ce of coalesced) samplePointer(ce, true);
        return;
      }
    }
    samplePointer(e, false);
  }

  // pointerrawupdate (Chrome/Edge, unthrottled, no coalescing needed — every raw
  // sample already delivered) falls back to pointermove (broader support, use
  // getCoalescedEvents() on it to recover sub-frame resolution) if unavailable.
  if ("onpointerrawupdate" in window) {
    document.addEventListener("pointerrawupdate", pointerHandler);
  } else {
    document.addEventListener("pointermove", pointerHandler);
  }

  document.addEventListener("click", (e) => {
    const rect = e.target && e.target.getBoundingClientRect ? e.target.getBoundingClientRect() : null;
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
