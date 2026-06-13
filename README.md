# Presentation-Helper

An AI presentation coach. Upload or record a talk and get actionable feedback on
**delivery**, **language**, and **content quality** — with a scored dashboard and
charts.

## Features

**Speech processing**
- Whisper speech-to-text with word-level timestamps; full transcript stored for analysis.

**Delivery quality**
- **Speaking rate** — Words Per Minute, too-fast/too-slow sections, vs. recommended range.
- **Pitch** — fundamental frequency via Librosa, variability score, monotone detection.
- **Volume** — RMS loudness, quiet/loud sections, consistency score.
- **Pauses** — gap + silence detection, classified as strategic / long-awkward / hesitation.
- **Filler words** — counts, timestamps, and fillers-per-minute (um, uh, like, you know, …).

**Language quality**
- **Transitions** — detects connective phrases (first, however, therefore, in conclusion …).
- **Buzzwords** — flags overused jargon and suggests clearer alternatives.
- **Repetition** — repeated words, phrases, and sentence starters.
- **Keywords & rhythm** — key-concept reinforcement and sentence-length variation.

**Content & structure (LLM or heuristic)**
- Scores + written feedback for introduction, thesis, evidence, organization, conclusion.
- Uses the OpenAI API when `OPENAI_API_KEY` is set; otherwise a transparent heuristic fallback.

**Dashboard**
- Overall weighted score (delivery / language / content) + key metric cards.
- Chart.js visualizations: WPM timeline, pitch, volume, pause timeline, filler words, content radar.
- Strengths, areas to improve, and the top-3 highest-impact recommendations.

## Tech stack

Flask (single-file backend `app.py`) · Whisper · Librosa · OpenAI API · HTML/CSS/JS · Chart.js

## Setup

```bash
cd backend
pip install -r requirements.txt        # needs the ffmpeg system binary on PATH

# optional: richer LLM content analysis
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini        # optional, default shown
export WHISPER_MODEL=base              # tiny|base|small|medium|large

python app.py                          # http://localhost:5000
```

## Architecture

The backend is intentionally a single file with a **modular** structure: each
analyzer (`analyze_speaking_rate`, `analyze_pitch`, `analyze_volume`,
`analyze_pauses`, `analyze_fillers`, `analyze_transitions`, `analyze_buzzwords`,
`analyze_repetition`, `analyze_content`, …) is an independent function that takes
transcript/audio data and returns a JSON-serializable dict. `run_analysis()`
orchestrates them, so new analyzers can be added without touching the others.

Heavy/optional dependencies (Whisper, Librosa, NumPy, OpenAI) are imported
lazily — if one is unavailable, that analyzer is skipped with a warning rather
than crashing the request.

### API

| Method | Route       | Description                                  |
|--------|-------------|----------------------------------------------|
| GET    | `/`         | Dashboard UI                                 |
| GET    | `/health`   | Status + which models are configured         |
| POST   | `/analyze`  | `multipart/form-data` field `audio` → results JSON |

### Future extensions (placeholders by design)

Silero VAD, LanguageTool grammar/readability, Hugging Face sentiment/confidence,
and webcam-based eye-contact/body-language analysis can be added as new analyzer
functions following the same contract.
