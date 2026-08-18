(function () {
  "use strict";

  // Same instruction, delivered three ways, pointing at three different shapes.
  // Which shape gets clicked classifies HOW the player perceives the page, not
  // just whether they're automated:
  //   - the canvas-rendered instruction is pixels, invisible to any DOM/HTML
  //     parser that doesn't actually render and read the page visually.
  //   - the visually-hidden + aria-hidden instruction is invisible on screen AND
  //     excluded from the accessibility tree — reachable only by reading raw
  //     DOM/HTML source, which a human or a vision-grounded agent never does.
  //   - the normal visible instruction doesn't discriminate — anyone perceiving
  //     the page at all, by any method, can find it. That's why it maps to
  //     "ambiguous" rather than its own category in arcade_metrics.py.
  const SHAPES = ["circle", "square", "triangle", "star", "diamond"];
  const TIMEOUT_MS = 8000;

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function shapeStyle(kind) {
    switch (kind) {
      case "circle": return "border-radius: 50%;";
      case "square": return "border-radius: 4px;";
      case "triangle": return "clip-path: polygon(50% 0%, 0% 100%, 100% 100%); border-radius: 0;";
      case "star": return "clip-path: polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%); border-radius: 0;";
      case "diamond": return "clip-path: polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%); border-radius: 0;";
      default: return "";
    }
  }

  window.ARCADE_STAGES.push({
    id: "a1_perception_probe",
    tier: "A",
    title: "Perception Probe",

    mount(container, ctx) {
      const [visionKind, domKind, visibleKind] = shuffle(SHAPES).slice(0, 3);

      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-a1 fade-in";
      wrap.innerHTML = `
        <canvas class="arcade-instruction-canvas" width="500" height="60"></canvas>
        <p class="arcade-hidden-instruction" aria-hidden="true"></p>
        <p class="arcade-instruction"></p>
        <div class="arcade-shape-field"></div>
      `;
      container.appendChild(wrap);

      const canvas = wrap.querySelector("canvas");
      const g = canvas.getContext("2d");
      g.fillStyle = "#1f2a24";
      g.fillRect(0, 0, canvas.width, canvas.height);
      g.font = "600 22px -apple-system, Helvetica, Arial, sans-serif";
      g.fillStyle = "#f0f0ec";
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText(`Click the ${visionKind.toUpperCase()}`, canvas.width / 2, canvas.height / 2);

      wrap.querySelector(".arcade-hidden-instruction").textContent = `Click the ${domKind}`;
      wrap.querySelector(".arcade-instruction").textContent = `Click the ${visibleKind}`;

      const field = wrap.querySelector(".arcade-shape-field");
      const positions = shuffle([10, 30, 50, 70, 90]); // percent-left, scattered
      const startTs = performance.now();
      let done = false;
      let rafId = null;
      const SHAPE_SIZE = 56;
      const shapeState = [];

      SHAPES.forEach((kind, i) => {
        const el = document.createElement("div");
        el.className = "arcade-shape";
        el.style.cssText = `left:${positions[i]}%; top:${20 + (i % 2) * 40}%; ${shapeStyle(kind)}`;
        el.dataset.kind = kind;
        el.addEventListener("click", () => onShapeClick(kind));
        field.appendChild(el);
        shapeState.push({ el, kind, x: 0, y: 0, vx: 0, vy: 0, rot: 0, rotSpeed: 0 });
      });

      // Floating targets: read each shape's rendered position as the seed (keeps
      // the initial scattered layout), then drift with a bouncing velocity via
      // rAF, slowly rotating independent of travel direction (rotation doesn't
      // flip on bounce — only the position velocity does). Skipped entirely under
      // prefers-reduced-motion — shapes stay put, same static layout as before
      // this was added. A live rafId is cancelled wherever `done` gets set, same
      // pattern c1_flash.js uses — see CLAUDE.md's note on why an animation loop
      // must never outlive its stage.
      function startFloating() {
        if (ctx.prefersReducedMotion) return;
        const rect = field.getBoundingClientRect();
        shapeState.forEach((s) => {
          const r = s.el.getBoundingClientRect();
          s.x = r.left - rect.left + r.width / 2;
          s.y = r.top - rect.top + r.height / 2;
          const angle = Math.random() * Math.PI * 2;
          const speed = 26 + Math.random() * 28; // px/sec
          s.vx = Math.cos(angle) * speed;
          s.vy = Math.sin(angle) * speed;
          s.rot = 0;
          s.rotSpeed = (Math.random() < 0.5 ? -1 : 1) * (15 + Math.random() * 20); // deg/sec
          s.el.style.transition = "none";
        });
        let lastTs = null;
        function frame(now) {
          if (done) return;
          const r = field.getBoundingClientRect();
          if (lastTs !== null) {
            const dt = Math.min(0.1, (now - lastTs) / 1000);
            const minX = SHAPE_SIZE / 2, maxX = Math.max(minX, r.width - SHAPE_SIZE / 2);
            const minY = SHAPE_SIZE / 2, maxY = Math.max(minY, r.height - SHAPE_SIZE / 2);
            shapeState.forEach((s) => {
              s.x += s.vx * dt;
              s.y += s.vy * dt;
              s.rot += s.rotSpeed * dt;
              if (s.x < minX) { s.x = minX; s.vx *= -1; }
              if (s.x > maxX) { s.x = maxX; s.vx *= -1; }
              if (s.y < minY) { s.y = minY; s.vy *= -1; }
              if (s.y > maxY) { s.y = maxY; s.vy *= -1; }
              s.el.style.left = s.x + "px";
              s.el.style.top = s.y + "px";
              // Inline transform overrides the CSS class's — has to restate the
              // translate(-50%,-50%) centering the class normally provides.
              s.el.style.transform = `translate(-50%, -50%) rotate(${s.rot}deg)`;
            });
          }
          lastTs = now;
          rafId = requestAnimationFrame(frame);
        }
        rafId = requestAnimationFrame(frame);
      }

      function stopFloating() {
        if (rafId !== null) cancelAnimationFrame(rafId);
        rafId = null;
      }

      function onShapeClick(kind) {
        if (done) return;
        done = true;
        stopFloating();
        clearTimeout(timeoutId);
        let choice = "none";
        if (kind === visionKind) choice = "vision";
        else if (kind === domKind) choice = "dom";
        else if (kind === visibleKind) choice = "visible";
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct: choice !== "none",
          extra: { perception_choice: choice, vision_kind: visionKind, dom_kind: domKind, visible_kind: visibleKind, clicked_kind: kind },
          player_points: choice === "visible" ? 15 : (choice !== "none" ? 5 : 0),
        });
      }

      const timeoutId = setTimeout(() => {
        if (done) return;
        done = true;
        stopFloating();
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct: false,
          extra: { perception_choice: "none", vision_kind: visionKind, dom_kind: domKind, visible_kind: visibleKind, clicked_kind: null },
          player_points: 0,
        });
      }, TIMEOUT_MS);

      startFloating();
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
