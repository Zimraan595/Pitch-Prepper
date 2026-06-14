# 🎤 Presentation Helper

**Your personal AI speaking coach.** Upload or record a talk and, in about a
minute, get a scored dashboard with concrete, actionable feedback on your
**delivery**, **language**, and **content** — all processed **locally on your own
machine**. No accounts, no API keys, nothing leaves your computer.

---

## What you get

Give it a presentation and it hands back:

- 🎯 **An overall score** out of 100, broken down into Delivery, Language, and Content.
- 📊 **Six visual charts** — speaking-rate timeline, pitch, volume, pauses, filler
  words, and a content radar.
- ✅ **Strengths, areas to improve, and your top-3 highest-impact fixes** in plain English.
- 📝 **A full transcript** of what you said.

What it listens for:

| Area | What it measures |
|------|------------------|
| **Delivery** | Speaking pace (WPM), pitch variation (monotone detection), volume consistency, strategic vs. awkward pauses, and filler words (*um, uh, like, you know…*). |
| **Language** | Transition phrases, overused buzzwords (with plainer suggestions), repeated words/phrases, keyword reinforcement, and sentence rhythm. |
| **Content** | Written feedback + scores for your introduction, thesis, evidence, organization, and conclusion. |

---

## 🚀 Quick start

> **Platform note:** Commands below are written for **Windows (PowerShell)**, since
> that's the most common setup. macOS/Linux equivalents are noted where they differ.

### 1. Prerequisites

You need three things installed **before** running the app:

