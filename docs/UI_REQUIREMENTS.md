# Pitch Prepper — UI Requirements (functional spec, design excluded)

This document lists **everything the UI must contain and do**, independent of
visual design (no colors, fonts, spacing, layout, or branding decisions are
prescribed). It is derived from the actual backend (`backend/app.py`) and the
existing reference frontend, so every field, state, and endpoint below is real.

The app is currently a **single-page app (SPA)**: one HTML document with
show/hide sections plus two modals. You may keep it as an SPA or split it into
routed pages — the requirements are organized by **logical screen** so either
implementation works. What must not change is the **data contract** with the
backend (Section 9) and the **behaviors** (Sections 3–8).

> Terminology: "screen/view" = a logical area the user sees; in the SPA these are
> sections toggled via a `hidden` class. "Card" = a self-contained content block.

---

## 0. Global conventions

- **Product name shown to users:** *Pitch Prepper* (subtitle: "AI Speaking Coach").
- **Backend transport:** all calls are `fetch` to same-origin endpoints returning
  JSON (except audio bytes, which arrive as a base64 data URI inside JSON).
- **No build step assumed:** the reference uses Chart.js from a CDN. Any chart
  library is acceptable as long as the six visualizations (Section 3.4.6) render.
- **Security — mandatory:** all server- or user-derived strings rendered into the
  DOM (username, transcript, feedback text, category feedback, buzzword
  suggestions, keywords, error messages) **must be HTML-escaped** to prevent XSS.
  The reference does this via an `esc()` helper.
- **Graceful degradation is a hard requirement.** Three backend features are
  optional and may be absent at runtime. The UI must detect this and hide the
  corresponding controls rather than show broken/erroring elements:
  1. **MongoDB** → accounts + leaderboard.
  2. **ElevenLabs API key** → "Hear how it could sound" audio.
  3. **Ollama (local LLM)** → richer content feedback & script rewrite (the UI
     still works; it just shows a "heuristic" method label).

---

## 1. App shell (present on every view)

