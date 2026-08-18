(function () {
  "use strict";

  const PALETTE = [
    { name: "RED", css: "#e63946" },
    { name: "YELLOW", css: "#f1c40f" },
    { name: "GREEN", css: "#2ecc71" },
    { name: "BLUE", css: "#3498db" },
    { name: "PURPLE", css: "#9b59b6" },
  ];
  const WASH_MS = 260;
  const DECOY_COUNT_RANGE = [2, 4];
  const REACTION_TIMEOUT_MS = 3000;

  function pick(arr) {
    return arr[Math.floor(Math.random() * arr.length)];
  }

  window.ARCADE_STAGES.push({
    id: "c1_flash_reaction",
    tier: "C",
    title: "Flash Reaction",

    mount(container, ctx) {
      const target = pick(PALETTE);
      const decoyCount = DECOY_COUNT_RANGE[0] + Math.floor(Math.random() * (DECOY_COUNT_RANGE[1] - DECOY_COUNT_RANGE[0] + 1));
      const decoys = [];
      for (let i = 0; i < decoyCount; i++) {
        let c;
        do { c = pick(PALETTE); } while (c.name === target.name);
        decoys.push(c);
      }

      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-c1 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Click when the wash settles on <strong>${target.name}</strong></p>
        <canvas class="arcade-canvas"></canvas>
      `;
      container.appendChild(wrap);
      const canvas = wrap.querySelector("canvas");
      const dpr = window.devicePixelRatio || 1;
      const resize = () => {
        canvas.width = canvas.clientWidth * dpr;
        canvas.height = canvas.clientHeight * dpr;
      };
      resize();
      const g = canvas.getContext("2d");

      let settled = false;
      let settleTs = null;
      let done = false;
      const startTs = performance.now();

      function drawWash(css, progress) {
        const w = canvas.width, h = canvas.height;
        g.fillStyle = "#111";
        g.fillRect(0, 0, w, h);
        const maxR = Math.hypot(w, h) / 2;
        g.fillStyle = css;
        g.beginPath();
        g.arc(w / 2, h / 2, Math.max(0, maxR * progress), 0, Math.PI * 2);
        g.fill();
      }

      // done guards every scheduled continuation below, not just onDone — a click
      // during a decoy wash must stop that wash's own rAF/setTimeout chain, or it
      // keeps running (and drawing to a canvas the stage harness may have already
      // torn down for the next stage) after this stage has already reported in.
      function washSequence(colors, i, onAllDone) {
        if (done) return;
        if (i >= colors.length) return onAllDone();
        const color = colors[i];
        const start = performance.now();
        function frame(now) {
          if (done) return;
          const p = Math.min(1, (now - start) / WASH_MS);
          drawWash(color.css, ctx.prefersReducedMotion ? 1 : p);
          if (p < 1 && !ctx.prefersReducedMotion) {
            requestAnimationFrame(frame);
          } else {
            setTimeout(() => washSequence(colors, i + 1, onAllDone), 150 + Math.random() * 300);
          }
        }
        requestAnimationFrame(frame);
      }

      function settleOnTarget() {
        if (done) return;
        const start = performance.now();
        function frame(now) {
          if (done) return;
          const p = Math.min(1, (now - start) / WASH_MS);
          drawWash(target.css, ctx.prefersReducedMotion ? 1 : p);
          if (p < 1 && !ctx.prefersReducedMotion) {
            requestAnimationFrame(frame);
          } else {
            settled = true;
            settleTs = performance.now();
            setTimeout(() => finish(false, null), REACTION_TIMEOUT_MS);
          }
        }
        requestAnimationFrame(frame);
      }

      function finish(correct, reactionMs) {
        if (done) return;
        done = true;
        canvas.removeEventListener("click", onClick);
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct,
          extra: { reaction_ms: reactionMs, target: target.name, false_start: !settled && reactionMs === null ? null : !settled },
          player_points: correct ? Math.max(10, Math.round(500 - (reactionMs || 500))) : 0,
        });
      }

      function onClick() {
        if (!settled) {
          finish(false, null); // false start
          return;
        }
        finish(true, performance.now() - settleTs);
      }

      canvas.addEventListener("click", onClick);
      washSequence(decoys, 0, settleOnTarget);
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
