# Pitch Prepper — UI Build Prompt & System Explanation

**Use this as the prompt/brief when building the frontend.** It explains *how the
pieces fit together and why*, then tells you what to build. The exhaustive,
field-level checklist lives in [`UI_REQUIREMENTS.md`](./UI_REQUIREMENTS.md) — this
document is the "understanding" layer that makes that checklist make sense.

---

## The prompt (paste this to a builder)

> You are building the web frontend for **Pitch Prepper**, an AI speaking coach.
> A user gives the app a recording of a talk (upload or live mic) and gets back a
> scored dashboard coaching their **delivery, language, and content**. The Python
> Flask backend already exists and is fixed — you are only building the UI that
> talks to it. Do **not** change the API; consume it exactly as specified in
> `UI_REQUIREMENTS.md` §9. Implement every screen, state, flow, and conditional in
> that document. Match the existing behavior; the visual design is yours to decide.
> The three rules you cannot get wrong: (1) analysis is **asynchronous** — submit,
> get a job id, then poll for the result; (2) three features are **optional and
> must hide themselves** when their backend dependency is absent; (3) **escape all
> dynamic text** to prevent XSS. Details and rationale below.

---

## 1. What the app actually is (the big picture)

Pitch Prepper turns a single audio recording into a coaching report. The whole
product is one loop:

```
record/upload a talk  →  the server transcribes + analyzes it  →  the UI shows a
scored dashboard (numbers, charts, written feedback)  →  (optionally) the user
saves the score to a leaderboard and/or hears an AI-polished version of their talk
```

Everything in the UI exists to serve one of three moments in that loop:
1. **Give the app a talk** (the Input screen).
2. **Wait while it thinks** (the Loading screen — analysis is slow, minutes on a
   cold start).
3. **Read the report** (the Results dashboard — the heart of the product).

Two side features wrap around that loop: **accounts + a leaderboard** (compete on
your best score) and **"hear how it could sound"** (text-to-speech of a rewritten
script). Both are optional.

---

## 2. The architecture, and how front and back fit together

- **Backend:** a single Flask app (`backend/app.py`). It exposes JSON endpoints.
  It does the heavy lifting — Whisper transcription, audio analysis (pitch,
  volume, pauses), language analysis, and an LLM (or heuristic) content review.
- **Frontend:** a browser client (currently one HTML page + one JS file) that
  calls those endpoints with `fetch` and renders the results. **No server-side
  rendering of data** — the page is a shell that fills itself in from JSON.

The contract between them is the set of endpoints in §9 of the requirements. As
long as the UI calls those and reads the documented fields, the design and
framework are entirely your choice.

```
Browser (your UI)                         Flask backend
  │   POST /analyze (audio file)  ───────►  saves file, starts background job
  │   ◄───────────────  202 { job_id }
  │   GET /analyze/status/<id>    ───────►  "processing" / "done" / "error"
  │   ◄───────────────  result JSON when done
  │   render dashboard
```

### Why analysis is async (don't skip this)
Transcription + analysis can run for **minutes** (the first run downloads models).
A single long-held HTTP request gets dropped by browsers/proxies and looks like a
"network error" even though the server finished. So the backend returns a **job
id immediately**, runs the work in a background thread, and the UI **polls** a
cheap status endpoint every ~2 seconds until it's `done` or `error`. Build the
loading screen around this polling loop, not around one awaited request. (Ceiling
~20 min, tolerate transient poll failures, treat a 404 as "job expired.")

---

## 3. The end-to-end user journey (how the screens connect)

```
                     ┌────────────────────────────────────────────┐
   page load  ──►    │  Input screen  (upload OR record + Analyze) │
                     └───────────────┬────────────────────────────┘
                                     │ click "Analyze"
                                     ▼
                     ┌────────────────────────────────────────────┐
                     │  Loading screen (spinner + polling status)  │
                     └───────┬───────────────────────┬────────────┘
                       error │                    done│
                             ▼                        ▼
                  ┌──────────────────┐   ┌────────────────────────────────────┐
                  │  Error screen    │   │  Results dashboard                  │
                  │  (message+retry) │   │  scores → metrics → recs → charts → │
                  └──────────────────┘   │  content → language → transcript    │
                                         └──────────────┬─────────────────────┘
                                                        │ (logged in?)
                                                        ▼
                                         saved-to-leaderboard note + refresh board
```

Overlays that can appear at any time:
- **Auth modal** — login/register (only when accounts are available).
- **Chart enlarge modal** — click any chart to zoom it.

The **leaderboard** and the **auth bar** live in the shell and are present
alongside whichever main screen is showing.

---

## 4. How a recording becomes the dashboard (the data flow)

This is the spine of the app — understand this and the dashboard sections stop
looking like a random pile of widgets:

