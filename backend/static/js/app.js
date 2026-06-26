/* Pitch Prepper — frontend logic.
   Single-page app served by Flask at `/`. Switches between Home / Results /
   Leaderboard views client-side and talks to the backend JSON API:
     /health, /api/me, /api/leaderboard, /analyze (+ /analyze/status/<id>),
     /api/ideal-delivery, /api/login, /api/register, /api/logout.
   Visual design = Stitch (Tailwind); all behavior wired here. */
(() => {
  const $ = (id) => document.getElementById(id);

  // ---- Semantic colors (match DESIGN.md: good ≥75 emerald, fair 55–74 amber,
  //      poor <55 rose, unknown neutral) -------------------------------------
  const C = {
    primary: "#3525cd",
    primaryContainer: "#4f46e5",
    good: "#006c49",
    fair: "#b45309",
    poor: "#ba1a1a",
    neutral: "#777587",
    axis: "#464555",
    grid: "#e4e1ee",
  };
  const scoreColor = (s) =>
    s == null ? C.neutral : s >= 75 ? C.good : s >= 55 ? C.fair : C.poor;
  const scoreLabel = (s) =>
    s == null
      ? ""
      : s >= 85
        ? "Excellent"
        : s >= 75
          ? "Strong"
          : s >= 55
            ? "Fair"
            : "Needs work";

  // ---- App state -----------------------------------------------------------
  let selectedBlob = null;
  let selectedName = null;
  let mediaRecorder = null;
  let chunks = [];
  let recordStart = 0;
  let minRecordingSec = 15;
  let charts = {};
  let chartConfigs = {};
  let lastResult = null;
  const authState = { user: null, dbAvailable: false };

  // =========================================================================
  // View switching
  // =========================================================================
  function showView(name) {
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    const el = $("view-" + name);
    if (el) el.classList.remove("hidden");
    document.querySelectorAll(".nav-link").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name),
    );
    if (name === "results") {
      const has = !!lastResult;
      $("resultsEmpty").classList.toggle("hidden", has);
      $("resultsContent").classList.toggle("hidden", !has);
    }
    if (name === "leaderboard") loadLeaderboard();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-view]");
    if (t) showView(t.dataset.view);
  });

  // =========================================================================
  // Input: upload + record
  // =========================================================================
  $("fileInput").addEventListener("change", (e) => {
    if (e.target.files.length) {
      selectedBlob = e.target.files[0];
      selectedName = e.target.files[0].name;
      $("recordStatus").textContent = "Ready to record";
      updateSelection();
    }
  });

  $("recordBtn").addEventListener("click", async () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (ev) => chunks.push(ev.data);
      mediaRecorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecordingUI(false);
        const elapsed = (Date.now() - recordStart) / 1000;
        if (elapsed < minRecordingSec) {
          selectedBlob = null;
          selectedName = null;
          $("recordStatus").textContent = `Only ${elapsed.toFixed(0)}s — record at least ${minRecordingSec}s.`;
          updateSelection();
          return;
        }
        selectedBlob = new Blob(chunks, { type: "audio/webm" });
        selectedName = "recording.webm";
        $("recordStatus").textContent = `Recorded ${elapsed.toFixed(0)}s ✓`;
        updateSelection();
      };
      mediaRecorder.start();
      recordStart = Date.now();
      setRecordingUI(true);
      $("recordStatus").textContent = `Recording… (min ${minRecordingSec}s)`;
    } catch {
      showError("Microphone access denied or unavailable.");
    }
  });

  function setRecordingUI(on) {
    const btn = $("recordBtn");
    $("recordBtnLabel").textContent = on ? "Stop Recording" : "Start Recording";
    btn.classList.toggle("animate-pulse", on);
  }

  function updateSelection() {
    $("selected").textContent = selectedName ? `Selected: ${selectedName}` : "";
    $("analyzeBtn").disabled = !selectedBlob;
  }

  // =========================================================================
  // Analyze flow (async submit + poll)
  // =========================================================================
  $("analyzeBtn").addEventListener("click", async () => {
    if (!selectedBlob) return;
    hide("error");
    $("inputArea").classList.add("hidden");
    show("loading");
    $("analyzeBtn").disabled = true;

    const form = new FormData();
    form.append("audio", selectedBlob, selectedName);
    setLoadingMsg("Uploading…");

    try {
      const resp = await fetch("/analyze", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        endLoading();
        showError(data.error || `Request failed (${resp.status}).`);
        return;
      }
      const result = await pollAnalysis(data.job_id);
      endLoading();
      if (!result) {
        showError("Analysis timed out. Please try again.");
      } else if (result.error) {
        showError(result.error);
      } else {
        lastResult = result;
        render(result);
        showView("results");
        handleSaved(result);
      }
    } catch (err) {
      endLoading();
      showError("Network error: " + err.message);
    } finally {
      $("analyzeBtn").disabled = false;
    }
  });

  function endLoading() {
    hide("loading");
    $("inputArea").classList.remove("hidden");
  }

  async function pollAnalysis(jobId) {
    const started = Date.now();
    const MAX_MS = 20 * 60 * 1000;
    let consecutiveErrors = 0;
    while (Date.now() - started < MAX_MS) {
      await sleep(2000);
      setLoadingMsg(
        `Transcribing and analyzing… (${Math.round((Date.now() - started) / 1000)}s)`,
      );
      let d;
      try {
        const r = await fetch(`/analyze/status/${jobId}`);
        if (r.status === 404) {
          d = await r.json().catch(() => ({}));
          return { error: d.error || "Analysis job expired. Please try again." };
        }
        d = await r.json();
        consecutiveErrors = 0;
      } catch (err) {
        if (++consecutiveErrors > 30) throw err;
        continue;
      }
      if (d.state === "done") return d.result;
      if (d.state === "error") return { error: d.error };
    }
    return null;
  }

  function handleSaved(d) {
    const note = $("savedNote");
    if (d.saved_to_leaderboard) {
      const score =
        d.scores && d.scores.overall != null ? Math.round(d.scores.overall) : "?";
      note.textContent = `✓ Saved to the leaderboard — you scored ${score}.`;
      note.classList.remove("hidden");
      loadLeaderboard();
    } else if (!authState.user && authState.dbAvailable) {
      note.textContent = "ℹ Log in to save this score to the leaderboard.";
      note.classList.remove("hidden");
    } else {
      note.classList.add("hidden");
    }
  }

  // =========================================================================
  // Rendering
  // =========================================================================
  function render(d) {
    renderHeader(d);
    renderScores(d.scores);
    renderMetrics(d);
    renderFeedback(d.feedback);
    renderIdeal(d);
    renderCharts(d);
    renderContent(d.content);
    renderLanguage(d.language);
    $("transcript").textContent = d.transcript || "";
    $("warnings").textContent = (d.warnings || []).join("  ·  ");
  }

  function renderHeader(d) {
    const lang = d.language_detected ? d.language_detected.toUpperCase() : "";
    $("resultsSubtitle").textContent = lang ? `Detected language: ${lang}` : "";
    const chips = [];
    if (d.duration_sec != null) chips.push(chip("schedule", fmtDuration(d.duration_sec)));
    if (d.word_count != null) chips.push(chip("mic", `${d.word_count} words`));
    $("resultsMetaChips").innerHTML = chips.join("");
  }
  const chip = (icon, text) =>
    `<span class="inline-flex items-center gap-1 px-3 py-1 bg-surface-container text-on-surface text-xs font-label tracking-wide rounded-full"><span class="material-symbols-outlined text-[16px]">${icon}</span>${esc(text)}</span>`;

  function renderScores(s) {
    if (!s) return;
    const o = s.overall;
    $("overallScore").textContent = o != null ? Math.round(o) : "–";
    const lbl = $("overallLabel");
    lbl.textContent = scoreLabel(o);
    lbl.style.color = scoreColor(o);
    const C2 = 2 * Math.PI * 45; // 282.74
    const arc = $("gaugeArc");
    arc.style.stroke = scoreColor(o);
    arc.style.strokeDashoffset = String(C2 * (1 - (o || 0) / 100));

    const rows = [
      ["Delivery", s.delivery, "record_voice_over"],
      ["Language", s.language, "language"],
      ["Content", s.content, "article"],
    ];
    $("subScores").innerHTML = rows
      .map(
        ([name, val, icon]) => `
      <div>
        <div class="flex justify-between items-end mb-2">
          <span class="font-body font-medium text-on-surface flex items-center gap-2">
            <span class="material-symbols-outlined text-primary text-[20px]">${icon}</span>${name}
          </span>
          <span class="font-body text-on-surface-variant">${val != null ? Math.round(val) : "–"}/100</span>
        </div>
        <div class="h-2 w-full bg-surface-container rounded-full overflow-hidden">
          <div class="h-full rounded-full transition-all duration-700" style="width:${val || 0}%;background:${scoreColor(val)}"></div>
        </div>
      </div>`,
      )
      .join("");
  }

  function renderMetrics(d) {
    const dl = d.delivery || {},
      r = dl.rate || {},
      p = dl.pitch || {},
      v = dl.volume || {},
      f = dl.fillers || {},
      pa = dl.pauses || {};
    const cards = [
      [r.wpm ?? "–", "Words / min"],
      [p.variability_score ?? "–", "Pitch variation"],
      [v.consistency_score ?? "–", "Volume consistency"],
      [f.total ?? "–", "Filler words"],
      [pa.score ?? "–", "Pause quality"],
      [d.content && d.content.score != null ? d.content.score : "–", "Structure score"],
      [d.duration_sec ? (d.duration_sec / 60).toFixed(1) : "–", "Minutes"],
      [d.word_count ?? "–", "Total words"],
    ];
    $("metrics").innerHTML = cards
      .map(
        ([val, lbl]) => `
      <div class="bg-surface-container-lowest rounded-xl border border-outline-variant shadow-sm p-4">
        <div class="font-label tracking-wide text-on-surface-variant uppercase text-xs">${esc(lbl)}</div>
        <div class="font-metric font-bold text-3xl text-on-surface mt-1">${esc(String(val))}</div>
      </div>`,
      )
      .join("");
  }

  function renderFeedback(fb) {
    if (!fb) return;
    const badge = ["bg-error-container text-on-error-container", "bg-tertiary-fixed text-on-tertiary-fixed", "bg-primary-fixed text-on-primary-fixed"];
    $("topRecs").innerHTML = (fb.top_recommendations || [])
      .map(
        (rec, i) => `
      <div class="p-4 bg-surface-container-low rounded-lg border border-outline-variant flex items-start gap-3">
        <div class="w-8 h-8 rounded-full ${badge[i] || badge[2]} flex items-center justify-center shrink-0 font-bold text-sm">${i + 1}</div>
        <p class="font-body text-sm text-on-surface-variant leading-relaxed">${esc(rec)}</p>
      </div>`,
      )
      .join("");

    const li = (txt, icon, color) =>
      `<li class="flex items-start gap-2 font-body text-sm text-on-surface-variant"><span class="material-symbols-outlined text-[18px] ${color} mt-0.5">${icon}</span><span>${esc(txt)}</span></li>`;
    $("strengths").innerHTML =
      (fb.strengths || []).map((t) => li(t, "check", "text-secondary")).join("") ||
      li("Solid delivery overall.", "check", "text-secondary");
    $("improvements").innerHTML =
      (fb.improvements || []).map((t) => li(t, "arrow_upward", "text-tertiary")).join("") ||
      `<li class="font-body text-sm text-on-surface-variant">None — nice work.</li>`;
  }

  function renderContent(c) {
    if (!c) return;
    $("contentSummary").textContent =
      (c.summary || "") + (c.method ? `  (method: ${c.method})` : "");
    const cats = c.categories || {};
    $("contentCats").innerHTML = Object.entries(cats)
      .map(
        ([name, info]) => `
      <div class="p-4 bg-surface-container-low rounded-lg border border-outline-variant">
        <div class="flex justify-between items-center mb-1">
          <span class="font-headline font-semibold text-on-surface capitalize">${esc(name)}</span>
          <span class="font-metric font-bold text-lg" style="color:${scoreColor(info.score)}">${info.score ?? "–"}</span>
        </div>
        <p class="font-body text-sm text-on-surface-variant">${esc(info.feedback || "")}</p>
      </div>`,
      )
      .join("");
  }

  function renderLanguage(lg) {
    if (!lg) return;
    const tr = lg.transitions || {},
      bz = lg.buzzwords || {},
      rp = lg.repetition || {},
      kw = lg.keywords || {};
    const pill = (k, v) =>
      `<span class="inline-block px-3 py-1 m-0.5 rounded-full bg-primary-fixed text-on-primary-fixed text-xs font-label">${esc(k)}${v != null ? " ×" + v : ""}</span>`;
    const pills = (obj) => {
      const e = Object.entries(obj || {});
      return e.length
        ? e.map(([k, v]) => pill(k, v)).join("")
        : `<span class="font-body text-sm text-on-surface-variant">none</span>`;
    };
    const h3 = (t, extra = "") =>
      `<h3 class="font-headline font-semibold text-on-surface mt-stack-md first:mt-0">${t}${extra}</h3>`;

    const left = `<div>
      ${h3(`Transitions used (${tr.total ?? 0})`)}
      <div class="mt-1">${pills(tr.by_phrase)}</div>
      ${h3("Keywords reinforced")}
      <div class="mt-1">${(kw.keywords || []).length ? (kw.keywords || []).map((k) => pill(k.word, k.count)).join("") : `<span class="font-body text-sm text-on-surface-variant">none</span>`}</div>
    </div>`;

    const sugg = Object.entries(bz.suggestions || {});
    const supp = Object.keys(bz.suppressed || {});
    const right = `<div>
      ${h3("Buzzwords flagged", ` <span class="font-body font-normal text-xs text-on-surface-variant">(advisory — doesn't affect your score)</span>`)}
      <div class="mt-1">${pills(bz.by_word)}</div>
      ${sugg.length ? `<p class="font-body text-xs text-on-surface-variant mt-2">Try: ${sugg.map(([b, a]) => `<b>${esc(b)}</b> → ${esc(a)}`).join("; ")}</p>` : ""}
      ${supp.length ? `<p class="font-body text-xs text-on-surface-variant mt-1">Not flagged — used appropriately in context: ${supp.map(esc).join(", ")}</p>` : ""}
      ${h3("Repeated words / phrases")}
      <div class="mt-1">${pills(Object.assign({}, rp.repeated_words, rp.repeated_phrases))}</div>
    </div>`;
    $("languageDetails").innerHTML = left + right;
  }

  // =========================================================================
  // Ideal delivery (ElevenLabs TTS)
  // =========================================================================
  let idealTranscript = "";
  let idealSentences = [];
  let idealSelected = new Set();

  function splitSentences(text) {
    return (text.match(/[\s\S]*?[.!?]+(?=\s|$)|[\s\S]+$/g) || [text])
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function renderIdeal(d) {
    resetIdeal();
    idealTranscript = d.transcript || "";
    idealSentences = idealTranscript ? splitSentences(idealTranscript) : [];
    idealSelected = new Set();
    renderIdealPicker();
    updateIdealSelInfo();
    $("idealCard").classList.toggle(
      "hidden",
      !(d.ideal_delivery_available && idealTranscript),
    );
  }

  function renderIdealPicker() {
    const picker = $("idealPicker");
    picker.innerHTML = "";
    idealSentences.forEach((s, i) => {
      const span = document.createElement("span");
      span.className = "sent";
      span.textContent = s + " ";
      span.addEventListener("click", () => {
        if (idealSelected.has(i)) {
          idealSelected.delete(i);
          span.classList.remove("selected");
        } else {
          idealSelected.add(i);
          span.classList.add("selected");
        }
        updateIdealSelInfo();
      });
      picker.appendChild(span);
    });
  }

  const idealSelectedText = () =>
    [...idealSelected].sort((a, b) => a - b).map((i) => idealSentences[i]).join(" ");

  function updateIdealSelInfo() {
    const n = idealSelected.size;
    if (!n) {
      $("idealSelInfo").textContent = "Improving: whole talk";
      hide("idealClear");
    } else {
      const words = idealSelectedText().split(/\s+/).filter(Boolean).length;
      $("idealSelInfo").textContent = `Improving: ${n} selected sentence${n > 1 ? "s" : ""} (~${words} words)`;
      show("idealClear");
    }
  }

  function resetIdeal() {
    hide("idealOutput");
    $("idealStatus").textContent = "";
    $("idealScript").textContent = "";
    const audio = $("idealAudio");
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    $("idealBtn").disabled = false;
    $("idealBtnLabel").textContent = "Generate ideal delivery";
  }

  async function generateIdeal() {
    const text = idealSelected.size ? idealSelectedText() : idealTranscript;
    if (!text) return;
    const btn = $("idealBtn");
    btn.disabled = true;
    const scope = idealSelected.size ? "selected part" : "whole talk";
    $("idealStatus").textContent = ` Rewriting the ${scope} and synthesizing…`;
    try {
      const r = await fetch("/api/ideal-delivery", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: text }),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        $("idealStatus").textContent = " " + (d.error || `Request failed (${r.status}).`);
        btn.disabled = false;
        return;
      }
      $("idealScript").textContent = d.script || "";
      const audio = $("idealAudio");
      const fromHeuristic = (d.method || "").startsWith("heuristic");
      if (d.audio) {
        audio.src = d.audio;
        audio.classList.remove("hidden");
        $("idealStatus").textContent = fromHeuristic
          ? " Ready — script cleaned without the LLM (set GEMINI_API_KEY for a fuller rewrite)."
          : " Ready — press play.";
      } else {
        audio.classList.add("hidden");
        $("idealStatus").textContent = " " + (d.audio_error || d.note || "Script ready (no audio).");
      }
      show("idealOutput");
      $("idealBtnLabel").textContent = "Regenerate";
      btn.disabled = false;
    } catch (err) {
      $("idealStatus").textContent = " Network error: " + err.message;
      btn.disabled = false;
    }
  }
  $("idealBtn").addEventListener("click", generateIdeal);
  $("idealClear").addEventListener("click", () => {
    idealSelected.clear();
    $("idealPicker").querySelectorAll(".sent.selected").forEach((s) => s.classList.remove("selected"));
    updateIdealSelInfo();
  });

  // =========================================================================
  // Charts
  // =========================================================================
  function destroyCharts() {
    Object.values(charts).forEach((c) => c && c.destroy());
    charts = {};
  }
  const baseOpts = () => ({
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: C.axis }, grid: { color: C.grid } },
      y: { ticks: { color: C.axis }, grid: { color: C.grid } },
    },
  });
  function mkChart(id, config) {
    chartConfigs[id] = structuredClone(config);
    return new Chart($(id), config);
  }

  function renderCharts(d) {
    destroyCharts();
    const dl = d.delivery || {};

    const wpm = (dl.rate && dl.rate.timeline) || [];
    charts.wpm = mkChart("wpmChart", {
      type: "line",
      data: {
        labels: wpm.map((p) => p.t + "s"),
        datasets: [
          {
            data: wpm.map((p) => p.wpm),
            borderColor: C.primary,
            backgroundColor: "rgba(53,37,205,.12)",
            fill: true,
            tension: 0.3,
            pointBackgroundColor: wpm.map((p) =>
              p.label === "too_fast" ? C.poor : p.label === "too_slow" ? C.fair : C.primary,
            ),
          },
        ],
      },
      options: baseOpts(),
    });

    const pitch = ((dl.pitch && dl.pitch.timeline) || []).filter((p) => p.hz != null);
    charts.pitch = mkChart("pitchChart", {
      type: "line",
      data: {
        labels: pitch.map((p) => p.t),
        datasets: [{ data: pitch.map((p) => p.hz), borderColor: C.good, pointRadius: 0, tension: 0.3 }],
      },
      options: baseOpts(),
    });

    const vol = (dl.volume && dl.volume.timeline) || [];
    charts.volume = mkChart("volumeChart", {
      type: "line",
      data: {
        labels: vol.map((p) => p.t),
        datasets: [
          {
            data: vol.map((p) => p.db),
            borderColor: C.primaryContainer,
            backgroundColor: "rgba(79,70,229,.15)",
            fill: true,
            pointRadius: 0,
            tension: 0.3,
          },
        ],
      },
      options: baseOpts(),
    });

    const pauses = (dl.pauses && dl.pauses.timeline) || [];
    const colorOf = (t) =>
      ({ strategic: C.good, long_awkward: C.poor, hesitation: C.fair, normal: C.primary })[t] || C.primary;
    charts.pause = mkChart("pauseChart", {
      type: "scatter",
      data: {
        datasets: [
          {
            data: pauses.map((p) => ({ x: p.t, y: p.duration })),
            backgroundColor: pauses.map((p) => colorOf(p.type)),
            pointRadius: 5,
          },
        ],
      },
      options: Object.assign(baseOpts(), {
        scales: {
          x: { title: { display: true, text: "time (s)", color: C.axis }, ticks: { color: C.axis }, grid: { color: C.grid } },
          y: { title: { display: true, text: "pause (s)", color: C.axis }, ticks: { color: C.axis }, grid: { color: C.grid } },
        },
      }),
    });

    const fillerTop = Object.entries((dl.fillers && dl.fillers.by_word) || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
    const fillerMax = fillerTop.length ? fillerTop[0][1] : 1;
    charts.filler = mkChart("fillerChart", {
      type: "bar",
      data: {
        labels: fillerTop.map(([w]) => w),
        datasets: [{ data: fillerTop.map(([, c]) => c), backgroundColor: C.poor, borderRadius: 4, maxBarThickness: 44 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: C.axis }, grid: { display: false } },
          y: { beginAtZero: true, suggestedMax: fillerMax + 1, ticks: { color: C.axis, precision: 0, stepSize: 1 }, grid: { color: C.grid } },
        },
      },
    });

    const cats = (d.content && d.content.categories) || {};
    charts.content = mkChart("contentChart", {
      type: "radar",
      data: {
        labels: Object.keys(cats),
        datasets: [{ data: Object.values(cats).map((c) => c.score || 0), borderColor: C.primary, backgroundColor: "rgba(53,37,205,.2)" }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          r: {
            min: 0,
            max: 100,
            ticks: { color: C.axis, backdropColor: "transparent" },
            grid: { color: C.grid },
            angleLines: { color: C.grid },
            pointLabels: { color: "#1b1b24" },
          },
        },
      },
    });
  }

  // ---- Click-to-enlarge ----------------------------------------------------
  let modalChart = null;
  function openChartModal(canvasId, title) {
    const cfg = chartConfigs[canvasId];
    if (!cfg) return;
    const clone = structuredClone(cfg);
    clone.options = Object.assign({}, clone.options, { responsive: true, maintainAspectRatio: false });
    $("chartModalTitle").textContent = title || "";
    $("chartModal").classList.remove("hidden");
    if (modalChart) modalChart.destroy();
    modalChart = new Chart($("chartModalCanvas"), clone);
  }
  function closeChartModal() {
    $("chartModal").classList.add("hidden");
    if (modalChart) {
      modalChart.destroy();
      modalChart = null;
    }
  }
  function wireChartZoom() {
    document.querySelectorAll(".chart-box").forEach((box) => {
      box.addEventListener("click", () => {
        const canvas = box.querySelector("canvas");
        const title = (box.querySelector("h3") || {}).textContent || "";
        if (canvas && canvas.id) openChartModal(canvas.id, title.trim());
      });
    });
  }

  // =========================================================================
  // Auth
  // =========================================================================
  let authMode = "login";

  async function loadMe() {
    try {
      const d = await (await fetch("/api/me")).json();
      authState.user = d.user;
      authState.dbAvailable = d.db_available;
    } catch {
      authState.user = null;
      authState.dbAvailable = false;
    }
    renderAuthbar();
  }

  function renderAuthbar() {
    const bar = $("authbar");
    if (authState.user) {
      bar.innerHTML =
        `<span class="hidden sm:inline-flex items-center gap-1 font-label text-sm text-on-surface"><span class="material-symbols-outlined text-[20px] text-primary">account_circle</span>${esc(authState.user.username)}</span>` +
        `<button id="logoutBtn" class="px-4 py-2 text-label tracking-wide text-primary border border-outline-variant rounded-full hover:bg-surface-container-low transition-colors">Log out</button>`;
      $("logoutBtn").addEventListener("click", logout);
    } else if (!authState.dbAvailable) {
      bar.innerHTML = "";
    } else {
      bar.innerHTML =
        `<button id="openLogin" class="px-4 py-2 text-label tracking-wide text-primary border border-outline-variant rounded-full hover:bg-surface-container-low transition-colors">Login</button>` +
        `<button id="openRegister" class="px-4 py-2 text-label tracking-wide bg-primary text-on-primary rounded-full hover:bg-primary-container transition-colors shadow-sm">Sign Up</button>`;
      $("openLogin").addEventListener("click", () => openAuth("login"));
      $("openRegister").addEventListener("click", () => openAuth("register"));
    }
  }

  function openAuth(mode) {
    if (!authState.dbAvailable) {
      alert("Accounts are unavailable — the server can't reach the database right now.");
      return;
    }
    setAuthMode(mode);
    hide("authError");
    $("authForm").reset();
    $("authModal").classList.remove("hidden");
  }
  const closeAuth = () => $("authModal").classList.add("hidden");

  function setAuthMode(mode) {
    authMode = mode;
    const on = "text-primary border-primary";
    const off = "text-on-surface-variant border-transparent";
    $("tabLogin").className = `flex-1 py-3 text-center font-label tracking-wide border-b-2 ${mode === "login" ? on : off}`;
    $("tabRegister").className = `flex-1 py-3 text-center font-label tracking-wide border-b-2 ${mode === "register" ? on : off}`;
    $("authEmailWrap").classList.toggle("hidden", mode !== "register");
    $("authSubmit").textContent = mode === "login" ? "Log in" : "Create account";
  }

  async function submitAuth(e) {
    e.preventDefault();
    const body = { username: $("authUsername").value.trim(), password: $("authPassword").value };
    if (authMode === "register") body.email = $("authEmail").value.trim();
    try {
      const r = await fetch(`/api/${authMode === "login" ? "login" : "register"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        const err = $("authError");
        err.textContent = d.error || "Failed.";
        err.classList.remove("hidden");
        return;
      }
      authState.user = d.user;
      closeAuth();
      renderAuthbar();
      loadLeaderboard();
    } catch (err) {
      const e2 = $("authError");
      e2.textContent = "Network error: " + err.message;
      e2.classList.remove("hidden");
    }
  }

  async function logout() {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch {}
    authState.user = null;
    renderAuthbar();
    loadLeaderboard();
  }

  // =========================================================================
  // Leaderboard (full view + home preview)
  // =========================================================================
  async function loadLeaderboard() {
    let d;
    try {
      d = await (await fetch("/api/leaderboard")).json();
    } catch {
      d = { error: "network" };
    }
    const rows = (d && d.leaderboard) || [];
    const available = !(d && d.error);

    // Home preview card
    const homeCard = $("homeLeaderboardCard");
    if (available && rows.length) {
      homeCard.classList.remove("hidden");
      $("homeLeaderboardBody").innerHTML = rows
        .slice(0, 3)
        .map(
          (row) => `
        <tr class="border-b border-outline-variant/40">
          <td class="py-2 font-body font-semibold text-primary">${row.rank}</td>
          <td class="py-2 font-body">${esc(row.username || "—")}</td>
          <td class="py-2 font-body font-bold text-right" style="color:${scoreColor(row.best_score)}">${row.best_score ?? "—"}</td>
        </tr>`,
        )
        .join("");
      $("homeLeaderboardNote").textContent = "";
    } else {
      homeCard.classList.add("hidden");
    }

    // Full leaderboard view
    const tableWrap = $("leaderboardTableWrap");
    const unavail = $("leaderboardUnavailable");
    if (!available) {
      tableWrap.classList.add("hidden");
      unavail.classList.remove("hidden");
      $("leaderboardNote").textContent = "Sign in (and connect a database) to compete on the global leaderboard.";
      return;
    }
    unavail.classList.add("hidden");
    tableWrap.classList.remove("hidden");
    if (!rows.length) {
      $("leaderboardNote").textContent = "No scores yet — be the first to get on the board!";
      $("leaderboardBody").innerHTML = "";
      return;
    }
    $("leaderboardNote").textContent = "Top scores across everyone — each user's best run.";
    $("leaderboardBody").innerHTML = rows
      .map(
        (row) => `
      <tr class="${row.is_me ? "bg-primary-fixed/30 border-l-4 border-primary" : "hover:bg-surface-container-lowest"} transition-colors">
        <td class="py-4 px-6 font-body font-medium">${row.rank}</td>
        <td class="py-4 px-6 font-body text-on-surface ${row.is_me ? "font-bold" : ""}">${esc(row.username || "—")}${row.is_me ? " (you)" : ""}</td>
        <td class="py-4 px-6 text-right font-body font-bold" style="color:${scoreColor(row.best_score)}">${row.best_score ?? "—"}</td>
        <td class="py-4 px-6 text-right font-body text-on-surface-variant">${row.attempts}</td>
      </tr>`,
      )
      .join("");
  }

  async function loadConfig() {
    try {
      const d = await (await fetch("/health")).json();
      if (typeof d.min_recording_sec === "number") minRecordingSec = d.min_recording_sec;
    } catch {
      /* keep default */
    }
  }

  // =========================================================================
  // Wire-up + initial load
  // =========================================================================
  $("authClose").addEventListener("click", closeAuth);
  document.querySelectorAll("[data-close-auth]").forEach((el) => el.addEventListener("click", closeAuth));
  $("tabLogin").addEventListener("click", () => setAuthMode("login"));
  $("tabRegister").addEventListener("click", () => setAuthMode("register"));
  $("authForm").addEventListener("submit", submitAuth);

  $("chartModalClose").addEventListener("click", closeChartModal);
  document.querySelectorAll("[data-close-chart]").forEach((el) => el.addEventListener("click", closeChartModal));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeChartModal();
      closeAuth();
    }
  });

  wireChartZoom();
  showView("home");
  loadConfig();
  loadMe();
  loadLeaderboard();

  // ---- Helpers -------------------------------------------------------------
  function show(id) {
    $(id).classList.remove("hidden");
  }
  function hide(id) {
    $(id).classList.add("hidden");
  }
  function showError(msg) {
    $("errorText").textContent = msg;
    show("error");
    showView("home");
  }
  function setLoadingMsg(msg) {
    const el = $("loadingMsg");
    if (el) el.textContent = msg;
  }
  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }
  function fmtDuration(sec) {
    const s = Math.round(sec || 0);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  function esc(s) {
    return String(s).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }
})();
