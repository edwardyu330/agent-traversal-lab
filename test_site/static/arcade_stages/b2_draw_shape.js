(function () {
  "use strict";

  // Trace a circle by dragging the mouse/pointer around it. The signal isn't
  // accuracy alone — it's that a real drag naturally produces dozens of
  // intermediate samples along a continuously curving path, which is exactly
  // what's expensive for a naive script to fake cheaply. A bot that doesn't
  // special-case this stage will either time out (never presses/drags at
  // all) or produce a "draw" from a couple of teleporting mouse.move() calls
  // — point_count and angular_coverage_deg below catch that distinction
  // directly, independent of the ambient ownpointer_sample stream (which
  // already captures the same drag for click_offset_scatter/correction_count
  // to use elsewhere — this stage's own tracking is a self-contained cross-check,
  // not a replacement for it).
  const RADIUS = 110;
  const START_TOLERANCE_PX = 40; // how far from the ring a mousedown may start
  const TIMEOUT_MS = 12000;
  const MIN_ANGULAR_COVERAGE_DEG = 300; // most of a full loop, not necessarily perfect
  const MAX_MEAN_DEVIATION_PX = 45;

  function angleDeg(cx, cy, x, y) {
    return (Math.atan2(y - cy, x - cx) * 180) / Math.PI;
  }

  window.ARCADE_STAGES.push({
    id: "b2_draw_shape",
    tier: "B",
    title: "Draw the Shape",

    mount(container, ctx) {
      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-b2 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Trace the circle — press, drag all the way around, release</p>
        <canvas class="arcade-draw-canvas" width="320" height="320"></canvas>
      `;
      container.appendChild(wrap);

      const canvas = wrap.querySelector("canvas");
      const g = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const cssSize = 320;
      canvas.width = cssSize * dpr;
      canvas.height = cssSize * dpr;
      canvas.style.width = cssSize + "px";
      canvas.style.height = cssSize + "px";
      g.scale(dpr, dpr);
      const cx = cssSize / 2, cy = cssSize / 2;

      function drawTarget() {
        g.clearRect(0, 0, cssSize, cssSize);
        g.fillStyle = "#111";
        g.fillRect(0, 0, cssSize, cssSize);
        g.strokeStyle = "#3a5a40";
        g.lineWidth = 3;
        g.beginPath();
        g.arc(cx, cy, RADIUS, 0, Math.PI * 2);
        g.stroke();
      }
      drawTarget();

      const startTs = performance.now();
      let done = false;
      let drawing = false;
      let pointCount = 0;
      let deviationSum = 0;
      let minAngle = null, maxAngle = null, unwrapped = 0, lastAngle = null;
      let drawStartTs = null;
      let lastX = null, lastY = null;

      function finish(correct, extra) {
        if (done) return;
        done = true;
        canvas.removeEventListener("pointerdown", onDown);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        clearTimeout(timeoutId);
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct,
          extra,
          player_points: correct ? 20 : 0,
        });
      }

      function onDown(e) {
        if (done) return;
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        const dist = Math.hypot(x - cx, y - cy);
        if (Math.abs(dist - RADIUS) > START_TOLERANCE_PX) return; // didn't start on the ring
        drawing = true;
        drawStartTs = performance.now();
        pointCount = 0;
        deviationSum = 0;
        minAngle = maxAngle = lastAngle = angleDeg(cx, cy, x, y);
        unwrapped = 0;
        lastX = x; lastY = y;
        drawTarget();
        g.strokeStyle = "#6fcf97";
        g.lineWidth = 3;
        g.beginPath();
        g.moveTo(x, y);
      }

      function onMove(e) {
        if (!drawing || done) return;
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        pointCount += 1;
        deviationSum += Math.abs(Math.hypot(x - cx, y - cy) - RADIUS);
        const angle = angleDeg(cx, cy, x, y);
        let delta = angle - lastAngle;
        if (delta > 180) delta -= 360;
        if (delta < -180) delta += 360;
        unwrapped += delta;
        lastAngle = angle;
        if (x >= 0 && x <= cssSize && y >= 0 && y <= cssSize) {
          g.lineTo(x, y);
          g.stroke();
        }
        lastX = x; lastY = y;
      }

      function onUp() {
        if (!drawing || done) return;
        drawing = false;
        const angularCoverage = Math.abs(unwrapped);
        const meanDeviation = pointCount ? deviationSum / pointCount : null;
        const correct = pointCount >= 8 && angularCoverage >= MIN_ANGULAR_COVERAGE_DEG &&
          meanDeviation !== null && meanDeviation <= MAX_MEAN_DEVIATION_PX;
        finish(correct, {
          point_count: pointCount,
          angular_coverage_deg: Math.round(angularCoverage),
          mean_deviation_px: meanDeviation !== null ? Math.round(meanDeviation * 10) / 10 : null,
          draw_duration_ms: performance.now() - drawStartTs,
          attempted: true,
        });
      }

      canvas.addEventListener("pointerdown", onDown);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);

      const timeoutId = setTimeout(() => {
        finish(false, {
          point_count: pointCount,
          angular_coverage_deg: minAngle === null ? 0 : Math.round(Math.abs(unwrapped)),
          mean_deviation_px: null,
          draw_duration_ms: null,
          attempted: pointCount > 0,
        });
      }, TIMEOUT_MS);
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
