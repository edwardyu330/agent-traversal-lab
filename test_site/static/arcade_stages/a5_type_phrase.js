(function () {
  "use strict";

  // Two parts, both pure typing-speed (no memorization — the text stays visible
  // the whole time, so latency reflects motor/typing speed, not recall). Feeds
  // ipi_cv and backspace_rate in arcade_metrics.py — both parts tag their
  // key_detail events with the same stage_id ("a5_type_phrase") so that signal
  // aggregates across the whole stage without any change to arcade_metrics.py.
  // Keystroke TIMING and whether a key was backspace is logged, never the
  // actual character, same privacy posture as collector.js's keydown handler.
  // A large pool, not a handful on repeat — a session that types the exact
  // same phrase/word set every run is itself a thing worth noticing, and a
  // small fixed pool made that too likely to happen by chance alone.
  const PHRASES = [
    "the quick fox jumps over lazy dogs",
    "pack my box with five dozen jugs",
    "bright stars fade before the dawn",
    "silent rivers carve the deepest stone",
    "clever foxes outrun the hunting hounds",
    "gentle rain falls on the old roof",
    "sharp winds bend the tall pine trees",
    "warm light spills across the quiet lake",
    "old maps hide the forgotten mountain trail",
    "small sparks drift above the campfire embers",
    "deep shadows stretch across the empty field",
    "loud thunder rolls behind the distant hills",
    "fresh snow covers the narrow forest path",
    "quiet waves lap against the wooden dock",
    "wild geese cross the pale evening sky",
    "cold mist settles over the sleeping town",
    "proud eagles circle above the rocky ridge",
    "soft moss grows along the shaded creek",
    "rough stones line the winding mountain road",
    "early light breaks over the frozen pond",
    "brave sailors chart the stormy northern sea",
    "tiny sparrows nest beneath the barn eaves",
    "heavy fog rolls through the sleepy valley",
    "young saplings bend beneath the fresh snow",
  ];
  const WORD_POOL = [
    "amber", "cinder", "willow", "quartz", "ember", "thistle",
    "harbor", "lantern", "meadow", "granite", "copper", "orchid",
    "falcon", "marble", "cedar", "ripple", "canyon", "velvet",
    "birch", "coral", "flint", "hazel", "ivory", "juniper",
    "linden", "onyx", "pebble", "reed", "saffron", "tundra",
    "violet", "walnut", "yarrow", "zephyr", "basalt", "clover",
    "driftwood", "foxglove", "gravel", "heron", "indigo", "kestrel",
  ];
  const SCATTER_WORD_COUNT = 6;
  const PART1_TIMEOUT_MS = 15000;
  const PART2_TIMEOUT_MS = 12000;

  function pick(arr) {
    return arr[Math.floor(Math.random() * arr.length)];
  }

  function pickN(arr, n) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a.slice(0, n);
  }

  function normalize(s) {
    return s.trim().toLowerCase().replace(/\s+/g, " ");
  }

  function charAccuracy(target, typed) {
    const a = normalize(target), b = normalize(typed);
    const len = Math.max(a.length, b.length);
    if (len === 0) return 1;
    let matches = 0;
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      if (a[i] === b[i]) matches += 1;
    }
    return matches / len;
  }

  window.ARCADE_STAGES.push({
    id: "a5_type_phrase",
    tier: "A",
    title: "Type the Phrase",

    mount(container, ctx) {
      const wrap = document.createElement("div");
      wrap.className = "arcade-stage arcade-stage-a5 fade-in";
      container.appendChild(wrap);

      const stageStartTs = performance.now();

      function onKeydown(e) {
        ctx.track("key_detail", {
          stage_id: "a5_type_phrase",
          is_backspace: e.key === "Backspace",
          is_trusted: e.isTrusted,
        });
      }

      function runPart1(onPart1Done) {
        const phrase = pick(PHRASES);
        // Each character its own span so typing progress can be colored live —
        // purely a local comparison of the input's own value against the phrase
        // already visible on screen, nothing new leaves the browser. Spaces use
        //   so adjacent single-character spans don't collapse the gap.
        const charSpans = phrase.split("").map((c) => `<span>${c === " " ? " " : c}</span>`).join("");
        wrap.innerHTML = `
          <p class="arcade-instruction">Type it as fast as you can</p>
          <p class="arcade-phrase-display">${charSpans}</p>
          <input type="text" class="arcade-type-input" autocomplete="off" autocapitalize="off" spellcheck="false" />
        `;
        const inputEl = wrap.querySelector(".arcade-type-input");
        const spans = wrap.querySelectorAll(".arcade-phrase-display span");
        const startTs = performance.now();
        let done = false;

        function updateCharColors() {
          const typed = inputEl.value;
          spans.forEach((span, i) => {
            if (i >= typed.length) {
              span.className = "";
            } else {
              span.className = typed[i] === phrase[i] ? "char-correct" : "char-wrong";
            }
          });
        }

        function finish(typed, timedOut) {
          if (done) return;
          done = true;
          clearTimeout(timeoutId);
          inputEl.removeEventListener("keydown", onKeydown);
          const accuracy = charAccuracy(phrase, typed);
          const wordCount = phrase.split(" ").length;
          const elapsedMin = (performance.now() - startTs) / 60000;
          const wpm = elapsedMin > 0 ? Math.round(wordCount / elapsedMin) : 0;
          onPart1Done({ accuracy, wpm, timed_out: !!timedOut, duration_ms: performance.now() - startTs });
        }

        inputEl.addEventListener("keydown", onKeydown);
        inputEl.addEventListener("input", updateCharColors);
        inputEl.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            inputEl.classList.add("arcade-input-flash");
            finish(inputEl.value, false);
          }
        });
        inputEl.focus();
        const timeoutId = setTimeout(() => finish(inputEl.value, true), PART1_TIMEOUT_MS);
      }

      function runPart2(onPart2Done) {
        const words = pickN(WORD_POOL, SCATTER_WORD_COUNT);
        const remaining = new Set(words);
        wrap.innerHTML = `
          <p class="arcade-instruction">Type all the scattered words</p>
          <div class="arcade-scatter-field"></div>
          <input type="text" class="arcade-type-input" autocomplete="off" autocapitalize="off" spellcheck="false" />
        `;
        const field = wrap.querySelector(".arcade-scatter-field");
        const inputEl = wrap.querySelector(".arcade-type-input");
        const wordEls = {};
        const positions = pickN(
          [[8, 15], [55, 10], [30, 45], [70, 55], [12, 75], [60, 80], [40, 20], [80, 30]],
          words.length
        );
        words.forEach((w, i) => {
          const el = document.createElement("span");
          el.className = "arcade-scatter-word";
          el.textContent = w;
          el.style.left = positions[i][0] + "%";
          el.style.top = positions[i][1] + "%";
          field.appendChild(el);
          wordEls[w] = el;
        });

        const startTs = performance.now();
        let done = false;

        function finish(timedOut) {
          if (done) return;
          done = true;
          clearTimeout(timeoutId);
          inputEl.removeEventListener("keydown", onKeydown);
          onPart2Done({
            found_count: words.length - remaining.size,
            total_count: words.length,
            timed_out: !!timedOut,
            duration_ms: performance.now() - startTs,
          });
        }

        inputEl.addEventListener("keydown", onKeydown);
        inputEl.addEventListener("keydown", (e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          const typed = normalize(inputEl.value);
          inputEl.value = "";
          if (remaining.has(typed)) {
            remaining.delete(typed);
            wordEls[typed].classList.add("found");
            inputEl.classList.remove("arcade-input-flash");
            void inputEl.offsetWidth; // restart the animation if it's still mid-flash from the previous word
            inputEl.classList.add("arcade-input-flash");
            if (remaining.size === 0) finish(false);
          }
        });
        inputEl.focus();
        const timeoutId = setTimeout(() => finish(true), PART2_TIMEOUT_MS);
      }

      runPart1((part1) => {
        runPart2((part2) => {
          const overallCorrect = part1.accuracy > 0.8 && part2.found_count === part2.total_count;
          ctx.onDone({
            duration_ms: performance.now() - stageStartTs,
            correct: overallCorrect,
            extra: { part1, part2 },
            player_points: Math.round(part1.accuracy * 20) + part2.found_count * 5,
          });
        });
      });
    },

    cleanup(container) {
      container.innerHTML = "";
    },
  });
})();
