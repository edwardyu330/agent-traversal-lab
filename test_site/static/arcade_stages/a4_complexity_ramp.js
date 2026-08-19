(function () {
  "use strict";

  // Same simple click task, repeated at escalating visual density. The signal
  // isn't raw latency — it's the SHAPE of latency vs. complexity: humans slope
  // with visual search difficulty, an LLM-driven agent slopes with per-step
  // inference cost (screenshot + reasoning), a dumb script's latency stays flat
  // regardless of how cluttered the field gets. arcade_metrics.py fits a simple
  // linear regression across levels to get latency_complexity_slope.
  const LEVELS = [4, 9, 16, 25]; // distractor-field sizes (total items per level)
  const TIMEOUT_MS = 6000;
  const MAX_WRONG_ATTEMPTS_PER_LEVEL = 4;
  const PENALTY_PER_WRONG = 3;

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  window.ARCADE_STAGES.push({
    id: "a4_complexity_ramp",
    tier: "A",
    title: "Complexity Ramp",

    mount(container, ctx) {
      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-a4 fade-in";
      wrap.innerHTML = `
        <p class="arcade-instruction">Click the odd one out</p>
        <div class="arcade-ramp-grid"></div>
      `;
      container.appendChild(wrap);
      const grid = wrap.querySelector(".arcade-ramp-grid");

      const stageStartTs = performance.now();
      const levelResults = [];
      let levelIndex = 0;
      let levelWrongAttempts = 0;
      let done = false;
      let timeoutId = null;

      function runLevel() {
        if (levelIndex >= LEVELS.length) return finishStage();
        const count = LEVELS[levelIndex];
        grid.innerHTML = "";
        grid.style.gridTemplateColumns = `repeat(${Math.ceil(Math.sqrt(count))}, 1fr)`;

        const targetPos = Math.floor(Math.random() * count);
        const levelStartTs = performance.now();

        for (let i = 0; i < count; i++) {
          const cell = document.createElement("div");
          cell.className = "arcade-ramp-cell" + (i === targetPos ? " target" : "");
          cell.addEventListener("click", () => {
            ctx.setClickTargetRect(cell.isConnected ? cell.getBoundingClientRect() : null, !cell.isConnected);
            ctx.reactAt(cell, i === targetPos);
            onCellClick(i === targetPos, count, levelStartTs);
          });
          grid.appendChild(cell);
        }

        timeoutId = setTimeout(() => onCellClick(false, count, levelStartTs, true), TIMEOUT_MS);
      }

      // A wrong click (or timeout) re-rolls the SAME level instead of
      // advancing — the level only counts as done, and only feeds
      // levelResults (what latency_complexity_slope regresses over), once
      // gotten right or once MAX_WRONG_ATTEMPTS_PER_LEVEL is spent. A
      // spam-clicker can't just click anything and ride the ramp forward.
      function onCellClick(correct, distractorCount, levelStartTs, timedOut) {
        if (done) return;
        clearTimeout(timeoutId);
        if (correct) {
          levelResults.push({
            distractor_count: distractorCount,
            latency_ms: performance.now() - levelStartTs,
            correct: true,
            timed_out: false,
            wrong_attempts: levelWrongAttempts,
          });
          levelIndex += 1;
          levelWrongAttempts = 0;
          runLevel();
          return;
        }
        levelWrongAttempts += 1;
        ctx.track("stage_result", {
          stage_id: "a4_complexity_ramp",
          stage_tier: "A",
          duration_ms: performance.now() - levelStartTs,
          correct: false,
          extra: { distractor_count: distractorCount, timed_out: !!timedOut, attempt: levelWrongAttempts, level_index: levelIndex },
        });
        if (levelWrongAttempts >= MAX_WRONG_ATTEMPTS_PER_LEVEL) {
          levelResults.push({
            distractor_count: distractorCount,
            latency_ms: performance.now() - levelStartTs,
            correct: false,
            timed_out: !!timedOut,
            gave_up: true,
            wrong_attempts: levelWrongAttempts,
          });
          levelIndex += 1;
          levelWrongAttempts = 0;
        }
        runLevel();
      }

      function finishStage() {
        if (done) return;
        done = true;
        const correctCount = levelResults.filter((r) => r.correct).length;
        const totalWrongAttempts = levelResults.reduce((sum, r) => sum + (r.wrong_attempts || 0), 0);
        ctx.onDone({
          duration_ms: performance.now() - stageStartTs,
          correct: correctCount === levelResults.length,
          extra: { levels: levelResults },
          player_points: Math.max(0, correctCount * 10 - totalWrongAttempts * PENALTY_PER_WRONG),
        });
      }

      runLevel();
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
