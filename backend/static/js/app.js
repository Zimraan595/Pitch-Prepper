/* Pitch Prepper — frontend logic */
(() => {
  const $ = (id) => document.getElementById(id);

  let selectedBlob = null;
  let selectedName = null;
  let mediaRecorder = null;
  let chunks = [];
  let charts = {};
  let chartConfigs = {}; // canvas id -> Chart config (for click-to-enlarge)
  let recordStart = 0; // ms timestamp when recording began
  let minRecordingSec = 15; // overwritten from /health

  const fileInput = $("fileInput");
  const recordBtn = $("recordBtn");
  const analyzeBtn = $("analyzeBtn");

  // ---- Input handling ------------------------------------------------------
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) {
      selectedBlob = fileInput.files[0];
      selectedName = fileInput.files[0].name;
      updateSelection();
    }
  });

  recordBtn.addEventListener("click", async () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
      mediaRecorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        recordBtn.textContent = "● Start recording";
        recordBtn.classList.remove("recording");
        const elapsed = (Date.now() - recordStart) / 1000;
        if (elapsed < minRecordingSec) {
          // Too short — reject client-side so they don't waste an analysis.
          selectedBlob = null;
          selectedName = null;
          $("recordStatus").textContent =
            `Recording was only ${elapsed.toFixed(0)}s — record at least ${minRecordingSec}s.`;
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
      recordBtn.textContent = "■ Stop recording";
      recordBtn.classList.add("recording");
      $("recordStatus").textContent = `Recording… (min ${minRecordingSec}s)`;
    } catch (err) {
      showError("Microphone access denied or unavailable.");
    }
  });

  function updateSelection() {
    $("selected").textContent = selectedName ? `Selected: ${selectedName}` : "";
    analyzeBtn.disabled = !selectedBlob;
  }

  // ---- Analyze -------------------------------------------------------------
  analyzeBtn.addEventListener("click", async () => {
    if (!selectedBlob) return;
    hide("error");
    hide("results");
    show("loading");
    analyzeBtn.disabled = true;

    const form = new FormData();
    form.append("audio", selectedBlob, selectedName);
    setLoadingMsg("Uploading…");

    try {
      // The server analyzes in the background and returns a job id right away,
      // so we never hold one long request open (which browsers/proxies drop and
      // surface as a "network error"). We then poll until it's finished.
      const resp = await fetch("/analyze", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        hide("loading");
        showError(data.error || `Request failed (${resp.status}).`);
        return;
      }
      const result = await pollAnalysis(data.job_id);
      hide("loading");
      if (!result) {
        showError("Analysis timed out. Please try again.");
      } else if (result.error) {
        showError(result.error);
      } else {
        // Keep rendering errors distinct from network errors — a bug while
        // drawing results shouldn't be reported as "Network error".
        try {
          render(result);
          show("results");
          handleSaved(result);
        } catch (e) {
          showError("Couldn't display the results: " + e.message);
        }
      }
    } catch (err) {
      hide("loading");
      showError("Network error: " + err.message);
    } finally {
      analyzeBtn.disabled = false;
    }
  });

  // Poll the lightweight status endpoint until the background analysis finishes.
  // Each request is short, so a brief network blip just retries instead of
  // failing the whole analysis. Returns the result, {error}, or null on timeout.
  async function pollAnalysis(jobId) {
    const started = Date.now();
    const MAX_MS = 20 * 60 * 1000;   // generous ceiling for very long talks
    let consecutiveErrors = 0;
    while (Date.now() - started < MAX_MS) {
      await sleep(2000);
      setLoadingMsg(`Transcribing and analyzing… (${Math.round((Date.now() - started) / 1000)}s)`);
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
        // Transient — keep polling through short outages.
        if (++consecutiveErrors > 30) throw err;
        continue;
      }
      if (d.state === "done") return d.result;
      if (d.state === "error") return { error: d.error };
    }
    return null;  // timed out
  }

  // Show whether the result was recorded to the leaderboard, then refresh it.
  function handleSaved(data) {
    const note = $("savedNote");
    if (data.saved_to_leaderboard) {
      const score =
        data.scores && data.scores.overall != null
          ? Math.round(data.scores.overall)
          : "?";
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

  // ---- Rendering -----------------------------------------------------------
  function render(d) {
    renderScores(d.scores);
    renderMetrics(d);
    renderFeedback(d.feedback);
    renderCharts(d);
    renderContent(d.content);
    renderLanguage(d.language);
    renderIdealDelivery(d);
    $("transcript").textContent = d.transcript || "";
    $("warnings").textContent = (d.warnings || []).join("  ·  ");
  }

  function scoreColor(s) {
    if (s == null) return "#8b93a7";
    if (s >= 75) return "#36d399";
    if (s >= 55) return "#fbbd23";
    return "#f87272";
  }

  function renderScores(s) {
    const o = s.overall ?? 0;
    const circle = $("overallScore");
    circle.textContent = s.overall != null ? Math.round(o) : "–";
    circle.style.background = `conic-gradient(${scoreColor(o)} ${o * 3.6}deg, var(--card2) 0deg)`;
    setBar("Delivery", s.delivery);
    setBar("Language", s.language);
    setBar("Content", s.content);
  }

  function setBar(name, val) {
    $("score" + name).textContent = val != null ? Math.round(val) : "–";
    const bar = $("bar" + name);
    bar.style.width = (val || 0) + "%";
    bar.style.background = scoreColor(val);
  }

  function metric(val, lbl) {
    return `<div class="metric"><div class="val">${val}</div><div class="lbl">${lbl}</div></div>`;
  }

  function renderMetrics(d) {
    const dl = d.delivery,
      lg = d.language;
    const m = [];
    m.push(metric(dl.rate.wpm ?? "–", "Words / min"));
    m.push(metric(dl.pitch.variability_score ?? "–", "Pitch variation"));
    m.push(metric(dl.volume.consistency_score ?? "–", "Volume consistency"));
    m.push(metric(dl.fillers.total ?? "–", "Filler words"));
    m.push(metric(dl.pauses.score ?? "–", "Pause quality"));
    m.push(metric(d.content.score ?? "–", "Structure score"));
    m.push(
      metric(
        d.duration_sec ? (d.duration_sec / 60).toFixed(1) : "–",
        "Minutes",
      ),
    );
    m.push(metric(d.word_count ?? "–", "Total words"));
    $("metrics").innerHTML = m.join("");
  }

  function renderFeedback(fb) {
    if (!fb) return;
    $("topRecs").innerHTML = (fb.top_recommendations || [])
      .map((r) => `<li>${esc(r)}</li>`)
      .join("");
    $("strengths").innerHTML = (fb.strengths || [])
      .map((r) => `<li>${esc(r)}</li>`)
      .join("");
    $("improvements").innerHTML =
      (fb.improvements || []).map((r) => `<li>${esc(r)}</li>`).join("") ||
      "<li class='muted'>None — nice work.</li>";
  }

  function renderContent(c) {
    if (!c) return;
    $("contentSummary").textContent =
      (c.summary || "") + (c.method ? `  (method: ${c.method})` : "");
    const cats = c.categories || {};
    $("contentCats").innerHTML = Object.entries(cats)
      .map(
        ([name, info]) => `
      <div class="cat">
        <div class="head">
          <span class="name">${esc(name)}</span>
          <span class="pts" style="color:${scoreColor(info.score)}">${info.score ?? "–"}</span>
        </div>
        <p>${esc(info.feedback || "")}</p>
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
    const pills = (obj) =>
      Object.entries(obj || {})
        .map(([k, v]) => `<span class="pill">${esc(k)} ×${v}</span>`)
        .join("") || "<span class='muted'>none</span>";
    const left = `
      <div>
        <h3>Transitions used (${tr.total ?? 0})</h3>
        <div>${pills(tr.by_phrase)}</div>
        <h3 style="margin-top:1rem">Keywords reinforced</h3>
        <div>${(kw.keywords || []).map((k) => `<span class="pill">${esc(k.word)} ×${k.count}</span>`).join("") || "<span class='muted'>none</span>"}</div>
      </div>`;
    const right = `
      <div>
        <h3>Buzzwords flagged <span class="muted" style="font-weight:400;font-size:.8em">(advisory — doesn't affect your score)</span></h3>
        <div>${pills(bz.by_word)}</div>
        ${
          Object.keys(bz.suggestions || {}).length
            ? `<p class="muted" style="margin-top:.5rem">Try: ` +
              Object.entries(bz.suggestions)
                .map(([b, a]) => `<b>${esc(b)}</b> → ${esc(a)}`)
                .join("; ") +
              "</p>"
            : ""
        }
        ${
          Object.keys(bz.suppressed || {}).length
            ? `<p class="muted" style="margin-top:.35rem;font-size:.85em">Not flagged — used appropriately in context: ` +
              Object.keys(bz.suppressed)
                .map((w) => esc(w))
                .join(", ") +
              "</p>"
            : ""
        }
        <h3 style="margin-top:1rem">Repeated words / phrases</h3>
        <div>${pills(Object.assign({}, rp.repeated_words, rp.repeated_phrases))}</div>
      </div>`;
    $("languageDetails").innerHTML = left + right;
  }

  // ---- Ideal delivery (ElevenLabs TTS) -------------------------------------
  let idealTranscript = "";
  let idealSentences = [];
  let idealSelected = new Set();

  // Split into sentences for click-to-select. The lookahead requires whitespace
  // or end-of-text after the terminator so decimals ("12.5") aren't split.
  function splitSentences(text) {
    return (text.match(/[\s\S]*?[.!?]+(?=\s|$)|[\s\S]+$/g) || [text])
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function renderIdealDelivery(d) {
    resetIdeal();
    idealTranscript = d.transcript || "";
    idealSentences = idealTranscript ? splitSentences(idealTranscript) : [];
    idealSelected = new Set();
    renderIdealPicker();
    updateIdealSelInfo();
    // Optional, opt-in cloud feature: only surface it when the server has an
    // ElevenLabs key configured. Otherwise stay fully local and hide the card.
    $("idealCard").classList.toggle(
      "hidden",
      !(d.ideal_delivery_available && idealTranscript),
    );
  }

  // Render the transcript as clickable sentences so the user can improve a part.
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

  // Selected sentences, joined back in their original order.
  function idealSelectedText() {
    return [...idealSelected]
      .sort((a, b) => a - b)
      .map((i) => idealSentences[i])
      .join(" ");
  }

  function updateIdealSelInfo() {
    const n = idealSelected.size;
    if (!n) {
      $("idealSelInfo").textContent = "Improving: whole talk";
      hide("idealClear");
    } else {
      const words = idealSelectedText().split(/\s+/).filter(Boolean).length;
      $("idealSelInfo").textContent =
        `Improving: ${n} selected sentence${n > 1 ? "s" : ""} (~${words} words)`;
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
    const btn = $("idealBtn");
    btn.disabled = false;
    btn.textContent = "▶ Generate ideal delivery";
  }

  async function generateIdeal() {
    const text = idealSelected.size ? idealSelectedText() : idealTranscript;
    if (!text) return;
    const btn = $("idealBtn");
    btn.disabled = true;
    const scope = idealSelected.size ? "selected part" : "whole talk";
    $("idealStatus").textContent =
      ` Rewriting the ${scope} and synthesizing… this can take a few seconds.`;
    try {
      const r = await fetch("/api/ideal-delivery", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: text }),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        $("idealStatus").textContent =
          " " + (d.error || `Request failed (${r.status}).`);
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
          ? " Ready — script cleaned without the LLM (start Ollama for a fuller rewrite)."
          : " Ready — press play.";
      } else {
        audio.classList.add("hidden");
        $("idealStatus").textContent =
          " " + (d.audio_error || d.note || "Script ready (no audio).");
      }
      show("idealOutput");
      btn.textContent = "↻ Regenerate";
      btn.disabled = false;
    } catch (err) {
      $("idealStatus").textContent = " Network error: " + err.message;
      btn.disabled = false;
    }
  }

  $("idealBtn").addEventListener("click", generateIdeal);
  $("idealClear").addEventListener("click", () => {
    idealSelected.clear();
    $("idealPicker")
      .querySelectorAll(".sent.selected")
      .forEach((s) => s.classList.remove("selected"));
    updateIdealSelInfo();
  });

  // ---- Charts --------------------------------------------------------------
  function destroyCharts() {
    Object.values(charts).forEach((c) => c && c.destroy());
    charts = {};
  }

  const baseOpts = {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: "#8b93a7" }, grid: { color: "#2a3346" } },
      y: { ticks: { color: "#8b93a7" }, grid: { color: "#2a3346" } },
    },
  };

  // Deep-clone a Chart.js config. Unlike structuredClone, this passes functions
  // (Chart.js callbacks) through by reference instead of throwing DataCloneError.
  function cloneConfig(value) {
    if (Array.isArray(value)) return value.map(cloneConfig);
    if (value && typeof value === "object") {
      const out = {};
      for (const k in value) out[k] = cloneConfig(value[k]);
      return out;
    }
    return value; // primitives and functions: shared by reference
  }

  // Create a chart and remember its config so it can be re-rendered, enlarged,
  // in the click-to-zoom modal.
  function mkChart(id, config) {
    // Keep a pristine copy for the enlarge-modal, and hand Chart.js its OWN copy
    // to mutate. Several charts share `baseOpts` by reference; if Chart.js
    // mutates it (it injects default tick callback functions), the next chart's
    // config would carry those functions and break cloning. Cloning what we give
    // Chart.js keeps the shared options pristine.
    chartConfigs[id] = cloneConfig(config);
    return new Chart($(id), cloneConfig(config));
  }

  function renderCharts(d) {
    destroyCharts();
    const dl = d.delivery;

    // WPM timeline
    const wpm = dl.rate.timeline || [];
    charts.wpm = mkChart("wpmChart", {
      type: "line",
      data: {
        labels: wpm.map((p) => p.t + "s"),
        datasets: [
          {
            data: wpm.map((p) => p.wpm),
            borderColor: "#5b8cff",
            backgroundColor: "rgba(91,140,255,.15)",
            fill: true,
            tension: 0.3,
            pointBackgroundColor: wpm.map((p) =>
              p.label === "too_fast"
                ? "#f87272"
                : p.label === "too_slow"
                  ? "#fbbd23"
                  : "#5b8cff",
            ),
          },
        ],
      },
      options: baseOpts,
    });

    // Pitch
    const pitch = (dl.pitch.timeline || []).filter((p) => p.hz != null);
    charts.pitch = mkChart("pitchChart", {
      type: "line",
      data: {
        labels: pitch.map((p) => p.t),
        datasets: [
          {
            data: pitch.map((p) => p.hz),
            borderColor: "#36d399",
            pointRadius: 0,
            tension: 0.3,
          },
        ],
      },
      options: baseOpts,
    });

    // Volume
    const vol = dl.volume.timeline || [];
    charts.volume = mkChart("volumeChart", {
      type: "line",
      data: {
        labels: vol.map((p) => p.t),
        datasets: [
          {
            data: vol.map((p) => p.db),
            borderColor: "#fbbd23",
            backgroundColor: "rgba(251,189,35,.15)",
            fill: true,
            pointRadius: 0,
            tension: 0.3,
          },
        ],
      },
      options: baseOpts,
    });

    // Pause timeline (scatter: time vs duration, colored by type)
    const pauses = dl.pauses.timeline || [];
    const colorOf = (t) =>
      ({
        strategic: "#36d399",
        long_awkward: "#f87272",
        hesitation: "#fbbd23",
        normal: "#5b8cff",
      })[t] || "#5b8cff";
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
      options: Object.assign({}, baseOpts, {
        scales: {
          x: {
            title: { display: true, text: "time (s)", color: "#8b93a7" },
            ticks: { color: "#8b93a7" },
            grid: { color: "#2a3346" },
          },
          y: {
            title: { display: true, text: "pause (s)", color: "#8b93a7" },
            ticks: { color: "#8b93a7" },
            grid: { color: "#2a3346" },
          },
        },
      }),
    });

    // Filler words bar — top 5 most frequent, highest first. Sort explicitly
    // (desc by count) so order is never ambiguous, and force whole-number ticks
    // (counts are integers — no 0.5 gridlines).
    const fillerTop = Object.entries(dl.fillers.by_word || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    charts.filler = mkChart("fillerChart", {
      type: "bar",
      data: {
        labels: fillerTop.map(([w]) => w),
        datasets: [
          { data: fillerTop.map(([, c]) => c), backgroundColor: "#f87272" },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8b93a7" }, grid: { color: "#2a3346" } },
          y: {
            beginAtZero: true,
            ticks: { color: "#8b93a7", precision: 0, stepSize: 1 },
            grid: { color: "#2a3346" },
          },
        },
      },
    });

    // Content radar
    const cats = (d.content && d.content.categories) || {};
    charts.content = mkChart("contentChart", {
      type: "radar",
      data: {
        labels: Object.keys(cats),
        datasets: [
          {
            data: Object.values(cats).map((c) => c.score || 0),
            borderColor: "#5b8cff",
            backgroundColor: "rgba(91,140,255,.25)",
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          r: {
            min: 0,
            max: 100,
            ticks: { color: "#8b93a7", backdropColor: "transparent" },
            grid: { color: "#2a3346" },
            angleLines: { color: "#2a3346" },
            pointLabels: { color: "#e6e9f0" },
          },
        },
      },
    });
  }

  // ---- Click-to-enlarge charts --------------------------------------------
  let modalChart = null;

  function openChartModal(canvasId, title) {
    const cfg = chartConfigs[canvasId];
    if (!cfg) return;
    const clone = cloneConfig(cfg);
    clone.options = Object.assign({}, clone.options, {
      responsive: true,
      maintainAspectRatio: false,
    });
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

  // ---- Auth & leaderboard --------------------------------------------------
  const authState = { user: null, dbAvailable: false };
  let authMode = "login";

  async function loadMe() {
    try {
      const r = await fetch("/api/me");
      const d = await r.json();
      authState.user = d.user;
      authState.dbAvailable = d.db_available;
    } catch {
      authState.user = null;
    }
    renderAuthbar();
  }

  function renderAuthbar() {
    const bar = $("authbar");
    if (authState.user) {
      bar.innerHTML =
        `<span class="who">👤 ${esc(authState.user.username)}</span>` +
        `<button class="btn small" id="logoutBtn">Log out</button>`;
      $("logoutBtn").addEventListener("click", logout);
    } else if (!authState.dbAvailable) {
      // Accounts are optional: hide sign-in entirely until MongoDB is reachable.
      bar.innerHTML = "";
    } else {
      bar.innerHTML =
        `<button class="btn small" id="openLogin">Log in</button>` +
        `<button class="btn small primary" id="openRegister">Sign up</button>`;
      $("openLogin").addEventListener("click", () => openAuth("login"));
      $("openRegister").addEventListener("click", () => openAuth("register"));
    }
  }

  function openAuth(mode) {
    if (!authState.dbAvailable) {
      alert(
        "Accounts are unavailable — the server can't reach MongoDB right now.",
      );
      return;
    }
    setAuthMode(mode);
    $("authError").textContent = "";
    $("authForm").reset();
    $("authModal").classList.remove("hidden");
  }
  function closeAuth() {
    $("authModal").classList.add("hidden");
  }

  function setAuthMode(mode) {
    authMode = mode;
    $("tabLogin").classList.toggle("active", mode === "login");
    $("tabRegister").classList.toggle("active", mode === "register");
    $("authEmail").style.display = mode === "register" ? "block" : "none";
    $("authSubmit").textContent =
      mode === "login" ? "Log in" : "Create account";
  }

  async function submitAuth(e) {
    e.preventDefault();
    const body = {
      username: $("authUsername").value.trim(),
      password: $("authPassword").value,
    };
    if (authMode === "register") body.email = $("authEmail").value.trim();
    try {
      const r = await fetch(
        `/api/${authMode === "login" ? "login" : "register"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const d = await r.json();
      if (!r.ok || d.error) {
        $("authError").textContent = d.error || "Failed.";
        return;
      }
      authState.user = d.user;
      closeAuth();
      renderAuthbar();
      loadLeaderboard();
    } catch (err) {
      $("authError").textContent = "Network error: " + err.message;
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

  async function loadLeaderboard() {
    const card = $("leaderboardCard");
    const body = $("leaderboardBody");
    const note = $("leaderboardNote");
    try {
      const r = await fetch("/api/leaderboard");
      const d = await r.json();
      if (d.error) {
        // No database yet — keep the leaderboard out of the way entirely.
        card.classList.add("hidden");
        body.innerHTML = "";
        return;
      }
      card.classList.remove("hidden");
      const rows = d.leaderboard || [];
      if (!rows.length) {
        note.textContent = "No scores yet — be the first to get on the board!";
        body.innerHTML = "";
        return;
      }
      note.textContent = "Top scores across everyone (each user's best run).";
      body.innerHTML = rows
        .map(
          (row) => `
        <tr class="${row.is_me ? "me" : ""}">
          <td>${row.rank}</td>
          <td>${esc(row.username || "—")}${row.is_me ? " (you)" : ""}</td>
          <td><b>${row.best_score ?? "—"}</b></td>
          <td>${row.attempts}</td>
        </tr>`,
        )
        .join("");
    } catch {
      card.classList.add("hidden");
    }
  }

  // Pull config (minimum recording length) from the server.
  async function loadConfig() {
    try {
      const d = await (await fetch("/health")).json();
      if (typeof d.min_recording_sec === "number")
        minRecordingSec = d.min_recording_sec;
    } catch {
      /* keep default */
    }
  }

  // Click any chart to open an enlarged version.
  function wireChartZoom() {
    document.querySelectorAll(".chart-box").forEach((box) => {
      box.addEventListener("click", () => {
        const canvas = box.querySelector("canvas");
        const title = (box.querySelector("h3") || {}).textContent || "";
        if (canvas && canvas.id) openChartModal(canvas.id, title);
      });
    });
  }

  // wire up modals + initial load
  $("authClose").addEventListener("click", closeAuth);
  $("authModal").addEventListener("click", (e) => {
    if (e.target.id === "authModal") closeAuth();
  });
  $("tabLogin").addEventListener("click", () => setAuthMode("login"));
  $("tabRegister").addEventListener("click", () => setAuthMode("register"));
  $("authForm").addEventListener("submit", submitAuth);
  $("chartModalClose").addEventListener("click", closeChartModal);
  $("chartModal").addEventListener("click", (e) => {
    if (e.target.id === "chartModal") closeChartModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeChartModal();
  });
  wireChartZoom();
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
    const e = $("error");
    e.textContent = msg;
    show("error");
  }
  function setLoadingMsg(msg) {
    const el = $("loadingMsg");
    if (el) el.textContent = msg;
  }
  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }
  function esc(s) {
    return String(s).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  }
})();
