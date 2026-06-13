/* Presentation Helper — frontend logic */
(() => {
  const $ = (id) => document.getElementById(id);

  let selectedBlob = null;
  let selectedName = null;
  let mediaRecorder = null;
  let chunks = [];
  let charts = {};

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
        selectedBlob = new Blob(chunks, { type: "audio/webm" });
        selectedName = "recording.webm";
        stream.getTracks().forEach((t) => t.stop());
        recordBtn.textContent = "● Start recording";
        recordBtn.classList.remove("recording");
        $("recordStatus").textContent = "";
        updateSelection();
      };
      mediaRecorder.start();
      recordBtn.textContent = "■ Stop recording";
      recordBtn.classList.add("recording");
      $("recordStatus").textContent = "Recording…";
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
    hide("error"); hide("results"); show("loading");
    analyzeBtn.disabled = true;

    const form = new FormData();
    form.append("audio", selectedBlob, selectedName);

    try {
      const resp = await fetch("/analyze", { method: "POST", body: form });
      const data = await resp.json();
      hide("loading");
      if (!resp.ok || data.error) {
        showError(data.error || `Request failed (${resp.status}).`);
      } else {
        render(data);
        show("results");
        handleSaved(data);
      }
    } catch (err) {
      hide("loading");
      showError("Network error: " + err.message);
    } finally {
      analyzeBtn.disabled = false;
    }
  });

  // Show whether the result was recorded to the leaderboard, then refresh it.
  function handleSaved(data) {
    const note = $("savedNote");
    if (data.saved_to_leaderboard) {
      const score = data.scores && data.scores.overall != null ? Math.round(data.scores.overall) : "?";
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
    circle.style.background =
      `conic-gradient(${scoreColor(o)} ${o * 3.6}deg, var(--card2) 0deg)`;
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
    const dl = d.delivery, lg = d.language;
    const m = [];
    m.push(metric(dl.rate.wpm ?? "–", "Words / min"));
    m.push(metric(dl.pitch.variability_score ?? "–", "Pitch variation"));
    m.push(metric(dl.volume.consistency_score ?? "–", "Volume consistency"));
    m.push(metric(dl.fillers.total ?? "–", "Filler words"));
    m.push(metric(dl.pauses.score ?? "–", "Pause quality"));
    m.push(metric(d.content.score ?? "–", "Structure score"));
    m.push(metric((d.duration_sec ? (d.duration_sec / 60).toFixed(1) : "–"), "Minutes"));
    m.push(metric(d.word_count ?? "–", "Total words"));
    $("metrics").innerHTML = m.join("");
  }

  function renderFeedback(fb) {
    if (!fb) return;
    $("topRecs").innerHTML = (fb.top_recommendations || []).map((r) => `<li>${esc(r)}</li>`).join("");
    $("strengths").innerHTML = (fb.strengths || []).map((r) => `<li>${esc(r)}</li>`).join("");
    $("improvements").innerHTML = (fb.improvements || []).map((r) => `<li>${esc(r)}</li>`).join("") ||
      "<li class='muted'>None — nice work.</li>";
  }

  function renderContent(c) {
    if (!c) return;
    $("contentSummary").textContent =
      (c.summary || "") + (c.method ? `  (method: ${c.method})` : "");
    const cats = c.categories || {};
    $("contentCats").innerHTML = Object.entries(cats).map(([name, info]) => `
      <div class="cat">
        <div class="head">
          <span class="name">${esc(name)}</span>
          <span class="pts" style="color:${scoreColor(info.score)}">${info.score ?? "–"}</span>
        </div>
        <p>${esc(info.feedback || "")}</p>
      </div>`).join("");
  }

  function renderLanguage(lg) {
    if (!lg) return;
    const tr = lg.transitions || {}, bz = lg.buzzwords || {}, rp = lg.repetition || {}, kw = lg.keywords || {};
    const pills = (obj) => Object.entries(obj || {}).map(([k, v]) =>
      `<span class="pill">${esc(k)} ×${v}</span>`).join("") || "<span class='muted'>none</span>";
    const left = `
      <div>
        <h3>Transitions used (${tr.total ?? 0})</h3>
        <div>${pills(tr.by_phrase)}</div>
        <h3 style="margin-top:1rem">Keywords reinforced</h3>
        <div>${(kw.keywords || []).map((k) => `<span class="pill">${esc(k.word)} ×${k.count}</span>`).join("") || "<span class='muted'>none</span>"}</div>
      </div>`;
    const right = `
      <div>
        <h3>Buzzwords flagged</h3>
        <div>${pills(bz.by_word)}</div>
        ${Object.keys(bz.suggestions || {}).length ? `<p class="muted" style="margin-top:.5rem">Try: ` +
          Object.entries(bz.suggestions).map(([b, a]) => `<b>${esc(b)}</b> → ${esc(a)}`).join("; ") + "</p>" : ""}
        <h3 style="margin-top:1rem">Repeated words / phrases</h3>
        <div>${pills(Object.assign({}, rp.repeated_words, rp.repeated_phrases))}</div>
      </div>`;
    $("languageDetails").innerHTML = left + right;
  }

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

  function renderCharts(d) {
    destroyCharts();
    const dl = d.delivery;

    // WPM timeline
    const wpm = dl.rate.timeline || [];
    charts.wpm = new Chart($("wpmChart"), {
      type: "line",
      data: {
        labels: wpm.map((p) => p.t + "s"),
        datasets: [{
          data: wpm.map((p) => p.wpm),
          borderColor: "#5b8cff", backgroundColor: "rgba(91,140,255,.15)",
          fill: true, tension: .3,
          pointBackgroundColor: wpm.map((p) =>
            p.label === "too_fast" ? "#f87272" : p.label === "too_slow" ? "#fbbd23" : "#5b8cff"),
        }],
      },
      options: baseOpts,
    });

    // Pitch
    const pitch = (dl.pitch.timeline || []).filter((p) => p.hz != null);
    charts.pitch = new Chart($("pitchChart"), {
      type: "line",
      data: {
        labels: pitch.map((p) => p.t),
        datasets: [{ data: pitch.map((p) => p.hz), borderColor: "#36d399", pointRadius: 0, tension: .3 }],
      },
      options: baseOpts,
    });

    // Volume
    const vol = dl.volume.timeline || [];
    charts.volume = new Chart($("volumeChart"), {
      type: "line",
      data: {
        labels: vol.map((p) => p.t),
        datasets: [{ data: vol.map((p) => p.db), borderColor: "#fbbd23",
          backgroundColor: "rgba(251,189,35,.15)", fill: true, pointRadius: 0, tension: .3 }],
      },
      options: baseOpts,
    });

    // Pause timeline (scatter: time vs duration, colored by type)
    const pauses = dl.pauses.timeline || [];
    const colorOf = (t) => ({ strategic: "#36d399", long_awkward: "#f87272",
      hesitation: "#fbbd23", normal: "#5b8cff" }[t] || "#5b8cff");
    charts.pause = new Chart($("pauseChart"), {
      type: "scatter",
      data: {
        datasets: [{
          data: pauses.map((p) => ({ x: p.t, y: p.duration })),
          backgroundColor: pauses.map((p) => colorOf(p.type)),
          pointRadius: 5,
        }],
      },
      options: Object.assign({}, baseOpts, {
        scales: {
          x: { title: { display: true, text: "time (s)", color: "#8b93a7" }, ticks: { color: "#8b93a7" }, grid: { color: "#2a3346" } },
          y: { title: { display: true, text: "pause (s)", color: "#8b93a7" }, ticks: { color: "#8b93a7" }, grid: { color: "#2a3346" } },
        },
      }),
    });

    // Filler words bar
    const fillers = dl.fillers.by_word || {};
    charts.filler = new Chart($("fillerChart"), {
      type: "bar",
      data: {
        labels: Object.keys(fillers),
        datasets: [{ data: Object.values(fillers), backgroundColor: "#f87272" }],
      },
      options: baseOpts,
    });

    // Content radar
    const cats = (d.content && d.content.categories) || {};
    charts.content = new Chart($("contentChart"), {
      type: "radar",
      data: {
        labels: Object.keys(cats),
        datasets: [{
          data: Object.values(cats).map((c) => c.score || 0),
          borderColor: "#5b8cff", backgroundColor: "rgba(91,140,255,.25)",
        }],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { r: { min: 0, max: 100, ticks: { color: "#8b93a7", backdropColor: "transparent" },
          grid: { color: "#2a3346" }, angleLines: { color: "#2a3346" }, pointLabels: { color: "#e6e9f0" } } },
      },
    });
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
    } catch { authState.user = null; }
    renderAuthbar();
  }

  function renderAuthbar() {
    const bar = $("authbar");
    if (authState.user) {
      bar.innerHTML = `<span class="who">👤 ${esc(authState.user.username)}</span>` +
        `<button class="btn small" id="logoutBtn">Log out</button>`;
      $("logoutBtn").addEventListener("click", logout);
    } else if (!authState.dbAvailable) {
      // Accounts are optional: hide sign-in entirely until MongoDB is reachable.
      bar.innerHTML = "";
    } else {
      bar.innerHTML = `<button class="btn small" id="openLogin">Log in</button>` +
        `<button class="btn small primary" id="openRegister">Sign up</button>`;
      $("openLogin").addEventListener("click", () => openAuth("login"));
      $("openRegister").addEventListener("click", () => openAuth("register"));
    }
  }

  function openAuth(mode) {
    if (!authState.dbAvailable) {
      alert("Accounts are unavailable — the server can't reach MongoDB right now.");
      return;
    }
    setAuthMode(mode);
    $("authError").textContent = "";
    $("authForm").reset();
    $("authModal").classList.remove("hidden");
  }
  function closeAuth() { $("authModal").classList.add("hidden"); }

  function setAuthMode(mode) {
    authMode = mode;
    $("tabLogin").classList.toggle("active", mode === "login");
    $("tabRegister").classList.toggle("active", mode === "register");
    $("authEmail").style.display = mode === "register" ? "block" : "none";
    $("authSubmit").textContent = mode === "login" ? "Log in" : "Create account";
  }

  async function submitAuth(e) {
    e.preventDefault();
    const body = {
      username: $("authUsername").value.trim(),
      password: $("authPassword").value,
    };
    if (authMode === "register") body.email = $("authEmail").value.trim();
    try {
      const r = await fetch(`/api/${authMode === "login" ? "login" : "register"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok || d.error) { $("authError").textContent = d.error || "Failed."; return; }
      authState.user = d.user;
      closeAuth();
      renderAuthbar();
      loadLeaderboard();
    } catch (err) {
      $("authError").textContent = "Network error: " + err.message;
    }
  }

  async function logout() {
    try { await fetch("/api/logout", { method: "POST" }); } catch {}
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
      body.innerHTML = rows.map((row) => `
        <tr class="${row.is_me ? "me" : ""}">
          <td>${row.rank}</td>
          <td>${esc(row.username || "—")}${row.is_me ? " (you)" : ""}</td>
          <td><b>${row.best_score ?? "—"}</b></td>
          <td>${row.attempts}</td>
        </tr>`).join("");
    } catch {
      card.classList.add("hidden");
    }
  }

  // wire up modal + initial load
  $("authClose").addEventListener("click", closeAuth);
  $("authModal").addEventListener("click", (e) => { if (e.target.id === "authModal") closeAuth(); });
  $("tabLogin").addEventListener("click", () => setAuthMode("login"));
  $("tabRegister").addEventListener("click", () => setAuthMode("register"));
  $("authForm").addEventListener("submit", submitAuth);
  loadMe();
  loadLeaderboard();

  // ---- Helpers -------------------------------------------------------------
  function show(id) { $(id).classList.remove("hidden"); }
  function hide(id) { $(id).classList.add("hidden"); }
  function showError(msg) { const e = $("error"); e.textContent = msg; show("error"); }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
})();