### 1.1 Header / hero
- Product title and a one-line description ("Upload or record your talk and get
  coaching on delivery, language, and content.").
- Contains the **auth bar** (Section 1.2).

### 1.2 Auth bar (top of header)
Driven by `GET /api/me` (`{user, db_available}`). Three mutually exclusive states:
- **Logged in:** show the username (e.g. "👤 alice") + a **Log out** button.
- **Logged out, DB available:** show **Log in** + **Sign up** buttons (Sign up
  opens the auth modal in register mode; Log in in login mode).
- **DB unavailable:** render **nothing** (accounts are hidden entirely — never
  show sign-in controls that can't work).

### 1.3 Footer
- Static credits/info line. (Content only; styling is design.)

### 1.4 Cross-cutting startup behavior
On initial load the UI must call, in any order:
- `GET /health` → read `min_recording_sec` (used by the recorder, Section 3.1).
- `GET /api/me` → render the auth bar.
- `GET /api/leaderboard` → render or hide the leaderboard (Section 3.5).

All three must fail soft (network error keeps defaults / hides the optional UI).

---

## 2. Screen inventory

| # | Screen / overlay | Type | Visible when |
|---|------------------|------|--------------|
| 1 | Input (Home) | view | Always (default landing) |
| 2 | Loading / progress | view | During analysis only |
| 3 | Error | view | When an analysis/request error occurs |
| 4 | Results dashboard | view | After a successful analysis |
| 5 | Leaderboard | view/card | When MongoDB is reachable |
| 6 | Auth modal (login / register) | modal | On demand (DB available) |
| 7 | Chart enlarge modal | modal | On clicking a chart |

The Input screen and Leaderboard can be visible at the same time. Loading,
Error, and Results are mutually exclusive with each other.

---

## 3. Screen-by-screen requirements

### 3.1 Input screen (Home)

Two input methods side by side, plus a single submit control.

**A. Upload audio**
- A file picker accepting these extensions (server-enforced allow-list):
  `wav, mp3, m4a, mp4, ogg, flac, webm, aac, opus`.
- On selection, capture the file and its name; show "Selected: `<filename>`".
- Max upload size is **200 MB** (server returns HTTP 413 with a JSON error if
  exceeded — surface that message, do not crash).

**B. Record**
- A single toggle button: **"● Start recording"** ↔ **"■ Stop recording"**.
- Uses the browser microphone (`getUserMedia({audio:true})`, `MediaRecorder`,
  output mime `audio/webm`, filename `recording.webm`).
- A status line shows live state:
  - While recording: "Recording… (min `<N>`s)".
  - On stop, if long enough: "Recorded `<elapsed>`s ✓".
  - On stop, if too short: "Recording was only `<elapsed>`s — record at least
    `<N>`s." and the recording is discarded.
- `<N>` = `min_recording_sec` from `/health` (default **15**). The UI must
  reject too-short clips **client-side** before upload.
- If mic access is denied/unavailable: show the Error screen with
  "Microphone access denied or unavailable."

**C. Analyze control**
- A single **"Analyze presentation"** button.
- **Disabled** until a file is chosen OR a valid recording exists.
- On click → kicks off the analysis flow (Section 4.1) and switches to the
  Loading screen.

**D. Visual separator** between the two input blocks (an "or"); content only.

### 3.2 Loading screen
- A progress indicator (spinner or equivalent).
- A status message that updates through the flow:
  - "Uploading…" immediately after submit.
  - "Transcribing and analyzing… (`<elapsed>`s)" while polling, with a live
    elapsed-seconds counter.
- Must explain that the first run can take a minute+ (cold model load).
- Shown exclusively while an analysis is in flight; hidden on done/error.

### 3.3 Error screen
- A single block that displays the error message text returned by the backend
  (or a client-side network/timeout message).
- Replaces the Loading screen on failure. The Analyze button must be re-enabled
  so the user can retry.
- Error sources the UI must handle and display:
  - Upload validation (missing file, empty filename, unsupported type).
  - 413 file-too-large.
  - ffmpeg missing (long actionable message from server).
  - Recording too short (server-side fallback message).
  - "Transcription produced no text…".
  - Job expired / not found (404 during polling).
  - Analysis timeout (client-side, 20-minute ceiling).
  - Generic network error.

### 3.4 Results dashboard

Shown after a successful analysis. Composed of the following cards/sections, in
this order. Each lists the exact data fields it consumes (see Section 9 for the
full schema and Section 6 for the source field path).

#### 3.4.1 Saved-to-leaderboard note (conditional banner)
A one-line status above the dashboard:
- If `saved_to_leaderboard` is true: "✓ Saved to the leaderboard — you scored
  `<overall>`." and trigger a leaderboard refresh.
- Else if **not logged in** but DB is available: "ℹ Log in to save this score to
  the leaderboard."
- Else: hidden.

#### 3.4.2 Score summary
- **Overall score** — a single 0–100 number from `scores.overall` (rounded;
  "–" if null). Reference renders it as a radial gauge; the gauge fill amount is
  functional (proportional to score), exact styling is design.
- **Three sub-scores with progress bars**: Delivery (`scores.delivery`),
  Language (`scores.language`), Content (`scores.content`). Each shows the rounded
  number and a bar whose width = the score %.
- **Semantic score levels** (functional, not specific colors): good ≥ 75,
  fair 55–74, poor < 55, unknown/null = neutral. Apply consistently to the
  overall gauge, the three bars, and content category points (3.4.7).

#### 3.4.3 Key Metrics (8 cards)
Each is a value + label:
| Label | Source field |
|-------|--------------|
| Words / min | `delivery.rate.wpm` |
| Pitch variation | `delivery.pitch.variability_score` |
| Volume consistency | `delivery.volume.consistency_score` |
| Filler words | `delivery.fillers.total` |
| Pause quality | `delivery.pauses.score` |
| Structure score | `content.score` |
| Minutes | `duration_sec` ÷ 60 (1 decimal) |
| Total words | `word_count` |
Missing values render as "–".

#### 3.4.4 Top 3 Recommendations + Strengths/Improvements
- **Top 3 Recommendations**: ordered list from `feedback.top_recommendations`.
- **Strengths**: bulleted list from `feedback.strengths`.
- **Areas to improve**: bulleted list from `feedback.improvements`; if empty,
  show a single "None — nice work." item.

#### 3.4.5 "Hear how it could sound" (Ideal Delivery) — conditional card
**Visible only when** `ideal_delivery_available` is true **and** a transcript
exists. Contents:
- Explanatory text: it's an AI-polished take on the user's own talk, generated
  on demand, and that clicking it **sends transcript text to ElevenLabs** (the
  one feature that leaves the machine).
- **Sentence picker**: the transcript split into individual, **clickable
  sentences**. Clicking toggles a sentence's "selected" state.
- **Selection info line**:
  - No selection: "Improving: whole talk".
  - With selection: "Improving: `<n>` selected sentence(s) (~`<w>` words)".
- **Clear selection** control: visible only when ≥1 sentence is selected;
  clears all selections.
- **Generate button**: label "▶ Generate ideal delivery", becomes
  "↻ Regenerate" after a run; disabled while generating.
- **Status line** reflecting progress/outcome (rewriting…, ready, errors,
  heuristic-fallback note).
- **Output area** (hidden until generated):
  - An **audio player** with controls (when audio is returned).
  - A **collapsible "Show the improved script"** revealing the rewritten text.
- Behavior detail: if the response has `script` but no `audio` (e.g. synthesis
  failed or key issue), still show the script and surface `audio_error`/`note`.
  If `method` starts with "heuristic", note that Ollama wasn't used.
- Must **reset** (clear audio, script, status; re-enable button) whenever a new
  analysis is rendered.

#### 3.4.6 Visualizations (6 charts)
A grid of six charts. Each chart is **click-to-enlarge** (opens the chart modal,
Section 3.7). Titles shown above each.

| Chart | Type | Data source | Notes |
|-------|------|-------------|-------|
| Speaking rate (WPM) | line | `delivery.rate.timeline[]` → `{t, wpm, label}` | Point markers convey per-window pacing state via `label` (`ok`/`too_fast`/`too_slow`) — semantic, color is design. |
| Pitch (Hz) | line | `delivery.pitch.timeline[]` → `{t, hz}` | Drop points where `hz` is null. |
| Volume (dB) | line | `delivery.volume.timeline[]` → `{t, db}` | |
| Pause timeline | scatter | `delivery.pauses.timeline[]` → `{t, duration, type}` | x = time, y = pause length; marker grouped by `type` (`strategic`/`long_awkward`/`hesitation`/`normal`). Axis titles "time (s)" / "pause (s)". |
| Filler words | bar | `delivery.fillers.by_word` (object word→count) | Show top 8 by count, descending; integer y-axis from 0. |
| Content scores | radar | `content.categories` (name→`{score,...}`) | Axis 0–100; one spoke per category. |

If a timeline/array is empty or its analyzer reported `available:false`, the
chart should render empty/blank gracefully (no crash).

#### 3.4.7 Content & Structure
- **Summary line**: `content.summary` plus the analysis `method` in parentheses
  (e.g. "(method: llm)" or "(method: heuristic (LLM unavailable))").
- **Per-category cards** for each entry in `content.categories`
  (typically: introduction, thesis, evidence, organization, conclusion). Each
  card shows the **category name**, its **score** (semantic level applied), and
  its **feedback** text.

#### 3.4.8 Language Details (two columns)
- **Transitions used (`transitions.total`)**: render `transitions.by_phrase`
  (phrase → count) as labeled pills; "none" if empty.
- **Keywords reinforced**: render `language.keywords.keywords[]` (`{word,count}`)
  as pills; "none" if empty.
- **Buzzwords flagged** — must include an **"advisory — doesn't affect your
  score"** qualifier:
  - `buzzwords.by_word` (word → count) as pills; "none" if empty.
  - If `buzzwords.suggestions` present: a "Try: `<buzz>` → `<alternative>`; …"
    line.
  - If `buzzwords.suppressed` present: a "Not flagged — used appropriately in
    context: `<words>`" line.
- **Repeated words / phrases**: merge `repetition.repeated_words` and
  `repetition.repeated_phrases` into pills; "none" if empty.

#### 3.4.9 Transcript
- The full transcript text (`transcript`), shown verbatim (escaped).

#### 3.4.10 Warnings
- A line joining `warnings[]` (e.g. "Librosa/numpy unavailable…", "LLM content
  analysis failed…"). Hidden/empty when there are no warnings.

### 3.5 Leaderboard
- A card with a table: columns **#** (rank), **User**, **Best score**,
  **Attempts**. Data from `GET /api/leaderboard` → `leaderboard[]` of
  `{rank, username, best_score, attempts, is_me}`.
- The current user's row is highlighted and labeled "(you)".
- A note line that varies:
  - DB present, has rows: "Top scores across everyone (each user's best run)."
  - DB present, no rows: "No scores yet — be the first to get on the board!"
- **Hide the entire card** when the leaderboard response contains `error`
  (DB unavailable) or on network failure.
- Refresh after: a successful saved analysis, login, register, and logout.

### 3.6 Auth modal (login / register)
A dismissible modal with:
- **Two tabs**: "Log in" and "Sign up", switching mode.
- **Form fields**:
  - Username (always).
  - Email — **shown only in register mode**, labeled optional.
  - Password (labeled "min 6 chars").
- **Submit button** whose label is "Log in" (login) or "Create account"
  (register).
- An **error line** for server validation errors.
- Helper text: "Sign in to save your scores to the global leaderboard."
- **Dismissal**: an × close button **and** clicking the backdrop.
- **Guard**: if a user tries to open auth while DB is unavailable, show an
  "Accounts are unavailable…" notice instead of the form.
- On success: store the user, close the modal, re-render the auth bar, refresh
  the leaderboard.
- Client should respect server validation rules (Section 5) and display the
  server's error messages verbatim on failure.

### 3.7 Chart enlarge modal
- Opens when any chart is clicked; shows the **chart's title** and an enlarged,
  responsive re-render of that same chart.
- **Dismissal**: × button, backdrop click, **and** the Escape key.
- Only one enlarged chart at a time (destroy/replace on reopen).

---

## 4. Key flows (state machines)

### 4.1 Analysis flow (async + polling — required)
The backend runs analysis in the background; the UI **must** poll, not hold one
long request:
1. `POST /analyze` with `multipart/form-data`, field name **`audio`** (the blob +
   filename). Expect **HTTP 202** with `{ "job_id": "<hex>" }`. On non-OK or a
   body `error`, show Error and stop.
2. Poll `GET /analyze/status/<job_id>` every **2 seconds**:
   - `{state:"processing"}` → keep polling, update elapsed-time message.
   - `{state:"done", result:{…}}` → render the dashboard (Section 3.4).
   - `{state:"error", error}` → show Error.
   - HTTP **404** → job expired/not found → show Error ("…try again").
   - Transient network errors: keep retrying; only fail after **>30 consecutive**
     failed polls.
   - Overall **timeout ceiling: 20 minutes** → "Analysis timed out. Please try
     again."
3. On `done`, also run the saved-note logic (3.4.1).
4. Re-enable the Analyze button in all terminal cases.

### 4.2 Auth flow
- Open modal → submit → `POST /api/login` or `POST /api/register` →
  on success update auth bar + leaderboard; on error show message in modal.
- Logout → `POST /api/logout` → clear user, re-render auth bar, refresh
  leaderboard.

### 4.3 Recording flow
- Start → request mic → record → Stop → measure elapsed → enforce
  `min_recording_sec` → set the blob as the analysis input (or reject).

### 4.4 Ideal-delivery flow
- Determine target text: selected sentences (joined in original order) or the
  whole transcript if none selected.
- `POST /api/ideal-delivery` with `{ transcript: "<text>" }`.
- Render returned `script`; if `audio` present, load it into the player; else
  show `audio_error`/`note`. Reflect `method` (llm vs heuristic) in the status.

---

## 5. Client-side validation rules
(Mirror the server so users get immediate feedback; the server still enforces.)
- **File type**: must be one of the allowed extensions (Section 3.1A).
- **File size**: ≤ 200 MB (server returns 413 otherwise — handle gracefully).
- **Recording length**: ≥ `min_recording_sec` (default 15s).
- **Username**: 3–30 chars, allowed `A–Z a–z 0–9 . _ -` (regex
  `^[A-Za-z0-9_.\-]{3,30}$`).
- **Password**: ≥ 6 characters.
- **Email**: optional; standard email format when provided.
- **Analyze button**: disabled with no valid input.

---

## 6. Conditional visibility matrix

| Element | Show only when |
|---|---|
| Auth bar sign-in buttons | `db_available` true and logged out |
| Auth bar user + logout | logged in |
| Auth modal openable | `db_available` true |
| Leaderboard card | `/api/leaderboard` returns no `error` |
| "Saved to leaderboard" note | analysis `saved_to_leaderboard` true |
| "Log in to save" note | logged out AND `db_available` true |
| Ideal Delivery card | `ideal_delivery_available` true AND transcript present |
| Ideal audio player | response contains `audio` |
| Buzzword "Try:" line | `buzzwords.suggestions` non-empty |
| Buzzword "suppressed" line | `buzzwords.suppressed` non-empty |
| Warnings line | `warnings[]` non-empty |
| Clear-selection control | ≥1 sentence selected in the picker |

---

## 7. Empty / edge states (must be handled, not crash)
- Any numeric metric missing → render "–".
- Empty pill groups (transitions, keywords, buzzwords, repetition) → "none".
- Empty improvements list → "None — nice work.".
- Empty strengths → backend already substitutes a default; render as given.
- Empty/blank chart data → blank chart.
- Heuristic content method (Ollama down) → still render scores + feedback, with
  the method label visible.
- DB down → no auth UI, no leaderboard, core analysis still fully usable.
- ElevenLabs absent → no Ideal Delivery card at all.

---

## 8. Accessibility & interaction (functional only)
- All actionable controls reachable and operable by keyboard.
- Modals close on Escape (chart modal) and on backdrop click (both modals).
- Inputs have associated labels/placeholders and appropriate `autocomplete`
  (username/email/current-password).
- Status/progress changes should be perceivable (text updates at minimum).
- All injected dynamic text is escaped (XSS) — see Section 0.

---

## 9. Backend API contract (authoritative reference)

All requests/responses are JSON unless noted. The UI must not assume any optional
field is present.

### `GET /health`
Returns runtime/config status. UI reads at least `min_recording_sec`.
```json
{
  "status": "ok",
  "transcribe_backend": "whisperx",
  "whisper_model": "base",
  "llm": "ollama (local)",
  "llm_model": "llama3.1",
  "ollama_host": "http://localhost:11434",
  "ffmpeg_found": true,
  "db_available": true,
  "min_recording_sec": 15,
  "elevenlabs_available": false,
  "elevenlabs_voice_id": null
}
```

### `POST /analyze`  (multipart/form-data, field `audio`)
- **202** → `{ "job_id": "<hex>" }`
- **400** → `{ "error": "..." }` (no file / empty name / unsupported type)
- **413** → `{ "error": "File too large. Maximum upload size is 200 MB." }`

### `GET /analyze/status/<job_id>`
- `{ "state": "processing" }`
- `{ "state": "done", "result": <ResultObject> }`
- `{ "state": "error", "error": "..." }`
- **404** → `{ "state": "unknown", "error": "..." }`

**`<ResultObject>` shape:**
```jsonc
{
  "transcript": "string",
  "language_detected": "en",
  "duration_sec": 123.4,
  "word_count": 540,
  "scores": {
    "overall": 0-100|null,
    "delivery": 0-100|null,
    "language": 0-100|null,
    "content": 0-100|null,
    "weights": { "delivery": 0.40, "language": 0.25, "content": 0.35 }
  },
  "delivery": {
    "rate":    { "available": true, "wpm": 0, "ideal_range": [120,150],
                 "timeline": [{"t":0,"wpm":0,"label":"ok|too_fast|too_slow"}],
                 "too_fast_windows": 0, "too_slow_windows": 0, "score": 0 },
    "pitch":   { "available": true, "mean_hz": 0, "std_hz": 0, "semitone_std": 0,
                 "variability_score": 0, "monotone": false,
                 "timeline": [{"t":0,"hz":0|null}], "score": 0 },
    "volume":  { "available": true, "mean_db": 0, "std_db": 0,
                 "consistency_score": 0, "quiet_pct": 0, "loud_pct": 0,
                 "timeline": [{"t":0,"db":0}], "score": 0 },
    "pauses":  { "available": true, "total_pauses": 0, "pauses_per_minute": 0,
                 "avg_pause_sec": 0, "strategic": 0, "long_awkward": 0,
                 "hesitation": 0, "silence_ratio": 0|null,
                 "timeline": [{"t":0,"duration":0,"type":"strategic|long_awkward|hesitation|normal"}],
                 "score": 0 },
    "fillers": { "available": true, "total": 0, "per_minute": 0,
                 "by_word": {"um": 3}, "timestamps": [{"word":"um","t":0}], "score": 0 }
  },
  "language": {
    "transitions": { "available": true, "total": 0, "density_per_100w": 0,
                     "by_phrase": {"first": 1}, "score": 0 },
    "buzzwords":   { "available": true, "advisory": true, "total": 0,
                     "density_per_100w": 0, "by_word": {"synergy": 2},
                     "overused": {}, "suggestions": {"synergy":"cooperation"},
                     "suppressed": {}, "reviewed": true },
    "repetition":  { "available": true, "repeated_words": {}, 
                     "repeated_sentence_starters": {}, "repeated_phrases": {},
                     "top_word_share": 0, "score": 0 },
    "keywords":    { "available": true,
                     "keywords": [{"word":"data","count":5}],
                     "reinforced_concepts": 0 },
    "rhythm":      { "available": true, "sentence_count": 0,
                     "avg_sentence_words": 0, "sentence_length_stdev": 0,
                     "varied": false }
  },
  "content": {
    "available": true, "method": "llm|heuristic|heuristic (LLM unavailable)",
    "model": "llama3.1",
    "categories": {
      "introduction": {"score":0,"feedback":"..."},
      "thesis":       {"score":0,"feedback":"..."},
      "evidence":     {"score":0,"feedback":"..."},
      "organization": {"score":0,"feedback":"..."},
      "conclusion":   {"score":0,"feedback":"..."}
    },
    "summary": "string",
    "score": 0,
    "llm_error": "string (only if fallback)"
  },
  "feedback": {
    "strengths": ["..."],
    "improvements": ["..."],
    "top_recommendations": ["..."]
  },
  "warnings": ["..."],
  "ideal_delivery_available": false,
  "saved_to_leaderboard": true
}
```
> Any analyzer can instead return `{ "available": false, "reason": "..." }` — the
> UI must treat such sections as "no data" and render placeholders/empty charts.

### `POST /api/ideal-delivery`  `{ "transcript": "..." }`
```json
{
  "script": "rewritten text",
  "method": "llm | heuristic (LLM unavailable)",
  "voice_id": "id | null",
  "audio": "data:audio/mpeg;base64,... | null",
  "audio_error": "string (optional)",
  "note": "string (optional, when ElevenLabs not configured)"
}
```
- **400** → `{ "error": "No transcript provided." }`

### `GET /api/voices`
`{ "voices": [ { "voice_id": "...", "name": "...", "category": "..." } ] }`
or `{ "error": "...", "voices": [] }`. (Used only if you build a voice picker;
the core UI does not require it.)

### `POST /api/register`  `{ username, email?, password }`
- **201** → `{ "user": { "id", "username", "email" } }` (also logs in)
- **400** → `{ "error": "Username must be 3-30 characters: letters, numbers, . _ -" }`
  or `{ "error": "Password must be at least 6 characters." }`
- **409** → `{ "error": "That username or email is already taken." }`
- **503** → `{ "error": "<DB unavailable message>" }`

### `POST /api/login`  `{ username, password }`
- **200** → `{ "user": { "id", "username", "email" } }`
- **401** → `{ "error": "Invalid username or password." }`
- **503** → DB unavailable.

### `POST /api/logout`
- **200** → `{ "ok": true }`

### `GET /api/me`
- `{ "user": {id,username,email} | null, "db_available": true|false }`

### `GET /api/leaderboard`
- `{ "leaderboard": [ {rank, username, best_score, attempts, is_me} ] }`
- DB down → `{ "error": "...", "leaderboard": [] }` (HTTP 200) → UI hides card.

---

## 10. Checklist (everything that must exist)
- [ ] Header with title + description
- [ ] Auth bar (3 states: user / sign-in / hidden)
- [ ] Footer
- [ ] Input screen: file upload + recorder + analyze button + selection text
- [ ] Recorder with min-length enforcement + live status
- [ ] Loading screen with updating progress text
- [ ] Error screen
- [ ] Saved-to-leaderboard note (3 states)
- [ ] Score summary: overall gauge + 3 sub-score bars
- [ ] Key metrics (8)
- [ ] Top 3 recommendations + strengths + improvements
- [ ] Ideal Delivery card: sentence picker, selection info, clear, generate/regenerate, status, audio player, collapsible script
- [ ] Six charts, each click-to-enlarge
- [ ] Content & Structure: summary + 5 category cards
- [ ] Language Details: transitions, keywords, buzzwords (+suggestions/suppressed), repetition
- [ ] Transcript
- [ ] Warnings line
- [ ] Leaderboard table (hideable)
- [ ] Auth modal (login/register tabs, fields, validation, dismissal, DB guard)
- [ ] Chart enlarge modal (Escape + backdrop close)
- [ ] Async analysis polling flow
- [ ] All optional-feature gating
- [ ] HTML escaping of all dynamic text
