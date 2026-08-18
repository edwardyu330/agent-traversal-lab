(function () {
  "use strict";

  const BOX_COUNT = 6;
  const TIMEOUT_MS = 8000;
  const ORDINALS = ["1st", "2nd", "3rd", "4th", "5th", "6th"];

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  // Boxes are unlabeled — a human can only answer by counting visual left-to-right
  // position, never by reading an index off the box itself. CSS `order` scrambles
  // visual sequence relative to DOM source order; a DOM-order parser (reads
  // element N in source, ignores rendered layout) and a visual reader (counts
  // rendered position) are guaranteed to disagree by construction below.
  function makeLayout(n) {
    let orderValues, instructedN, visualTargetDomIndex, domTargetDomIndex;
    do {
      orderValues = shuffle([...Array(n).keys()]);
      instructedN = 1 + Math.floor(Math.random() * n);
      const domIndicesByVisualOrder = [...Array(n).keys()].sort((a, b) => orderValues[a] - orderValues[b]);
      visualTargetDomIndex = domIndicesByVisualOrder[instructedN - 1] + 1;
      domTargetDomIndex = instructedN;
    } while (visualTargetDomIndex === domTargetDomIndex);
    return { orderValues, instructedN, visualTargetDomIndex, domTargetDomIndex };
  }

  window.ARCADE_STAGES.push({
    id: "a2_visual_vs_dom_order",
    tier: "A",
    title: "Visual vs. DOM Order",

    mount(container, ctx) {
      const { orderValues, instructedN, visualTargetDomIndex, domTargetDomIndex } = makeLayout(BOX_COUNT);

      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-a2 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Click the <strong>${ORDINALS[instructedN - 1]}</strong> box from the left</p>
        <div class="arcade-box-row"></div>
      `;
      container.appendChild(wrap);

      const row = wrap.querySelector(".arcade-box-row");
      const startTs = performance.now();
      let done = false;

      for (let i = 0; i < BOX_COUNT; i++) {
        const domIndex = i + 1;
        const el = document.createElement("div");
        el.className = "arcade-order-box";
        el.style.order = String(orderValues[i]);
        el.dataset.domIndex = String(domIndex);
        el.addEventListener("click", () => onBoxClick(domIndex));
        row.appendChild(el);
      }

      function onBoxClick(clickedDomIndex) {
        if (done) return;
        done = true;
        clearTimeout(timeoutId);
        let choice = "other";
        if (clickedDomIndex === visualTargetDomIndex) choice = "visual";
        else if (clickedDomIndex === domTargetDomIndex) choice = "dom";
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct: choice === "visual",
          extra: {
            instructed_n: instructedN,
            clicked_dom_index: clickedDomIndex,
            visual_target_dom_index: visualTargetDomIndex,
            dom_target_dom_index: domTargetDomIndex,
            choice,
          },
          player_points: choice === "visual" ? 15 : 0,
        });
      }

      const timeoutId = setTimeout(() => {
        if (done) return;
        done = true;
        ctx.onDone({
          duration_ms: performance.now() - startTs,
          correct: false,
          extra: { instructed_n: instructedN, clicked_dom_index: null, visual_target_dom_index: visualTargetDomIndex, dom_target_dom_index: domTargetDomIndex, choice: "none" },
          player_points: 0,
        });
      }, TIMEOUT_MS);
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