#### a) Python 3.9+
Check with `python --version`. If you don't have it, get it from
[python.org](https://www.python.org/downloads/) (tick *"Add Python to PATH"* during install).

#### b) ffmpeg  ← **required, this is the #1 thing people miss**
The app uses `ffmpeg` to read your audio. Without it, every analysis fails with
`Analysis failed: [WinError 2] The system cannot find the file specified`.

```powershell
winget install Gyan.FFmpeg.Essentials
```

- The **Essentials** build is all you need — the app only *decodes* audio, and
  every format it accepts is covered. (The much larger "full" build also works if
  you already have it, but it's unnecessary.)
- **macOS:** `brew install ffmpeg` · **Debian/Ubuntu:** `sudo apt install ffmpeg`

> ⚠️ **After installing ffmpeg, close your terminal and open a brand-new one.**
> Windows only updates the `PATH` for *newly* opened terminals — if you reuse the
> same window (or restart the app in it), it still won't find ffmpeg and you'll see
> `[WinError 2]` again. This trips up almost everyone. Verify it's visible with:
> ```powershell
> ffmpeg -version
> ```

#### c) Ollama  *(optional — for AI content feedback)*
Content scoring works without it (a built-in heuristic fills in), but for the
richer LLM-written feedback, install [Ollama](https://ollama.com) and pull the model:

```powershell
ollama pull llama3.1
ollama serve          # leave running in its own terminal
```

### 2. Install the app

```powershell
cd backend
pip install -r requirements.txt
```

This pulls in WhisperX, Librosa, and PyTorch — it's a large download the first time.

### 3. Run it

```powershell
python app.py
```

Then open **http://localhost:5000** in your browser. 🎉

### ⏳ Heads up: the *first* analysis is slow

The first time you analyze something, WhisperX **downloads a speech-alignment model
(a few hundred MB)** and PyTorch warms up. If the logs seem to pause right after a
line like `Detected language: en (0.99)`, **it is not frozen** — it's downloading
in the background and/or running the local LLM. Give it a couple of minutes. Every
run after that is much faster.

---

## 🎬 Using the app — step by step

Everything below happens in your browser at **http://localhost:5000**.

### 1. Give it your talk

On the **Home** screen you have two options: **Upload an audio file**
(`wav, mp3, m4a, mp4, ogg, flac, webm, aac, opus`, up to 200 MB) or click
**Start Recording** to capture yourself live (recordings need to be at least
~15 seconds). Then press **Analyze Presentation**.

### 2. (Optional) Sign in to save your scores

Click **Login / Sign Up** in the top-right to create an account (username +
password; email optional). While you're logged in, every analysis is
automatically recorded and ranked on the global leaderboard. This needs MongoDB —
skip it and the core analysis still works exactly the same.

### 3. Read your results dashboard

After about a minute you get your **Overall Score** (0–100), broken out into
**Delivery**, **Language**, and **Content**, plus your **Top 3 Recommendations**
and a list of strengths and areas to improve. The score ring and bars are
color-coded — green (good ≥ 75), amber (fair 55–74), and red (needs work < 55).

### 4. Dive into the detailed analytics

Scroll to the **Charts** for the six visualizations — speaking rate (WPM), pitch
variation, filler words, pause timeline, content effectiveness, and volume
dynamics. **Click any chart to enlarge it.**

The full dashboard also includes a **Content & Structure** breakdown
(intro, thesis, evidence, organization, conclusion), **Language Details**
(transitions, buzzwords, repeated words/phrases, keywords), and the complete
**Transcript** of what you said.

### 5. Climb the leaderboard

The **Leaderboard** tab ranks everyone by their best overall score, so you can
track your progress over time and compete. Your own row is highlighted.

---

## 📈 Understanding your scores

Your **overall score** is a weighted blend:

- **Delivery — 40%** · *how* you speak: pace, pitch variety, volume, pauses, fillers.
  Aim for **120–150 WPM**, varied pitch (not monotone), steady volume, and few fillers.
- **Language — 25%** · *the words* you choose: clear transitions, plain language over
  buzzwords, and not over-repeating yourself.
- **Content — 35%** · *what* you say: a clear opening, a stated main message, evidence,
  logical organization, and a real conclusion.

Each metric is scored against research-backed targets, so a lower number always comes
with specific feedback on how to raise it.

---

## 🛠️ Troubleshooting

<table>
<tr><th>What you see</th><th>What it means & how to fix it</th></tr>

<tr>
<td><code>Analysis failed: [WinError 2] The system cannot find the file specified</code></td>
<td><b>ffmpeg isn't installed or isn't on your PATH.</b> Install it (<code>winget install Gyan.FFmpeg.Essentials</code>) and then <b>open a NEW terminal</b> before running the app again.</td>
</tr>

<tr>
<td>Still <code>[WinError 2]</code> even though you just installed ffmpeg</td>
<td>You're in a terminal that started <i>before</i> the install, so it has a stale PATH. <b>Close it, open a new one</b>, confirm with <code>ffmpeg -version</code>, then <code>python app.py</code> again.</td>
</tr>

<tr>
<td><code>torchcodec is not installed correctly…</code> / <code>Could not load libtorchcodec</code> / <code>FFmpeg version 4/5/6/7…</code></td>
<td><b>Harmless warning — ignore it.</b> A sub-component (torchcodec) wants ffmpeg's shared libraries and only supports ffmpeg 4–7; you likely have ffmpeg 8. WhisperX doesn't need torchcodec — it decodes audio with the ffmpeg program directly — so analysis runs fine anyway. If transcription proceeds (you'll see a <code>Detected language</code> line), everything is working.</td>
</tr>

<tr>
<td>Logs seem stuck after <code>Detected language: en (0.99)</code></td>
<td><b>Not stuck — it's the first-run model download.</b> WhisperX is fetching a few-hundred-MB alignment model and/or waiting on the local LLM. Wait a couple of minutes; later runs are fast.</td>
</tr>

<tr>
<td>Content feedback says <i>"heuristic (LLM unavailable)"</i></td>
<td>Ollama isn't running, so the app used its built-in fallback. That's fine — for AI-written feedback, start Ollama: <code>ollama pull llama3.1</code> then <code>ollama serve</code>, and re-analyze.</td>
</tr>

<tr>
<td><code>Lightning automatically upgraded your loaded checkpoint…</code></td>
<td>Informational only — no action needed.</td>
</tr>

<tr>
<td><code>File too large</code></td>
<td>Uploads are capped at 200 MB. Trim or compress the audio, or record a shorter segment.</td>
</tr>
</table>

---

## 🏆 Accounts & leaderboard (optional)

Sign in to track your progress and compete on a **global leaderboard**.

- **Sign up / log in** from the top-right of the page (username + password; email optional).
- While logged in, **every analysis you run is automatically recorded**, and the
  leaderboard ranks each user by their **best overall score**.
- Logins use signed session cookies; passwords are stored **hashed** (never in plain text).

This feature needs **MongoDB**. The rest of the app works fine without it — if
MongoDB isn't reachable, the sign-in buttons simply report it's unavailable.

```powershell
# Easiest: run MongoDB in Docker
docker run -d -p 27017:27017 --name mongo mongo:7
```

Or install MongoDB Community Server from [mongodb.com](https://www.mongodb.com/try/download/community),
or point `MONGO_URI` at a free MongoDB Atlas cluster. Then set a real `SECRET_KEY`
(see Configuration) and restart the app.

---

## 🔊 Hear how it could sound (optional)

After an analysis, a **"Hear how it could sound"** card can render a polished take
on *your* talk: a local LLM rewrites your transcript — fillers trimmed, phrasing
tightened, your meaning and first-person voice kept — and [ElevenLabs](https://elevenlabs.io)
reads it back in a clear, well-paced voice. It's your own words, delivered well.

You can **click sentences in the card to improve just one part** of your talk
(say, only your conclusion) instead of the whole thing — leave them unselected to
redo everything. Improving a shorter selection is also noticeably faster, since
the rewrite time scales with length.

> ⚠️ **This is the one feature that leaves your machine.** Everything else runs
> locally; this sends your transcript text to ElevenLabs' API. So it's **off by
> default and opt-in**: the card only appears when you set an API key, and audio
> is generated **only when you click the button** — never during a normal analysis.

```powershell
$env:ELEVENLABS_API_KEY = "sk_your_key_here"   # get one at elevenlabs.io
python app.py
```

The LLM rewrite uses a small local Ollama model — **`llama3.2:3b`** by default
(`ollama pull llama3.2:3b`), separate from the `llama3.1` used for content
analysis. The rewrite scales with transcript length, so a smaller model is far
faster on long talks (~2.4× in local testing) while cleaning up filler just as
well. It falls back to a simple filler-stripping pass if Ollama isn't running, so
the *rewrite* stays local even when the *voice* doesn't. Override with
`IDEAL_REWRITE_MODEL`.

### Choosing a voice (and the free-tier gotcha)

On the **free ElevenLabs API tier you cannot use the shared "library"/premade
voices** (Rachel, etc.) — the API returns `HTTP 402 payment_required`. You must
synthesize with a voice **you own**. The free tier lets you make one: go to
[elevenlabs.io](https://elevenlabs.io) → **VoiceLab → Instant Voice Clone** (a
~1-minute recording of your own voice — which is perfect here: you'll hear the
improved script in *your* voice). Then either:

- leave `ELEVENLABS_VOICE_ID` unset — the app auto-picks one of your own voices, or
- set `ELEVENLABS_VOICE_ID` to a specific voice. List the IDs your account can use
  at **`GET /api/voices`** (or in VoiceLab).

Paid plans can use the built-in library voices directly, so any `ELEVENLABS_VOICE_ID` works.

---

## ⚙️ Configuration (optional)

Everything works out of the box, but you can tune behavior with environment
variables. In PowerShell, set one with `$env:NAME = "value"` before `python app.py`
(on macOS/Linux, `export NAME=value`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `WHISPER_MODEL` | `base` | Accuracy vs. speed: `tiny`·`base`·`small`·`medium`·`large-v3`. Bigger = better but slower. |
| `TRANSCRIBE_BACKEND` | `whisperx` | `whisperx` (accurate word timings) or `whisper` (lighter fallback). |
| `WHISPERX_DEVICE` | `auto` | `auto`·`cpu`·`cuda`. Use `cuda` if you have a compatible NVIDIA GPU. |
| `WHISPERX_COMPUTE_TYPE` | auto | e.g. `int8` (CPU) or `float16` (GPU). |
| `LLM_MODEL` | `llama3.1` | Which Ollama model to use for content analysis. |
| `IDEAL_REWRITE_MODEL` | `llama3.2:3b` | Smaller/faster Ollama model used **only** for the "Hear how it could sound" rewrite. The rewrite scales with transcript length, so an 8B model is slow on long talks; a 3B model cleans up just as well. Run `ollama pull llama3.2:3b`. |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening. |
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection for accounts & leaderboard. |
| `MONGO_DB_NAME` | `presentation_helper` | Database name to use. |
| `SECRET_KEY` | `dev-insecure-change-me` | Signs login session cookies — **set a real value in production.** |
| `SESSION_DAYS` | `30` | How long a login stays valid (persistent across browser restarts). |
| `MIN_RECORDING_SEC` | `15` | Minimum seconds of audio; shorter clips are rejected to prevent low-effort uploads. |
| `ELEVENLABS_API_KEY` | *(unset)* | Enables the optional **"Hear how it could sound"** playback. When unset, the feature is hidden and nothing is ever sent off-machine. |
| `ELEVENLABS_VOICE_ID` | *(auto)* | Voice that reads the polished script. Leave unset to auto-pick a voice you own from your account. **Free API tier:** you must use your own voice — see the note below. |
| `ELEVENLABS_MODEL` | `eleven_multilingual_v2` | ElevenLabs text-to-speech model. |
| `IDEAL_DELIVERY_MAX_CHARS` | `5000` | Caps the transcript length sent for rewrite + synthesis (latency / credit control). |
| `PORT` | `5000` | Port the web app runs on. |

**Tip:** on a CPU-only machine, `WHISPER_MODEL=tiny` or `small` makes analysis
noticeably faster.

---

## 👩‍💻 For developers

**Tech stack:** Flask (single-file backend `app.py`) · WhisperX · Librosa · Ollama / Llama 3.1 (local) · ElevenLabs (optional TTS) · HTML/CSS/JS · Chart.js

**Architecture.** The backend is intentionally one file with a **modular** structure:
each analyzer (`analyze_speaking_rate`, `analyze_pitch`, `analyze_volume`,
`analyze_pauses`, `analyze_fillers`, `analyze_transitions`, `analyze_buzzwords`,
`analyze_repetition`, `analyze_content`, …) is an independent function that takes
transcript/audio data and returns a JSON-serializable dict. `run_analysis()`
orchestrates them, so new analyzers can be added without touching the others. Heavy
dependencies (Whisper, Librosa, NumPy, the local LLM) are imported lazily — if one is
unavailable, that analyzer is skipped with a warning rather than crashing the request.

**API**

| Method | Route       | Description                                  |
|--------|-------------|----------------------------------------------|
| GET    | `/`         | Dashboard UI                                 |
| GET    | `/health`   | Status + which models/services are configured (incl. `db_available`) |
| POST   | `/analyze`  | `multipart/form-data` field `audio` → `{job_id}` (analysis runs in the background; poll status) |
| GET    | `/analyze/status/<job_id>` | `{state: processing\|done\|error}`; when `done`, the results JSON is under `result` (records to leaderboard if logged in) |
| POST   | `/api/ideal-delivery` | `{transcript}` → polished script + (if ElevenLabs is configured) base64 MP3 audio |
| GET    | `/api/voices` | ElevenLabs voices available to the configured account (id, name, category) |
| POST   | `/api/register` | `{username, email?, password}` → create account + log in |
| POST   | `/api/login`    | `{username, password}` → start a session |
| POST   | `/api/logout`   | End the session |
| GET    | `/api/me`       | Current user + whether MongoDB is reachable |
| GET    | `/api/leaderboard` | Global ranking of each user's best score |

Accounts & leaderboard are backed by **MongoDB** (`pymongo`), in a self-contained
module (`get_db`, `current_user`, `_record_result`, and the `/api/*` routes) — and,
like the analysis features, degrade gracefully when the database is unavailable.

**Future extensions (placeholders by design).** Silero VAD, LanguageTool
grammar/readability, Hugging Face sentiment/confidence, and webcam-based
eye-contact/body-language analysis can be added as new analyzer functions following
the same contract.
