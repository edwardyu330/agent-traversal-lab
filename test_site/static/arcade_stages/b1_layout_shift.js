(function () {
  "use strict";

  // A target that jumps position once the cursor gets close, mimicking a real
  // lazy-load/reflow shifting content out from under a click that's already in
  // flight. Measures the perception→inference→act loop directly: if the click
  // lands near where the target USED to be, that's how far behind "now" this
  // player's motor plan was — arcade_metrics.py converts that pixel gap to
  // milliseconds using this player's own locally-measured cursor speed (from the
  // ambient pointer_sample stream), not a guessed constant.
  const TRIGGER_RADIUS_PX = 140;
  const SHIFT_MIN_PX = 90;
  const SHIFT_MAX_PX = 160;
  const TIMEOUT_MS = 6000;

  window.ARCADE_STAGES.push({
    id: "b1_layout_shift",
    tier: "B",
    title: "Layout Shift",

    mount(container, ctx) {
      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-b1 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Click the box</p>
        <div class="arcade-shift-field"></div>
      `;
      container.appendChild(wrap);

      const field = wrap.querySelector(".arcade-shift-field");
      const target = document.createElement("div");
      target.className = "arcade-shift-target";
      field.appendChild(target);

      const rect = field.getBoundingClientRect();
      const preX = rect.width * (0.25 + Math.random() * 0.5);
      const preY = rect.height * (0.3 + Math.random() * 0.4);
      target.style.left = preX + "px";
      target.style.top = preY + "px";

      let shifted = false;
      let postX = preX, postY = preY;
      let shiftTs = null;
      let done = false;
      const startTs = performance.now();

      function maybeShift(clientX, clientY) {
        if (shifted || done) return;
        const r = field.getBoundingClientRect();
        const tx = r.left + preX, ty = r.top + preY;
        const dist = Math.hypot(clientX - tx, clientY - ty);
        if (dist < TRIGGER_RADIUS_PX && dist > 20) {
          shifted = true;
          shiftTs = performance.now();
          const angle = Math.random() * Math.PI * 2;
          const mag = SHIFT_MIN_PX + Math.random() * (SHIFT_MAX_PX - SHIFT_MIN_PX);
          postX = Math.min(r.width - 20, Math.max(20, preX + Math.cos(angle) * mag));
          postY = Math.min(r.height - 20, Math.max(20, preY + Math.sin(angle) * mag));
          target.style.left = postX + "px";
          target.style.top = postY + "px";
        }
      }

      function onPointerMove(e) {
        maybeShift(e.clientX, e.clientY);
      }
      document.addEventListener("pointermove", onPointerMove);

      function finish(clickX, clickY, timedOut) {
        if (done) return;
        done = true;
        document.removeEventListener("pointermove", onPointerMove);
        clearTimeout(timeoutId);
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct: !timedOut,
          extra: {
            shifted,
            shift_ts: shiftTs,
            pre_shift_x: preX, pre_shift_y: preY,
            post_shift_x: postX, post_shift_y: postY,
            click_x: clickX, click_y: clickY,
          },
          player_points: timedOut ? 0 : 15,
        });
      }

      target.addEventListener("click", (e) => finish(e.clientX, e.clientY, false));

      const timeoutId = setTimeout(() => finish(null, null, true), TIMEOUT_MS);
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