1. The user provides audio → the UI uploads it to `POST /analyze`.
2. The backend transcribes it into **words with timestamps** and a full text.
3. Independent analyzers each turn that into a small JSON object:
   - **Delivery** (how they speak): speaking rate, pitch, volume, pauses, fillers.
   - **Language** (the words): transitions, buzzwords, repetition, keywords, rhythm.
   - **Content** (what they say): intro, thesis, evidence, organization, conclusion.
4. A scorer blends those into an **overall score** (Delivery 40% / Language 25% /
   Content 35%) and a feedback synthesizer produces **strengths, improvements, and
   top-3 recommendations**.
5. All of that comes back as one `result` object (§9). **Every dashboard section
   is just a view onto one branch of that object:**

| Dashboard section | Reads from |
|---|---|
| Overall gauge + 3 bars | `result.scores` |
| 8 key-metric cards | `result.delivery.*`, `result.content.score`, `duration_sec`, `word_count` |
| Top-3 / strengths / improvements | `result.feedback` |
| WPM / pitch / volume / pause / filler charts | `result.delivery.*.timeline` & `fillers.by_word` |
| Content radar + category cards | `result.content.categories` |
| Language details (pills) | `result.language.*` |
| Transcript | `result.transcript` |
| Warnings | `result.warnings` |

So the dashboard is not bespoke logic per widget — it's a **deterministic
projection of one JSON document**. Build a `render(result)` that fans the object
out into the sections.

---

## 5. The three optional features and graceful degradation (a core principle)

The app is designed to run with **zero external dependencies** and light up extra
features only when their dependency is present. The UI's job is to **detect and
hide**, never to show a broken control. There are exactly three:

| Feature | Backend dependency | How the UI knows | If absent |
|---|---|---|---|
| Accounts + leaderboard | MongoDB | `db_available` in `/api/me` & `/health`; `error` in `/api/leaderboard` | Hide auth bar sign-in + the whole leaderboard card |
| "Hear how it could sound" | ElevenLabs API key | `ideal_delivery_available` in the result + `elevenlabs_available` in `/health` | Hide the ideal-delivery card entirely |
| Richer content feedback / script rewrite | Ollama (local LLM) | `content.method` = `"heuristic …"` | Still show everything; just surface the "heuristic" method label |

This is why the UI calls `/health`, `/api/me`, and `/api/leaderboard` on load —
to learn which features to reveal. **Assume nothing is available until the server
says so.**

---

## 6. The optional sub-flows (how they hang off the main loop)

**Accounts/leaderboard:** logging in is *not* required to analyze. It only adds
persistence — when logged in, each analysis is auto-recorded and the leaderboard
ranks each user by their **best** overall score. So the auth modal, auth bar, the
"saved to leaderboard" note, and the leaderboard card are all one feature seen
from different angles. After login/register/logout/saved-analysis → refresh the
board.

**"Hear how it could sound":** appears as a card *inside* the results, but it's a
**separate on-demand call** (`POST /api/ideal-delivery`), never part of `/analyze`
— because it's the only feature that sends data off-machine (to ElevenLabs) and
costs credits. The user can click individual transcript sentences to improve just
that part (faster), or improve the whole talk. The response is a rewritten
**script** plus, if a voice is configured, **base64 MP3 audio** to play inline.

---

## 7. What you must get right (the non-negotiables)

1. **Async polling**, not a single long request (§2, §4).
2. **Graceful degradation** — gate the three optional features (§5).
3. **Escape all dynamic text** (transcript, feedback, usernames, errors) — it's
   user/model-generated and goes straight into the DOM.
4. **Every section handles "no data"** — any analyzer can return
   `{available:false}`; charts render empty, metrics show "–", pill groups show
   "none". Never crash on a missing field.
5. **Errors are surfaced verbatim** — the backend returns actionable messages
   (ffmpeg missing, file too large, too short, job expired). Show them; don't
   swallow them.
6. **Client-side validation mirrors the server** (file types, 200 MB cap, min
   recording length, username/password rules) for instant feedback — but the
   server is the source of truth.

---

## 8. Suggested build order

1. **App shell** — header, auth bar placeholder, footer; wire the on-load calls
   to `/health`, `/api/me`, `/api/leaderboard`.
2. **Input screen** — file picker + recorder + Analyze button + validation.
3. **Analysis flow** — submit, then the polling loop with the loading screen and
   the error screen.
4. **Results `render(result)`** — scores → metrics → feedback → charts → content
   → language → transcript → warnings. (Get the data fan-out right first, polish
   later.)
5. **Charts** — the six visualizations + click-to-enlarge modal.
6. **Accounts + leaderboard** — auth modal, auth bar states, saved note, board.
7. **Ideal delivery card** — sentence picker, generate, audio player.
8. **Edge/empty states + accessibility pass** (§7, §8 of the requirements).

---

## 9. Where to find the details

- **Field-by-field UI spec, every screen, every state, full API schemas:**
  [`UI_REQUIREMENTS.md`](./UI_REQUIREMENTS.md)
- **The backend itself (source of truth for the contract):** `backend/app.py`
- **End-user feature description & scoring rationale:** `README.md`
