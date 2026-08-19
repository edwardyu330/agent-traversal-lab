(function () {
  "use strict";

  // Click safe moles, avoid near-identical traps, under time pressure. The point
  // isn't really the game — it's error_rate_floor: this task is deliberately
  // built to induce a few human mistakes (fast decisions, similar-looking
  // targets), so a session with zero errors here is itself a tell, not
  // necessarily a good thing. Each round logs its own stage_result (not just one
  // at the end), so error_rate_floor gets real trial-level resolution instead of
  // one pass/fail data point for the whole stage.
  const ROUNDS = 10;
  const TRAP_PROBABILITY = 0.3;
  const MOLE_VISIBLE_MS = 850;
  const GRID_SIZE = 9; // 3x3

  window.ARCADE_STAGES.push({
    id: "c4_whack_a_mole",
    tier: "C",
    title: "Go / No-Go",

    mount(container, ctx) {
      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-c4 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Click <strong>green</strong>, avoid <strong>red</strong></p>
        <div class="arcade-mole-grid"></div>
      `;
      container.appendChild(wrap);
      const grid = wrap.querySelector(".arcade-mole-grid");
      const cells = [];
      for (let i = 0; i < GRID_SIZE; i++) {
        const cell = document.createElement("div");
        cell.className = "arcade-mole-cell";
        grid.appendChild(cell);
        cells.push(cell);
      }

      const stageStartTs = performance.now();
      let round = 0;
      let done = false;
      let correctCount = 0;

      function runRound() {
        if (round >= ROUNDS) return finishStage();
        const cellIndex = Math.floor(Math.random() * GRID_SIZE);
        const isTrap = Math.random() < TRAP_PROBABILITY;
        const cell = cells[cellIndex];
        const mole = document.createElement("div");
        mole.className = "arcade-mole" + (isTrap ? " trap" : " safe");
        cell.appendChild(mole);

        const spawnTs = performance.now();
        let clicked = false;

        function onMoleClick() {
          // Checked first, before anything else touches the DOM: if this
          // mole is already disconnected, the click landed on a stale cached
          // reference from a round that already ended (this is exactly what
          // the Browser Use validation run did — clicked a mole 10.8s after
          // its round had timed out). A live human/CDP click can never fire
          // on an element that isn't currently on screen, so this is a clean,
          // direct signal rather than something to filter out as bad data.
          const stale = !mole.isConnected;
          ctx.setClickTargetRect(stale ? null : mole.getBoundingClientRect(), stale);
          if (stale) return; // this round already resolved via timeout — don't double-record it

          if (clicked) return;
          clicked = true;
          clearTimeout(timeoutId);
          const reactionMs = performance.now() - spawnTs;
          const correct = !isTrap; // clicking a trap is always wrong
          ctx.reactAt(mole, correct); // read before recordRound removes it
          recordRound(correct, isTrap, true, reactionMs);
        }
        mole.addEventListener("click", onMoleClick);

        const timeoutId = setTimeout(() => {
          if (clicked) return;
          mole.remove();
          const correct = isTrap; // not clicking a trap in time is correct (avoided)
          recordRound(correct, isTrap, false, null);
        }, MOLE_VISIBLE_MS);

        function recordRound(correct, wasTrap, wasClicked, reactionMs) {
          if (mole.parentNode) mole.remove();
          if (correct) correctCount += 1;
          ctx.track("stage_result", {
            stage_id: "c4_whack_a_mole",
            stage_tier: "C",
            duration_ms: reactionMs,
            correct,
            extra: { round, was_trap: wasTrap, clicked: wasClicked, reaction_ms: reactionMs },
          });
          round += 1;
          setTimeout(runRound, 120);
        }
      }

      function finishStage() {
        if (done) return;
        done = true;
        ctx.onDone({
          duration_ms: performance.now() - stageStartTs,
          correct: correctCount >= Math.round(ROUNDS * 0.8),
          extra: { correct_count: correctCount, total_rounds: ROUNDS },
          player_points: correctCount * 8,
        });
      }

      runRound();
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
