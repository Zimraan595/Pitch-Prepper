"""Pitch Prepper — single-file Flask backend.

A presentation coaching web application. A user uploads or records a
presentation audio file; the app transcribes it (Whisper), analyzes delivery
(speaking rate, pitch, volume, pauses, filler words), language quality
(transitions, buzzwords, repetition, keywords) and content/structure (LLM or
heuristic fallback), then returns a scored dashboard with visualization data.

Design goals
------------
* Single-file backend (`app.py`) as requested.
* Modular: each analysis concern is an independent, pure-ish function that
  takes transcript/audio data and returns a JSON-serializable dict. New
  analyzers can be added without touching the others.
* Graceful degradation: heavy/optional dependencies (whisper, librosa, numpy)
  and the local LLM are used lazily. If one is unavailable, that analyzer is
  skipped or falls back, instead of crashing the whole request.

Run
---
    pip install -r requirements.txt
    python app.py
    # open http://localhost:5000
"""

from __future__ import annotations

import os
import re
import json
import math
import time
import uuid
import shutil
import datetime
import threading
import tempfile
import statistics
import concurrent.futures
from functools import wraps
from collections import Counter

from flask import Flask, request, jsonify, render_template, session

# Load config from a local .env file (backend/.env) if present, so MONGO_URI /
# SECRET_KEY / API keys work no matter how the app is launched (Git Bash, an IDE
# "Run" button, double-click). Pointed at the file next to this module so it
# loads regardless of cwd. override=True makes the .env authoritative: a stale
# shell `export` (e.g. an old MONGO_URI left in a terminal) can't silently shadow
# it. Optional: if python-dotenv isn't installed, real env vars are still used.
try:
    from dotenv import load_dotenv
    load_dotenv(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        override=True,
    )
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload cap

# Transcription backend: "whisperx" (default — adds forced alignment for
# accurate word timestamps) or "whisper" (the original openai-whisper).
TRANSCRIBE_BACKEND = os.environ.get("TRANSCRIBE_BACKEND", "whisperx").lower()

# Whisper model size: tiny/base/small/medium/large-v2/large-v3.
# Smaller = faster, less RAM. WhisperX works with any of these.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

# WhisperX runtime. Device "auto" picks CUDA when available, else CPU.
# compute_type defaults to float16 on GPU, int8 on CPU (override if needed).
WHISPERX_DEVICE = os.environ.get("WHISPERX_DEVICE", "auto")
WHISPERX_COMPUTE_TYPE = os.environ.get("WHISPERX_COMPUTE_TYPE", "")
WHISPERX_BATCH_SIZE = int(os.environ.get("WHISPERX_BATCH_SIZE", "16"))

# Content analysis uses a local LLM via Ollama — fully on-device, no API key,
# nothing sent to any external service. If Ollama isn't running, content
# analysis falls back to a built-in heuristic.
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Fixed seed paired with temperature 0 for deterministic greedy decoding, so the
# same transcript yields the same content analysis on repeated runs.
LLM_SEED = int(os.environ.get("LLM_SEED", "0"))
# The "ideal delivery" rewrite is a simple text-cleanup task (strip fillers,
# tighten phrasing) rather than reasoning, so it uses a smaller/faster Ollama
# model than the content analysis above. This matters because the rewrite scales
# with transcript length — an 8B model on CPU is very slow on a 5-minute talk —
# and a 3B model does cleanup just as well. Falls back to the filler-stripping
# heuristic if this model isn't pulled (`ollama pull llama3.2:3b`).
IDEAL_REWRITE_MODEL = os.environ.get("IDEAL_REWRITE_MODEL", "llama3.2:3b")

# --- Ideal-delivery playback (ElevenLabs text-to-speech) -------------------
# OPTIONAL, opt-in cloud feature. The "hear how it could sound" card rewrites a
# user's transcript into a tighter script and reads it back in a clear voice.
# Unlike every other feature here it sends text to ElevenLabs' servers, so it is
# OFF by default: when ELEVENLABS_API_KEY is unset the card is simply hidden and
# the app stays fully local — the same graceful-degradation pattern as Ollama
# and MongoDB. Synthesis runs only on an explicit user click, never during a
# normal analysis.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_API_BASE = os.environ.get("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
# Voice to synthesize with. Leave UNSET to auto-pick a voice from your account
# (preferring your own cloned/generated voices). Important: the free ElevenLabs
# API tier cannot use the shared "library"/premade voices (Rachel, etc.) — it
# returns HTTP 402 — so you must use a voice you own. See _resolve_voice_id().
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
# Cap the text sent for a rewrite + synthesis — keeps latency and credit use sane.
IDEAL_DELIVERY_MAX_CHARS = int(os.environ.get("IDEAL_DELIVERY_MAX_CHARS", "5000"))

# --- User accounts, sessions & leaderboard (MongoDB) -----------------------
# Login sessions are signed with this key — set a real SECRET_KEY in production.
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
# Keep users logged in across browser restarts (sessions are marked permanent on
# login/register). Default 30 days; override with SESSION_DAYS.
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
app.permanent_session_lifetime = datetime.timedelta(days=SESSION_DAYS)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "presentation_helper")
DB_UNAVAILABLE_MSG = (
    "User accounts are unavailable — the app can't reach MongoDB. Make sure "
    "MongoDB is running and MONGO_URI is correct (default mongodb://localhost:27017), "
    "and that `pymongo` is installed. Core analysis still works without it."
)

ALLOWED_EXTENSIONS = {
    "wav", "mp3", "m4a", "mp4", "ogg", "flac", "webm", "aac", "opus",
}

# Whisper/WhisperX (and librosa for some formats) decode audio by invoking the
# `ffmpeg` binary as a subprocess. If it isn't on PATH the failure is an opaque
# FileNotFoundError ("[WinError 2] The system cannot find the file specified" on
# Windows), so we detect it up front and return this actionable message instead.
FFMPEG_MISSING_MSG = (
    "ffmpeg was not found on PATH. Whisper needs the ffmpeg binary to decode "
    "audio, so transcription can't start without it. Install ffmpeg "
    "(Windows: `winget install Gyan.FFmpeg`; macOS: `brew install ffmpeg`; "
    "Debian/Ubuntu: `sudo apt install ffmpeg`), then restart this server from a "
    "NEW terminal so it picks up the updated PATH."
)

# Minimum amount of audio we're willing to analyze. Anything shorter is rejected
# so users can't game the tool with a 2-second "Hi I'm X" clip. Override with
# MIN_RECORDING_SEC.
MIN_DURATION_SEC = float(os.environ.get("MIN_RECORDING_SEC", "15"))

# --- Speaking-rate reference (words per minute) ----------------------------
WPM_IDEAL_LOW = 120
WPM_IDEAL_HIGH = 150
WPM_TOO_SLOW = 110
WPM_TOO_FAST = 165
WINDOW_SECONDS = 15  # bucket size for timeline metrics

# Audio-feature analysis settings. A lower sample rate + larger hop dramatically
# speed up pitch/volume extraction (especially librosa.pyin) with no meaningful
# loss for speech coaching metrics.
ANALYSIS_SR = 16000
ANALYSIS_HOP = 1024

# --- Filler words -----------------------------------------------------------
# Multi-word fillers are checked first so "you know" isn't double counted.
FILLER_PHRASES = ["you know", "i mean", "sort of", "kind of"]

# "Hard" fillers are almost always disfluencies regardless of position, so they
# are counted wherever they appear.
FILLER_WORDS_HARD = {"um", "uh", "uhm", "er", "erm", "ah", "hmm", "mm", "mhm"}

# Discourse markers double as ordinary words ("the results are SO good", "tools
# LIKE this"), so counting every occurrence over-penalizes normal speech. They
# read as filler mainly in the sentence-/clause-initial position ("So, ...",
# "Well, ...", "Basically, ..."), so analyze_fillers only counts them there.
FILLER_DISCOURSE = {
    "like", "basically", "actually", "literally", "so",
    "right", "okay", "well", "yeah",
}

# --- Transition phrases (signal logical flow) -------------------------------
TRANSITION_PHRASES = [
    "first", "firstly", "second", "secondly", "third", "next", "then",
    "finally", "furthermore", "moreover", "in addition", "additionally",
    "however", "on the other hand", "therefore", "thus", "consequently",
    "as a result", "for example", "for instance", "in contrast",
    "meanwhile", "subsequently", "to summarize", "in summary",
    "in conclusion", "to conclude", "overall",
]

# --- Buzzwords + clearer suggested alternatives -----------------------------
# Deliberately limited to topic-independent filler — empty corporate jargon and
# vague praise that reads as fluff in any presentation. Context-dependent
# technical terms (leverage, robust, scalable, ecosystem, bandwidth, seamless,
# paradigm, actionable, mission-critical, best practice) are intentionally
# absent: they're precise, legitimate vocabulary in many fields, so flagging
# them produced false positives on technical talks. Buzzwords are advisory only
# and do NOT affect the language score (see analyze_buzzwords / compute_scores).
#
# The list lives in an external JSON file (BUZZWORDS_FILE, default
# backend/buzzwords.json) so it can be tuned without code changes — edit the
# file and restart. The dict below is the built-in fallback used when that file
# is missing or unreadable, keeping the feature working offline / out-of-the-box.
_DEFAULT_BUZZWORDS = {
    "synergy": "cooperation / working together",
    "disrupt": "change / improve",
    "disruptive": "game-changing — say what specifically",
    "holistic": "complete / whole",
    "low-hanging fruit": "easy wins",
    "circle back": "follow up",
    "move the needle": "make a measurable difference",
    "deep dive": "detailed look",
    "value-add": "benefit",
    "cutting-edge": "newest",
    "game-changer": "major improvement",
    "think outside the box": "be creative",
    "core competency": "main strength",
    "innovative": "new — describe how",
    "world-class": "excellent — give evidence",
}

BUZZWORDS_FILE = os.environ.get(
    "BUZZWORDS_FILE", os.path.join(os.path.dirname(__file__), "buzzwords.json")
)


def _load_buzzwords() -> dict:
    """Load buzzword -> alternative pairs from BUZZWORDS_FILE.

    Read once at startup. Falls back to the built-in defaults if the file is
    missing or malformed, so a bad edit can never take the feature down. Keys are
    lowercased to match the case-insensitive transcript scan in analyze_buzzwords.
    """
    try:
        with open(BUZZWORDS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return dict(_DEFAULT_BUZZWORDS)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[buzzwords] could not read {BUZZWORDS_FILE}: {exc}; using defaults")
        return dict(_DEFAULT_BUZZWORDS)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        print(f"[buzzwords] {BUZZWORDS_FILE} must be a JSON object of "
              "string->string; using defaults")
        return dict(_DEFAULT_BUZZWORDS)
    return {k.lower(): v for k, v in data.items()}


BUZZWORDS = _load_buzzwords()

# --- Stopwords for keyword/repetition analysis ------------------------------
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in",
    "on", "at", "for", "with", "as", "by", "from", "is", "are", "was",
    "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "its", "i", "you", "he", "she", "we", "they", "them", "our", "your",
    "my", "me", "us", "do", "does", "did", "have", "has", "had", "will",
    "would", "can", "could", "should", "about", "into", "over", "so",
    "than", "too", "very", "just", "also", "not", "no", "yes", "what",
    "which", "who", "when", "where", "how", "all", "any", "some", "more",
    "most", "going", "got", "get", "like", "really", "things", "thing",
}

# Weights for the overall composite score.
SCORE_WEIGHTS = {"delivery": 0.40, "language": 0.25, "content": 0.35}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round(value, ndigits: int = 2):
    """JSON-safe rounding that tolerates None / NaN."""
    try:
        if value is None:
            return None
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, ndigits)
    except (TypeError, ValueError):
        return None


def _tokenize(text: str):
    """Lowercase word tokens, apostrophes preserved (don't, it's)."""
    return re.findall(r"[a-z']+", text.lower())


# ---------------------------------------------------------------------------
# Module 1 — Speech to text (WhisperX, with openai-whisper fallback)
# ---------------------------------------------------------------------------

# Models are expensive to load (hundreds of MB) so they're loaded once and
# reused across requests. The lock prevents two concurrent requests on the
# threaded dev server from loading the same model twice.
_model_lock = threading.Lock()
_whisperx_model = None
_whisperx_align: dict = {}   # language_code -> (align_model, metadata)
_whisper_model = None


def transcribe(audio_path: str) -> dict:
    """Transcribe audio with word-level timestamps.

    Uses WhisperX by default — it runs Whisper for the transcript and then a
    forced-alignment pass for much more accurate word timestamps, which the
    pause/WPM/filler analyzers depend on. Falls back to openai-whisper if
    WhisperX is unavailable or TRANSCRIBE_BACKEND=whisper.

    Returns {text, language, words[], segments[], duration} where each word is
    {word, start, end}. Raises RuntimeError if no backend is available.
    """
    if TRANSCRIBE_BACKEND != "whisper":
        try:
            return _transcribe_whisperx(audio_path)
        except ImportError:
            # WhisperX not installed — fall back to plain whisper below.
            pass
    return _transcribe_whisper(audio_path)


def _resolve_device() -> str:
    if WHISPERX_DEVICE != "auto":
        return WHISPERX_DEVICE
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _get_whisperx_model():
    """Load (once) and cache the WhisperX transcription model."""
    global _whisperx_model
    if _whisperx_model is None:
        with _model_lock:
            if _whisperx_model is None:
                import whisperx  # ImportError -> caller falls back to whisper
                device = _resolve_device()
                compute_type = WHISPERX_COMPUTE_TYPE or (
                    "float16" if device == "cuda" else "int8"
                )
                _whisperx_model = whisperx.load_model(
                    WHISPER_MODEL, device, compute_type=compute_type,
                )
    return _whisperx_model


def _get_align_model(language: str, device: str):
    """Load (once per language) and cache the forced-alignment model."""
    if language not in _whisperx_align:
        with _model_lock:
            if language not in _whisperx_align:
                import whisperx
                _whisperx_align[language] = whisperx.load_align_model(
                    language_code=language, device=device,
                )
    return _whisperx_align[language]


def _transcribe_whisperx(audio_path: str) -> dict:
    import whisperx  # raises ImportError -> caller falls back to whisper

    device = _resolve_device()
    model = _get_whisperx_model()
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=WHISPERX_BATCH_SIZE)
    language = result.get("language")

    # Forced alignment for accurate word-level timestamps.
    segments = result.get("segments", [])
    try:
        align_model, metadata = _get_align_model(language, device)
        aligned = whisperx.align(
            segments, align_model, metadata, audio, device,
            return_char_alignments=False,
        )
        segments = aligned.get("segments", segments)
    except Exception:
        # Alignment model may be missing for some languages — keep raw segments.
        pass

    words, text_parts = [], []
    for seg in segments:
        text_parts.append((seg.get("text") or "").strip())
        seg_start = float(seg.get("start", 0.0) or 0.0)
        seg_end = float(seg.get("end", seg_start) or seg_start)
        for w in seg.get("words", []):
            token = (w.get("word") or "").strip()
            if not token:
                continue
            # Some words may be unaligned (no timing) — fall back to segment bounds
            # so word counts stay accurate for WPM.
            start = w.get("start")
            end = w.get("end")
            words.append({
                "word": token,
                "start": float(start) if start is not None else seg_start,
                "end": float(end) if end is not None else seg_end,
            })

    text = " ".join(p for p in text_parts if p).strip()
    duration = 0.0
    if words:
        duration = words[-1]["end"]
    elif segments:
        duration = float(segments[-1].get("end", 0.0) or 0.0)

    return {
        "text": text,
        "language": language,
        "words": words,
        "segments": segments,
        "duration": duration,
    }


def _get_whisper_model():
    """Load (once) and cache the openai-whisper fallback model."""
    global _whisper_model
    if _whisper_model is None:
        with _model_lock:
            if _whisper_model is None:
                import whisper  # openai-whisper
                _whisper_model = whisper.load_model(WHISPER_MODEL)
    return _whisper_model


def _transcribe_whisper(audio_path: str) -> dict:
    try:
        model = _get_whisper_model()
    except ImportError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "No transcription backend available. Install WhisperX "
            "(`pip install whisperx`) or openai-whisper (`pip install "
            "openai-whisper`)."
        ) from exc

    result = model.transcribe(audio_path, word_timestamps=True, fp16=False)

    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            token = (w.get("word") or "").strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": float(w.get("start", seg.get("start", 0.0))),
                "end": float(w.get("end", seg.get("end", 0.0))),
            })

    duration = 0.0
    if words:
        duration = words[-1]["end"]
    elif result.get("segments"):
        duration = float(result["segments"][-1].get("end", 0.0))

    return {
        "text": (result.get("text") or "").strip(),
        "language": result.get("language"),
        "words": words,
        "segments": result.get("segments", []),
        "duration": duration,
    }


# ---------------------------------------------------------------------------
# Audio loading (Librosa) — shared by the audio-feature analyzers
# ---------------------------------------------------------------------------

def load_audio(audio_path: str):
    """Load audio as mono waveform. Returns (y, sr, np) or (None, None, None).

    Resampled to ANALYSIS_SR (16 kHz). The feature analyzers (pitch up to 400 Hz,
    RMS volume, silence) don't need full fidelity, and a lower rate makes the
    expensive pitch tracking several times faster with no meaningful quality loss.
    """
    try:
        import numpy as np
        import librosa
    except ImportError:
        return None, None, None
    try:
        y, sr = librosa.load(audio_path, sr=ANALYSIS_SR, mono=True)
        return y, sr, np
    except Exception:
        return None, None, None


def probe_duration(audio_path: str):
    """Best-effort media duration in seconds, or None if it can't be determined.

    Used to reject too-short clips up front (before the expensive transcription).
    """
    try:
        import soundfile as sf
        return float(sf.info(audio_path).duration)
    except Exception:
        pass
    try:
        import librosa
        return float(librosa.get_duration(path=audio_path))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Module 2a — Speaking rate (WPM)
# ---------------------------------------------------------------------------

def analyze_speaking_rate(words: list, duration: float) -> dict:
    if not words or duration <= 0:
        return {"available": False, "reason": "No timed words available."}

    overall_wpm = len(words) / (duration / 60.0)

    # Windowed timeline of WPM.
    timeline = []
    n_windows = max(1, int(math.ceil(duration / WINDOW_SECONDS)))
    for i in range(n_windows):
        start = i * WINDOW_SECONDS
        end = start + WINDOW_SECONDS
        count = sum(1 for w in words if start <= w["start"] < end)
        span_sec = min(end, duration) - start
        span_min = span_sec / 60.0
        wpm = count / span_min if span_min > 0 else 0
        label = "ok"
        # Don't flag very short tail windows — a 2s remainder with a couple of
        # words divides by a tiny span and spuriously reads as "too fast".
        if span_sec >= 5:
            if wpm and wpm < WPM_TOO_SLOW:
                label = "too_slow"
            elif wpm > WPM_TOO_FAST:
                label = "too_fast"
        timeline.append({
            "t": _round(start, 1),
            "wpm": _round(wpm, 1),
            "label": label,
        })

    fast = [s for s in timeline if s["label"] == "too_fast"]
    slow = [s for s in timeline if s["label"] == "too_slow"]

    # Score: distance of overall WPM from the ideal band.
    if WPM_IDEAL_LOW <= overall_wpm <= WPM_IDEAL_HIGH:
        score = 100.0
    else:
        nearest = WPM_IDEAL_LOW if overall_wpm < WPM_IDEAL_LOW else WPM_IDEAL_HIGH
        score = _clamp(100 - abs(overall_wpm - nearest) * 1.2)

    return {
        "available": True,
        "wpm": _round(overall_wpm, 1),
        "ideal_range": [WPM_IDEAL_LOW, WPM_IDEAL_HIGH],
        "timeline": timeline,
        "too_fast_windows": len(fast),
        "too_slow_windows": len(slow),
        "score": _round(score, 1),
    }


# ---------------------------------------------------------------------------
# Module 2b — Pitch analysis (fundamental frequency via Librosa)
# ---------------------------------------------------------------------------

def analyze_pitch(y, sr, np) -> dict:
    if y is None:
        return {"available": False, "reason": "Librosa/numpy not available."}
    try:
        import librosa
        # Larger hop = far fewer frames for pyin's Viterbi decode (the slow part),
        # which is plenty of resolution for pitch-variation metrics.
        f0, voiced_flag, _ = librosa.pyin(
            y, fmin=70, fmax=400, sr=sr, frame_length=2048, hop_length=ANALYSIS_HOP,
        )
    except Exception as exc:
        return {"available": False, "reason": f"Pitch extraction failed: {exc}"}

    voiced = f0[~np.isnan(f0)]
    if voiced.size < 5:
        return {"available": False, "reason": "Not enough voiced audio."}

    mean = float(np.mean(voiced))
    std = float(np.std(voiced))
    semitone_std = float(np.std(12 * np.log2(voiced / mean))) if mean > 0 else 0.0

    # ~2-3 semitones of variation is expressive; <1 is monotone.
    variability_score = _clamp((semitone_std / 3.0) * 100)
    monotone = semitone_std < 1.0

    # Downsampled timeline for plotting.
    hop = max(1, f0.size // 200)
    times = librosa.times_like(f0, sr=sr, hop_length=ANALYSIS_HOP)
    timeline = []
    for i in range(0, f0.size, hop):
        val = f0[i]
        timeline.append({
            "t": _round(float(times[i]), 1),
            "hz": None if (val is None or np.isnan(val)) else _round(float(val), 1),
        })

    return {
        "available": True,
        "mean_hz": _round(mean, 1),
        "std_hz": _round(std, 1),
        "semitone_std": _round(semitone_std, 2),
        "variability_score": _round(variability_score, 1),
        "monotone": bool(monotone),
        "timeline": timeline,
        "score": _round(variability_score, 1),
    }


# ---------------------------------------------------------------------------
# Module 2c — Volume / loudness analysis (RMS energy via Librosa)
# ---------------------------------------------------------------------------

def analyze_volume(y, sr, np) -> dict:
    if y is None:
        return {"available": False, "reason": "Librosa/numpy not available."}
    try:
        import librosa
        frame, hop = 2048, ANALYSIS_HOP
        rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    except Exception as exc:
        return {"available": False, "reason": f"Volume analysis failed: {exc}"}

    # Positive dB scale referenced to a fixed quiet floor (~ -100 dBFS), so the
    # numbers read like a sound-level meter (e.g. 40-90) instead of the negative
    # "dB below loudest" scale. Clamp at 0 so it can never go negative.
    REF = 1e-5
    db = 20.0 * np.log10(np.maximum(rms, 1e-10) / REF)
    db = np.maximum(db, 0.0)

    # Consider only frames with actual speech energy (within 45 dB of the loudest).
    peak = float(np.max(db))
    speech = db[db > peak - 45]
    if speech.size < 5:
        return {"available": False, "reason": "Audio too quiet to analyze."}

    mean_db = float(np.mean(speech))
    std_db = float(np.std(speech))
    consistency = _clamp(100 - std_db * 4)  # lower spread => more consistent

    quiet_thresh = mean_db - 12
    loud_thresh = peak - 1.5
    quiet = int(np.sum(db < quiet_thresh) / len(db) * 100)
    loud = int(np.sum(db > loud_thresh) / len(db) * 100)

    hop_ds = max(1, len(rms) // 200)
    timeline = [
        {"t": _round(float(times[i]), 1), "db": _round(float(db[i]), 1)}
        for i in range(0, len(rms), hop_ds)
    ]

    return {
        "available": True,
        "mean_db": _round(mean_db, 1),
        "std_db": _round(std_db, 1),
        "consistency_score": _round(consistency, 1),
        "quiet_pct": quiet,
        "loud_pct": loud,
        "timeline": timeline,
        "score": _round(consistency, 1),
    }


# ---------------------------------------------------------------------------
# Module 2d — Pause analysis (timestamps + silence detection)
# ---------------------------------------------------------------------------

# Pronoun forms that are always capitalized mid-sentence, so a capitalized next
# word here is NOT a reliable "new sentence" signal.
_ALWAYS_CAP = {"i", "i'm", "i've", "i'll", "i'd"}


def _looks_like_sentence_break(prev_word: str, next_word: str) -> bool:
    """Heuristic: did a sentence/clause likely end between these two words?

    WhisperX word tokens frequently drop punctuation, so checking only for a
    trailing '.', '!' or '?' misses most real breaks (which is why "strategic"
    pauses almost never registered). As a fallback we use the fact that the model
    still capitalizes the first word of a new sentence: a capitalized next word
    (other than the pronoun "I") usually starts one.
    """
    if prev_word.rstrip().endswith((".", "!", "?")):
        return True
    nxt = next_word.strip()
    return bool(nxt[:1].isupper() and nxt.lower() not in _ALWAYS_CAP)


def analyze_pauses(words: list, duration: float, y, sr, np) -> dict:
    if not words:
        return {"available": False, "reason": "No timed words available."}

    pauses = []
    for prev, nxt in zip(words, words[1:]):
        gap = nxt["start"] - prev["end"]
        if gap < 0.25:  # ignore micro-gaps
            continue
        after_sentence = _looks_like_sentence_break(prev["word"], nxt["word"])
        if gap >= 2.5:
            kind = "long_awkward"
        elif after_sentence and 0.5 <= gap <= 1.8:
            kind = "strategic"
        elif 0.25 <= gap < 0.6:
            kind = "hesitation"
        else:
            kind = "normal"
        pauses.append({
            "t": _round(prev["end"], 2),
            "duration": _round(gap, 2),
            "type": kind,
        })

    counts = Counter(p["type"] for p in pauses)
    durations = [p["duration"] for p in pauses]
    avg = statistics.mean(durations) if durations else 0.0
    per_min = len(pauses) / (duration / 60.0) if duration > 0 else 0.0

    # Optional silence-ratio cross-check via librosa.
    silence_ratio = None
    if y is not None:
        try:
            import librosa
            intervals = librosa.effects.split(y, top_db=30)
            voiced = sum((b - a) for a, b in intervals) / sr
            total = len(y) / sr
            if total > 0:
                silence_ratio = _round(max(0.0, (total - voiced) / total), 3)
        except Exception:
            silence_ratio = None

    # Quality: strategic good, awkward/hesitation bad.
    score = 100.0
    score -= counts.get("long_awkward", 0) * 8
    score -= max(0, counts.get("hesitation", 0) - 2) * 3
    score += min(counts.get("strategic", 0) * 3, 12)
    score = _clamp(score)

    return {
        "available": True,
        "total_pauses": len(pauses),
        "pauses_per_minute": _round(per_min, 2),
        "avg_pause_sec": _round(avg, 2),
        "strategic": counts.get("strategic", 0),
        "long_awkward": counts.get("long_awkward", 0),
        "hesitation": counts.get("hesitation", 0),
        "silence_ratio": silence_ratio,
        "timeline": pauses,
        "score": _round(score, 1),
    }


# ---------------------------------------------------------------------------
# Module 2e — Filler word detection
# ---------------------------------------------------------------------------

def analyze_fillers(words: list, text: str, duration: float) -> dict:
    found = []  # {word, t}

    # Multi-word phrases from the joined text (timestamp = nearest word start).
    lowered = text.lower()
    for phrase in FILLER_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", lowered):
            t = _nearest_word_time(words, m.start(), text)
            found.append({"word": phrase, "t": t})

    # Single-token pass over the timestamped words:
    #  - Hard fillers (um, uh, …): always count (timestamps are exact here).
    #  - Discourse markers (so, well, yeah, …): count when REPEATED back-to-back
    #    ("yeah yeah yeah"), which is unambiguous filler/backchannel.
    prev_token = None
    for w in words:
        token = re.sub(r"[^a-z']", "", w["word"].lower())
        if token in FILLER_WORDS_HARD:
            found.append({"word": token, "t": _round(w["start"], 2)})
        elif token and token == prev_token and token in FILLER_DISCOURSE:
            found.append({"word": token, "t": _round(w["start"], 2)})
        prev_token = token

    # Discourse markers (so, well, like, …): also count when sentence-/clause-
    # initial — at the very start or right after sentence punctuation — so
    # ordinary mid-sentence usage isn't penalized but "So, ..." openers are.
    for marker in FILLER_DISCOURSE:
        pattern = r"(?:^|[.!?]['\"\)\]]?\s+)(" + re.escape(marker) + r")\b"
        for m in re.finditer(pattern, lowered):
            t = _nearest_word_time(words, m.start(1), text)
            found.append({"word": marker, "t": t})

    counts = Counter(f["word"] for f in found)
    per_min = len(found) / (duration / 60.0) if duration > 0 else 0.0

    # ~1/min is fine; >5/min is distracting.
    score = _clamp(100 - max(0.0, per_min - 1) * 12)

    return {
        "available": True,
        "total": len(found),
        "per_minute": _round(per_min, 2),
        "by_word": dict(counts.most_common()),
        "timestamps": sorted(found, key=lambda f: (f["t"] or 0)),
        "score": _round(score, 1),
    }


def _nearest_word_time(words: list, char_index: int, text: str):
    """Approximate a timestamp for a character offset in the full transcript."""
    if not words or not text:
        return None
    ratio = char_index / max(1, len(text))
    idx = min(len(words) - 1, int(ratio * len(words)))
    return _round(words[idx]["start"], 2)


# ---------------------------------------------------------------------------
# Module 3 — Language quality (transitions, buzzwords, repetition, keywords)
# ---------------------------------------------------------------------------

def analyze_transitions(text: str) -> dict:
    lowered = text.lower()
    found = Counter()
    for phrase in TRANSITION_PHRASES:
        n = len(re.findall(r"\b" + re.escape(phrase) + r"\b", lowered))
        if n:
            found[phrase] += n
    total = sum(found.values())
    word_count = max(1, len(_tokenize(text)))
    density = total / word_count * 100  # transitions per 100 words

    # ~1.5–4 transitions per 100 words reads as well-connected.
    if 1.0 <= density <= 4.0:
        score = 100.0
    elif density < 1.0:
        score = _clamp(40 + density * 60)
    else:
        score = _clamp(100 - (density - 4.0) * 10)

    return {
        "available": True,
        "total": total,
        "density_per_100w": _round(density, 2),
        "by_phrase": dict(found.most_common()),
        "score": _round(score, 1),
    }


def analyze_buzzwords(text: str) -> dict:
    lowered = text.lower()
    found = Counter()
    suggestions = {}
    for buzz, alt in BUZZWORDS.items():
        n = len(re.findall(r"\b" + re.escape(buzz) + r"\b", lowered))
        if n:
            found[buzz] += n
            suggestions[buzz] = alt
    total = sum(found.values())
    word_count = max(1, len(_tokenize(text)))
    density = total / word_count * 100
    overused = {b: c for b, c in found.items() if c >= 3}

    # Advisory only — no score. Buzzwords surface plainer-wording tips but do not
    # feed the language grade: even the curated filler above is occasionally used
    # deliberately, so penalizing it would punish fair usage. compute_scores
    # leaves this out of language_score on purpose.
    return {
        "available": True,
        "advisory": True,
        "total": total,
        "density_per_100w": _round(density, 2),
        "by_word": dict(found.most_common()),
        "overused": overused,
        "suggestions": suggestions,
    }


def _apply_buzzword_review(bz: dict, review: dict | None) -> dict:
    """Drop buzzwords the content LLM judged appropriate in context.

    `review` maps each flagged word -> True (used as precise, legitimate
    vocabulary here — suppress it) or False (genuine filler — keep flagging).
    Only the content call produces it; with no review (LLM unavailable) the
    deterministic flags are returned unchanged, so the feature stays offline-safe.
    """
    if not review:
        return bz
    by_word = dict(bz.get("by_word") or {})
    suggestions = dict(bz.get("suggestions") or {})
    overused = dict(bz.get("overused") or {})
    suppressed = {}
    for word in list(by_word):
        if review.get(word) is True:
            suppressed[word] = by_word.pop(word)
            suggestions.pop(word, None)
            overused.pop(word, None)
    if not suppressed:
        return {**bz, "reviewed": True}
    old_total = bz.get("total") or 0
    new_total = sum(by_word.values())
    old_density = bz.get("density_per_100w") or 0
    # density = total / word_count * 100, so scaling by the surviving-total ratio
    # gives the exact new density without re-tokenizing.
    new_density = _round(old_density * new_total / old_total, 2) if old_total else 0
    return {
        **bz,
        "by_word": by_word,
        "suggestions": suggestions,
        "overused": overused,
        "suppressed": suppressed,
        "total": new_total,
        "density_per_100w": new_density,
        "reviewed": True,
    }


def analyze_repetition(text: str, words: list) -> dict:
    tokens = [t for t in _tokenize(text) if t not in STOPWORDS and len(t) > 2]
    word_freq = Counter(tokens)
    repeated_words = {w: c for w, c in word_freq.most_common(10) if c >= 4}

    # Repeated sentence starters.
    sentences = re.split(r"[.!?]+", text)
    starters = Counter()
    for s in sentences:
        s = s.strip()
        if s:
            first = _tokenize(s)
            if first:
                starters[first[0]] += 1
    repeated_starters = {w: c for w, c in starters.most_common(5) if c >= 3}

    # Repeated bigrams (phrases).
    bigrams = Counter(zip(tokens, tokens[1:]))
    repeated_phrases = {
        f"{a} {b}": c for (a, b), c in bigrams.most_common(8) if c >= 3
    }

    # Vocabulary concentration: if one word dominates the talk (e.g. saying
    # "yeah" over and over), that's low-substance and should be punished hard.
    # Counting distinct repeated words barely moved the score before.
    total = len(tokens)
    top_share = (word_freq.most_common(1)[0][1] / total) if total else 0.0
    concentration_penalty = max(0.0, top_share - 0.15) * 200  # >15% share hurts

    penalty = len(repeated_words) * 4 + len(repeated_starters) * 5 + concentration_penalty
    score = _clamp(100 - penalty)

    return {
        "available": True,
        "repeated_words": repeated_words,
        "repeated_sentence_starters": repeated_starters,
        "repeated_phrases": repeated_phrases,
        "top_word_share": _round(top_share, 2),
        "score": _round(score, 1),
    }


def extract_keywords(text: str, top_n: int = 12) -> dict:
    tokens = [t for t in _tokenize(text) if t not in STOPWORDS and len(t) > 3]
    freq = Counter(tokens)
    keywords = [{"word": w, "count": c} for w, c in freq.most_common(top_n)]
    # Reinforcement = key concepts repeated a few times (not just once).
    reinforced = sum(1 for k in keywords if k["count"] >= 3)
    return {
        "available": True,
        "keywords": keywords,
        "reinforced_concepts": reinforced,
    }


def analyze_rhythm(words: list, text: str) -> dict:
    """Speaking-rhythm: variation in sentence length and pacing."""
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    lengths = [len(_tokenize(s)) for s in sentences if _tokenize(s)]
    if len(lengths) < 2:
        return {"available": False, "reason": "Not enough sentences."}
    mean_len = statistics.mean(lengths)
    stdev_len = statistics.pstdev(lengths)
    return {
        "available": True,
        "sentence_count": len(lengths),
        "avg_sentence_words": _round(mean_len, 1),
        "sentence_length_stdev": _round(stdev_len, 1),
        "varied": stdev_len > 3,  # varied sentence length keeps attention
    }


# ---------------------------------------------------------------------------
# Module 4 — Content & structure analysis (LLM with heuristic fallback)
# ---------------------------------------------------------------------------

CONTENT_CATEGORIES = ["introduction", "thesis", "evidence", "organization", "conclusion"]


def analyze_content(text: str, transitions: dict,
                    flagged_buzzwords: list | None = None) -> dict:
    """Evaluate intro, thesis, evidence, organization, conclusion.

    Uses the local LLM (Ollama/Llama 3.1) and falls back to a transparent
    heuristic if Ollama isn't reachable, so the app stays fully functional.
    `flagged_buzzwords` (if any) are vetted in the same call — see
    _content_via_llm — and the verdict is returned under "buzzword_review".
    """
    try:
        return _content_via_llm(text, flagged_buzzwords)
    except Exception as exc:  # fall back, but surface why
        result = _content_heuristic(text, transitions)
        result["llm_error"] = str(exc)
        result["method"] = "heuristic (LLM unavailable)"
        return result


def _content_via_llm(text: str, flagged_buzzwords: list | None = None) -> dict:
    import urllib.request

    schema = (
        "{\"categories\": {"
        "\"introduction\": {\"score\": 0-100, \"feedback\": str}, "
        "\"thesis\": {...}, \"evidence\": {...}, \"organization\": {...}, "
        "\"conclusion\": {...}}, \"summary\": str"
    )
    instructions = (
        "You are a STRICT, demanding presentation coach grading a transcript. "
        "Do NOT inflate scores — most attempts are mediocre. Use the FULL 0-100 "
        "range and be willing to give low scores. Grade each of five dimensions "
        "by how COMPLETE and developed it actually is: introduction (clear "
        "opening, context & purpose), thesis (central message/goal clearly "
        "stated), evidence (examples/data/explanations supporting claims), "
        "organization (logical structure, coherent connections), and conclusion "
        "(summarizes key points, reinforces message).\n\n"
        "Scoring rubric (apply it literally):\n"
        "- 0-15: the section is missing or essentially absent.\n"
        "- 16-40: barely present — a single sentence with no development. "
        "Example: an introduction that only says 'Hi, I'm Nicholas, I'm a coach' "
        "names the speaker but gives NO context, purpose, or hook, so it scores "
        "around 30 — never 80.\n"
        "- 41-60: present but thin, vague, or generic.\n"
        "- 61-80: clear and reasonably developed.\n"
        "- 81-100: thorough, specific, and compelling.\n\n"
        "Hard rules: a section that is only one short sentence CANNOT score "
        "above 40. Only award 80+ when the section is genuinely complete and "
        "well-developed. If a whole section (e.g. a conclusion) is absent, score "
        "it 0-15. Give concise, actionable feedback that names what is missing."
    )
    # Fold an optional buzzword context-check into the SAME call (no extra
    # latency): decide which flagged terms are precise, legitimate vocabulary in
    # THIS talk vs empty filler, so the deterministic matcher's false positives
    # (e.g. "robust" in a stats talk) can be suppressed downstream.
    words = sorted({w for w in (flagged_buzzwords or []) if w})
    if words:
        instructions += (
            "\n\nAlso review these flagged words: " + json.dumps(words) +
            ". For each, decide whether it is used as precise, legitimate "
            "vocabulary in this transcript (true) or as vague filler (false)."
        )
        schema += ", \"buzzword_review\": {\"<word>\": true|false}"
    schema += "}"
    prompt = (
        instructions + "\n\nReturn ONLY valid JSON with no markdown: " +
        schema + "\n\nTRANSCRIPT:\n" + text[:12000]
    )
    # Call the local Ollama server directly over its native HTTP API. format=json
    # makes Ollama return guaranteed-valid JSON, so no fence-stripping is needed.
    # temperature 0 + a fixed seed make the model decode greedily, so the same
    # transcript yields the same scores/feedback on repeated runs (as close to
    # deterministic as the local runtime allows).
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "top_p": 1, "seed": LLM_SEED},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = json.loads(body.get("message", {}).get("content", "") or "{}")
    cats = data.get("categories", {})
    scores = [cats.get(c, {}).get("score", 0) for c in CONTENT_CATEGORIES]
    overall = statistics.mean([s for s in scores if isinstance(s, (int, float))] or [0])
    result = {
        "available": True,
        "method": "llm",
        "model": LLM_MODEL,
        "categories": cats,
        "summary": data.get("summary", ""),
        "score": _round(overall, 1),
    }
    review = data.get("buzzword_review")
    if words and isinstance(review, dict):
        # Normalize to {lowercased word: bool} for _apply_buzzword_review.
        result["buzzword_review"] = {str(k).lower(): bool(v) for k, v in review.items()}
    return result


def _content_heuristic(text: str, transitions: dict) -> dict:
    """Rule-based structure analysis used when no LLM is configured."""
    lowered = text.lower()
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    word_count = len(_tokenize(text))
    opening = " ".join(sentences[:3]).lower()
    closing = " ".join(sentences[-3:]).lower()

    def has_any(haystack, needles):
        return any(n in haystack for n in needles)

    cats = {}

    # Introduction
    intro_cues = ["today", "i'm going to", "i will", "let me", "welcome",
                  "good morning", "good afternoon", "this presentation",
                  "i'd like to", "we're here", "my name"]
    intro_hit = has_any(opening, intro_cues)
    cats["introduction"] = {
        "score": 72 if intro_hit else 32,
        "feedback": ("Clear opening that sets up the talk."
                     if intro_hit else
                     "Opening is weak — greet the audience and state what the "
                     "talk is about up front."),
    }

    # Thesis / objective
    thesis_cues = ["the goal", "the purpose", "i argue", "main point",
                   "objective", "today i", "i want to show", "the key",
                   "this matters", "the problem"]
    thesis_hit = has_any(lowered[:1500], thesis_cues)
    cats["thesis"] = {
        "score": 70 if thesis_hit else 32,
        "feedback": ("A central message is stated early."
                     if thesis_hit else
                     "State your core message in one sentence near the start so "
                     "the audience knows the goal."),
    }

    # Evidence — numbers, data words, examples
    has_numbers = bool(re.search(r"\b\d+(\.\d+)?%?\b", text))
    evidence_cues = ["for example", "for instance", "data", "study", "research",
                     "percent", "according to", "case", "evidence", "results"]
    evidence_hits = sum(lowered.count(c) for c in evidence_cues)
    ev_score = _clamp(40 + (has_numbers * 20) + min(evidence_hits * 8, 40))
    cats["evidence"] = {
        "score": int(ev_score),
        "feedback": ("Claims are backed by examples or data."
                     if ev_score >= 65 else
                     "Add concrete examples, numbers, or sources to support your "
                     "claims."),
    }

    # Organization — transition density + length
    t_density = transitions.get("density_per_100w", 0) or 0
    org_score = _clamp(45 + min(t_density * 12, 45))
    cats["organization"] = {
        "score": int(org_score),
        "feedback": ("Ideas are connected with clear transitions."
                     if org_score >= 65 else
                     "Use transition phrases (first, next, however, therefore) to "
                     "guide the audience between ideas."),
    }

    # Conclusion
    concl_cues = ["in conclusion", "to conclude", "to summarize", "in summary",
                  "finally", "thank you", "key takeaway", "to wrap up", "overall"]
    concl_hit = has_any(closing, concl_cues)
    cats["conclusion"] = {
        "score": 74 if concl_hit else 28,
        "feedback": ("There is a clear closing that wraps things up."
                     if concl_hit else
                     "End with an explicit conclusion that summarizes key points "
                     "and restates your message."),
    }

    overall = statistics.mean(c["score"] for c in cats.values())
    return {
        "available": True,
        "method": "heuristic",
        "categories": cats,
        "summary": (f"Heuristic structural review of ~{word_count} words. "
                    "Start Ollama (`ollama serve`) for richer LLM-based feedback."),
        "score": _round(overall, 1),
    }


# ---------------------------------------------------------------------------
# Module 5 — Scoring & feedback synthesis
# ---------------------------------------------------------------------------

def _avg_available(scores):
    vals = [s for s in scores if isinstance(s, (int, float))]
    return statistics.mean(vals) if vals else None


def compute_scores(delivery, language, content) -> dict:
    delivery_score = _avg_available([
        delivery["rate"].get("score"),
        delivery["pitch"].get("score"),
        delivery["volume"].get("score"),
        delivery["pauses"].get("score"),
        delivery["fillers"].get("score"),
    ])
    # Buzzwords are intentionally excluded — they're advisory only (too
    # context-dependent to grade fairly). See analyze_buzzwords / BUZZWORDS.
    language_score = _avg_available([
        language["transitions"].get("score"),
        language["repetition"].get("score"),
    ])
    content_score = content.get("score")

    parts = {
        "delivery": delivery_score,
        "language": language_score,
        "content": content_score,
    }
    weighted, total_w = 0.0, 0.0
    for key, weight in SCORE_WEIGHTS.items():
        if isinstance(parts[key], (int, float)):
            weighted += parts[key] * weight
            total_w += weight
    overall = weighted / total_w if total_w else None

    return {
        "overall": _round(overall, 1),
        "delivery": _round(delivery_score, 1),
        "language": _round(language_score, 1),
        "content": _round(content_score, 1),
        "weights": SCORE_WEIGHTS,
    }


def build_feedback(scores, delivery, language, content) -> dict:
    strengths, improvements = [], []

    def note(cond_good, cond_bad, good_msg, bad_msg):
        if cond_good:
            strengths.append(good_msg)
        elif cond_bad:
            improvements.append((bad_msg, _impact_weight(bad_msg)))

    rate = delivery["rate"]
    if rate.get("available"):
        wpm = rate.get("wpm") or 0
        note(WPM_IDEAL_LOW <= wpm <= WPM_IDEAL_HIGH, True,
             f"Speaking pace is well-judged ({wpm} WPM).",
             (f"Pace is off ({wpm} WPM); aim for {WPM_IDEAL_LOW}-{WPM_IDEAL_HIGH} WPM."
              if not (WPM_IDEAL_LOW <= wpm <= WPM_IDEAL_HIGH) else ""))

    pitch = delivery["pitch"]
    if pitch.get("available"):
        note(not pitch.get("monotone"), pitch.get("monotone"),
             "Expressive pitch variation keeps the delivery engaging.",
             "Delivery sounds monotone — vary your pitch to emphasize key points.")

    vol = delivery["volume"]
    if vol.get("available"):
        note((vol.get("consistency_score") or 0) >= 70, (vol.get("consistency_score") or 0) < 70,
             "Volume is consistent and easy to follow.",
             "Volume is uneven — project steadily and avoid trailing off.")

    fil = delivery["fillers"]
    if fil.get("available"):
        pm = fil.get("per_minute") or 0
        note(pm <= 2, pm > 2,
             f"Very few filler words ({pm}/min).",
             f"Reduce filler words — {fil.get('total')} used ({pm}/min).")

    pau = delivery["pauses"]
    if pau.get("available"):
        note(pau.get("long_awkward", 0) <= 1, pau.get("long_awkward", 0) > 1,
             "Pauses are used effectively.",
             f"{pau.get('long_awkward')} long awkward pauses break the flow.")

    tr = language["transitions"]
    if tr.get("available"):
        note((tr.get("score") or 0) >= 70, (tr.get("score") or 0) < 60,
             "Good use of transitions to connect ideas.",
             "Add transition phrases (first, however, therefore) for smoother flow.")

    bz = language["buzzwords"]
    if bz.get("overused"):
        improvements.append((
            f"Overused buzzwords: {', '.join(bz['overused'])}. Use plainer language.",
            6))

    for cat, info in (content.get("categories") or {}).items():
        sc = info.get("score", 0)
        if sc >= 75:
            strengths.append(f"Strong {cat}: {info.get('feedback', '')}".strip())
        elif sc < 55:
            improvements.append((f"{cat.capitalize()}: {info.get('feedback', '')}".strip(),
                                 8 if cat in ("thesis", "conclusion") else 5))

    # Top-3 highest-impact recommendations.
    improvements.sort(key=lambda x: -x[1])
    top3 = [msg for msg, _ in improvements[:3]]

    return {
        "strengths": strengths or ["Solid, watchable delivery overall."],
        "improvements": [msg for msg, _ in improvements],
        "top_recommendations": top3 or ["Keep practicing — no major issues detected."],
    }


def _impact_weight(msg: str) -> int:
    msg = msg.lower()
    if "pace" in msg or "monotone" in msg:
        return 7
    if "filler" in msg:
        return 6
    return 4


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _too_short_msg(seconds: float) -> str:
    return (
        f"Recording is too short (~{seconds:.0f}s). Please record at least "
        f"{int(MIN_DURATION_SEC)} seconds of speech so there's enough content to "
        "analyze fairly."
    )


def run_analysis(audio_path: str) -> dict:
    warnings = []

    # Fail fast with a clear message if ffmpeg is missing, rather than letting
    # the subprocess raise an opaque FileNotFoundError mid-transcription.
    if shutil.which("ffmpeg") is None:
        return {"error": FFMPEG_MISSING_MSG}

    # Reject too-short clips up front (cheap probe, before transcription) so a
    # 2-second "Hi I'm X" can't be analyzed.
    probe = probe_duration(audio_path)
    if probe is not None and probe < MIN_DURATION_SEC:
        return {"error": _too_short_msg(probe)}

    tx = transcribe(audio_path)
    text, words, duration = tx["text"], tx["words"], tx["duration"]
    if not text:
        return {"error": "Transcription produced no text. Is there speech in the audio?"}

    # Fallback length check when the probe couldn't read the format: use the
    # transcript's own end time.
    if probe is None and duration and duration < MIN_DURATION_SEC:
        return {"error": _too_short_msg(duration)}

    y, sr, np = load_audio(audio_path)
    if y is None:
        warnings.append("Librosa/numpy unavailable — pitch/volume analysis skipped.")

    # Cheap text analyzers first (the LLM call below depends on them).
    transitions = analyze_transitions(text)
    # Buzzwords are flagged deterministically, then handed to the content LLM
    # call, which vets them for context; its verdict feeds back to suppress
    # false positives.
    buzzwords = analyze_buzzwords(text)
    flagged = list((buzzwords.get("by_word") or {}).keys())

    # Run the LLM content analysis (network/Ollama latency) concurrently with the
    # CPU-heavy audio feature extraction (pitch/volume). Both release the GIL
    # during their slow work, so this overlaps instead of adding up — typically
    # the single biggest speedup for total analysis time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        content_future = ex.submit(analyze_content, text, transitions, flagged)
        delivery = {
            "rate": analyze_speaking_rate(words, duration),
            "pitch": analyze_pitch(y, sr, np),
            "volume": analyze_volume(y, sr, np),
            "pauses": analyze_pauses(words, duration, y, sr, np),
            "fillers": analyze_fillers(words, text, duration),
        }
        content = content_future.result()

    if content.get("llm_error"):
        warnings.append(f"LLM content analysis failed: {content['llm_error']}")
    buzzwords = _apply_buzzword_review(buzzwords, content.pop("buzzword_review", None))

    language = {
        "transitions": transitions,
        "buzzwords": buzzwords,
        "repetition": analyze_repetition(text, words),
        "keywords": extract_keywords(text),
        "rhythm": analyze_rhythm(words, text),
    }

    scores = compute_scores(delivery, language, content)
    feedback = build_feedback(scores, delivery, language, content)

    return {
        "transcript": text,
        "language_detected": tx.get("language"),
        "duration_sec": _round(duration, 1),
        "word_count": len(words),
        "scores": scores,
        "delivery": delivery,
        "language": language,
        "content": content,
        "feedback": feedback,
        "warnings": warnings,
        # Whether the optional "hear how it could sound" card should be offered.
        "ideal_delivery_available": elevenlabs_available(),
    }


# ---------------------------------------------------------------------------
# Module 6 — User accounts, sessions & leaderboard (MongoDB)
# ---------------------------------------------------------------------------
#
# Optional, like the other heavy features. If pymongo isn't installed or the
# MongoDB server isn't reachable, the account/leaderboard endpoints return a
# clear "unavailable" message and the core analysis flow keeps working.

_mongo_lock = threading.Lock()
_mongo_client = None
_mongo_indexes_ready = False
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,30}$")


def get_db():
    """Return the MongoDB database handle, or None if unavailable."""
    global _mongo_client, _mongo_indexes_ready
    try:
        from pymongo import MongoClient, ASCENDING, DESCENDING
    except Exception:
        # pymongo not installed, or a broken install — treat DB as unavailable.
        return None
    if _mongo_client is None:
        with _mongo_lock:
            if _mongo_client is None:
                try:
                    _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500)
                except Exception:
                    # Bad/unresolvable URI (e.g. placeholder host) — not fatal.
                    return None
    db = _mongo_client[MONGO_DB_NAME]
    if not _mongo_indexes_ready:
        try:
            db.users.create_index([("username", ASCENDING)], unique=True)
            db.users.create_index([("email", ASCENDING)], unique=True, sparse=True)
            db.results.create_index([("user_id", ASCENDING)])
            db.results.create_index([("overall_score", DESCENDING)])
            _mongo_indexes_ready = True
        except Exception:
            pass  # server unreachable right now — retry on a later call
    return db


# Cache the ping result briefly so /health and /api/me (hit on every page load)
# don't pay a network round trip — or, when the DB is down, a multi-second
# timeout — on every single request.
_DB_STATUS_TTL = 5.0  # seconds
_db_status = {"ok": False, "checked_at": 0.0}


def db_available() -> bool:
    """True only if MongoDB actually responds to a ping (cached for a few seconds).

    Never raises — any failure (no pymongo, bad URI, server unreachable, auth
    error) is reported as simply "not available" so callers like /health stay
    200 instead of 500.
    """
    now = time.monotonic()
    if now - _db_status["checked_at"] < _DB_STATUS_TTL:
        return _db_status["ok"]
    ok = False
    try:
        db = get_db()
        if db is not None:
            db.client.admin.command("ping")
            ok = True
    except Exception:
        ok = False
    _db_status["ok"] = ok
    _db_status["checked_at"] = now
    return ok


def _user_by_id(uid):
    """Load a user document by id string, or None. Unlike current_user() this
    needs no request context, so background analysis jobs can use it to attribute
    a result to the user who submitted it (captured at submit time)."""
    if not uid:
        return None
    db = get_db()
    if db is None:
        return None
    try:
        from bson import ObjectId
        return db.users.find_one({"_id": ObjectId(uid)})
    except Exception:
        return None


def current_user():
    """Return the logged-in user document, or None."""
    return _user_by_id(session.get("user_id"))


def _public_user(user):
    if not user:
        return None
    return {
        "id": str(user["_id"]),
        "username": user.get("username"),
        "email": user.get("email"),
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "You must be logged in."}), 401
        return fn(*args, **kwargs)
    return wrapper


def _record_result(result: dict, user_id=None):
    """Persist a completed analysis to the leaderboard for the given user.

    `user_id` is captured at request time and passed in explicitly, because the
    analysis runs in a background thread that has no Flask session to read.

    Returns the saved overall score, or None if not saved (anonymous user,
    DB unavailable, or no score).
    """
    user = _user_by_id(user_id)
    if not user:
        return None
    db = get_db()
    if db is None:
        return None
    scores = result.get("scores") or {}
    overall = scores.get("overall")
    if overall is None:
        return None
    rate = (result.get("delivery") or {}).get("rate") or {}
    doc = {
        "user_id": str(user["_id"]),
        "username": user.get("username"),
        "overall_score": overall,
        "delivery_score": scores.get("delivery"),
        "language_score": scores.get("language"),
        "content_score": scores.get("content"),
        "wpm": rate.get("wpm"),
        "duration_sec": result.get("duration_sec"),
        "word_count": result.get("word_count"),
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    try:
        db.results.insert_one(doc)
        return overall
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Module 7 — Ideal-delivery playback (script rewrite + ElevenLabs TTS)
# ---------------------------------------------------------------------------
#
# "Hear how it could sound": rewrite the transcript into a tighter spoken script
# (local LLM, with a filler-stripping heuristic fallback), then synthesize it as
# audio with ElevenLabs. Optional and opt-in — gated on ELEVENLABS_API_KEY — and
# the only feature that sends text off the machine, so it runs solely when the
# user explicitly asks for it (see /api/ideal-delivery), never during /analyze.

# Vocalized pauses only — unambiguous disfluencies that are always safe to drop.
# The broader FILLER_WORDS list (so, like, well, right…) is deliberately NOT used
# here: those double as real words, so blind removal would mangle the sentence.
# The LLM rewrite handles them in context; this heuristic is just the offline net.
_HEURISTIC_FILLERS = {"um", "uh", "uhm", "er", "ah", "hmm", "mm", "mhm"}


_resolved_voice_id = None


def elevenlabs_available() -> bool:
    """True only if an ElevenLabs API key is configured."""
    return bool(ELEVENLABS_API_KEY)


def list_voices() -> list:
    """Return the voices available to the configured ElevenLabs account.

    Each entry is {voice_id, name, category}. `category` is "premade"/"famous"
    for the shared library voices (blocked on the free API tier) versus
    "cloned"/"generated"/"professional" for voices you own.
    """
    import urllib.request
    if not elevenlabs_available():
        return []
    url = ELEVENLABS_API_BASE.rstrip("/") + "/v1/voices"
    req = urllib.request.Request(url, headers={"xi-api-key": ELEVENLABS_API_KEY})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [
        {"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category")}
        for v in data.get("voices", []) if v.get("voice_id")
    ]


def _resolve_voice_id() -> str:
    """Pick the voice to synthesize with.

    An explicit ELEVENLABS_VOICE_ID always wins. Otherwise auto-pick from the
    account, preferring the user's OWN voices (cloned/generated/professional),
    because the free API tier rejects the shared library/premade voices (402).
    The choice is cached so we don't re-list voices on every request.
    """
    global _resolved_voice_id
    if ELEVENLABS_VOICE_ID:
        return ELEVENLABS_VOICE_ID
    if _resolved_voice_id:
        return _resolved_voice_id
    voices = list_voices()
    if not voices:
        raise RuntimeError(
            "No ElevenLabs voices found on this account. Create one at "
            "elevenlabs.io (VoiceLab → Instant Voice Clone is free) or set "
            "ELEVENLABS_VOICE_ID to a voice you own."
        )
    own = [v for v in voices if v.get("category") not in (None, "premade", "famous")]
    _resolved_voice_id = (own or voices)[0]["voice_id"]
    return _resolved_voice_id


def _strip_fillers_heuristic(text: str) -> str:
    """Offline fallback rewrite: drop vocalized fillers and tidy whitespace.

    Used when the LLM is unreachable so the feature still produces a cleaner
    script. Conservative on purpose — only removes clear disfluencies.
    """
    cleaned = text
    for phrase in FILLER_PHRASES:  # "you know", "i mean", "sort of", "kind of"
        cleaned = re.sub(r"\b" + re.escape(phrase) + r"\b", "", cleaned, flags=re.I)
    filler_re = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in sorted(_HEURISTIC_FILLERS)) + r")\b",
        re.I,
    )
    cleaned = filler_re.sub("", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)   # space before punctuation
    cleaned = re.sub(r"(,\s*){2,}", ", ", cleaned)        # collapse ", ," runs
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or text


def improve_script(text: str) -> dict:
    """Rewrite the transcript into a polished spoken script.

    Returns {script, method[, llm_error]}. Tries the local LLM (Ollama) for a
    real rewrite; if that's unavailable, falls back to stripping vocalized
    fillers so the feature still produces something usable offline.
    """
    text = (text or "").strip()
    if not text:
        return {"script": "", "method": "none"}
    try:
        return {"script": _improve_via_llm(text), "method": "llm", "model": IDEAL_REWRITE_MODEL}
    except Exception as exc:
        return {
            "script": _strip_fillers_heuristic(text),
            "method": "heuristic (LLM unavailable)",
            "llm_error": str(exc),
        }


def _improve_via_llm(text: str) -> str:
    """Ask the local Ollama model to rewrite the transcript. Raises on failure."""
    import urllib.request

    prompt = (
        "You are an expert speechwriter and presentation coach. Rewrite the "
        "following spoken-presentation transcript into a polished version of the "
        "SAME talk that the speaker could deliver aloud. Keep the speaker's "
        "meaning, intent, and first-person voice. Remove filler words and false "
        "starts, tighten wordy phrasing, fix grammar, and smooth the flow with "
        "natural transitions — but do NOT add new facts, claims, or extra length. "
        "Return ONLY valid JSON with no markdown: "
        "{\"script\": \"<the rewritten talk as plain text>\"}"
        "\n\nTRANSCRIPT:\n" + text[:12000]
    )
    payload = json.dumps({
        "model": IDEAL_REWRITE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.4},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = json.loads(body.get("message", {}).get("content", "") or "{}")
    script = (data.get("script") or "").strip()
    if not script:
        raise ValueError("LLM returned an empty script")
    return script


def synthesize_speech(text: str) -> bytes:
    """Synthesize `text` to MP3 bytes via the ElevenLabs API. Raises on failure."""
    import urllib.request
    import urllib.error

    if not elevenlabs_available():
        raise RuntimeError("ElevenLabs is not configured (set ELEVENLABS_API_KEY).")
    voice_id = _resolve_voice_id()
    url = (
        ELEVENLABS_API_BASE.rstrip("/")
        + "/v1/text-to-speech/" + voice_id
        + "?output_format=mp3_44100_128"
    )
    payload = json.dumps({
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        # Expressive but stable — models the varied, well-paced delivery the app
        # coaches users toward, without drifting off-script into a flat read.
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.75,
            "style": 0.3,
            "use_speaker_boost": True,
        },
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 402:
            raise RuntimeError(
                "ElevenLabs rejected this voice on your current plan (HTTP 402). "
                "The free API tier can't use the shared library/premade voices. "
                "Create your own voice at elevenlabs.io (VoiceLab → Instant Voice "
                "Clone is free), then set ELEVENLABS_VOICE_ID to it — or just leave "
                "it unset and the app auto-picks one of your own voices. See your "
                "available voices at /api/voices."
            ) from exc
        raise RuntimeError(f"ElevenLabs API error {exc.code}: {detail}") from exc


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "transcribe_backend": TRANSCRIBE_BACKEND,
        "whisper_model": WHISPER_MODEL,
        "llm": "ollama (local)",
        "llm_model": LLM_MODEL,
        "ollama_host": OLLAMA_HOST,
        # ffmpeg is required to decode audio; this tells you if the running
        # server process can actually find it on PATH.
        "ffmpeg_found": bool(shutil.which("ffmpeg")),
        "db_available": db_available(),
        "min_recording_sec": MIN_DURATION_SEC,
        # Optional "hear how it could sound" playback (ElevenLabs TTS).
        "elevenlabs_available": elevenlabs_available(),
        "elevenlabs_voice_id": (ELEVENLABS_VOICE_ID or "auto") if elevenlabs_available() else None,
    })


# --- Accounts & leaderboard -------------------------------------------------

@app.route("/api/register", methods=["POST"])
def api_register():
    db = get_db()
    if db is None:
        return jsonify({"error": DB_UNAVAILABLE_MSG}), 503
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not USERNAME_RE.match(username):
        return jsonify({"error": "Username must be 3-30 characters: letters, numbers, . _ -"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    from werkzeug.security import generate_password_hash
    from pymongo.errors import DuplicateKeyError
    doc = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    # Only store email when provided. Storing an explicit null would collide on
    # the unique (sparse) email index for every email-less account after the
    # first — a sparse index still indexes present-but-null fields. Omitting the
    # field keeps it out of the index, so unlimited accounts without an email
    # work while real emails stay unique.
    if email:
        doc["email"] = email
    try:
        res = db.users.insert_one(doc)
    except DuplicateKeyError:
        return jsonify({"error": "That username or email is already taken."}), 409
    except Exception as exc:
        return jsonify({"error": f"Database error: {exc}"}), 503
    session.permanent = True
    session["user_id"] = str(res.inserted_id)
    doc["_id"] = res.inserted_id
    return jsonify({"user": _public_user(doc)}), 201


@app.route("/api/login", methods=["POST"])
def api_login():
    db = get_db()
    if db is None:
        return jsonify({"error": DB_UNAVAILABLE_MSG}), 503
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    try:
        user = db.users.find_one({"username": username})
    except Exception as exc:
        return jsonify({"error": f"Database error: {exc}"}), 503

    from werkzeug.security import check_password_hash
    if not user or not check_password_hash(user.get("password_hash", ""), password):
        return jsonify({"error": "Invalid username or password."}), 401
    session.permanent = True
    session["user_id"] = str(user["_id"])
    return jsonify({"user": _public_user(user)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    return jsonify({
        "user": _public_user(current_user()),
        "db_available": db_available(),
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    """Global leaderboard: each user's best score, ranked high to low."""
    db = get_db()
    if db is None:
        return jsonify({"error": DB_UNAVAILABLE_MSG, "leaderboard": []}), 200
    try:
        pipeline = [
            {"$group": {
                "_id": "$user_id",
                "username": {"$first": "$username"},
                "best_score": {"$max": "$overall_score"},
                "attempts": {"$sum": 1},
                "last_at": {"$max": "$created_at"},
            }},
            {"$sort": {"best_score": -1, "last_at": 1}},
            {"$limit": 100},
        ]
        rows = list(db.results.aggregate(pipeline))
    except Exception as exc:
        return jsonify({"error": f"Database error: {exc}", "leaderboard": []}), 200

    me = session.get("user_id")
    leaderboard = [{
        "rank": i + 1,
        "username": r.get("username"),
        "best_score": _round(r.get("best_score"), 1),
        "attempts": r.get("attempts"),
        "is_me": r.get("_id") == me,
    } for i, r in enumerate(rows)]
    return jsonify({"leaderboard": leaderboard})


@app.route("/api/ideal-delivery", methods=["POST"])
def api_ideal_delivery():
    """Rewrite a transcript into a tighter script and, if ElevenLabs is
    configured, synthesize it to audio — the "hear how it could sound" feature.

    Generated on demand (not during /analyze): synthesis costs ElevenLabs credits
    and sends text to their API, so doing it only on an explicit click keeps the
    default analysis flow fully local.
    """
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "No transcript provided."}), 400

    improved = improve_script(transcript[:IDEAL_DELIVERY_MAX_CHARS])
    script = improved.get("script") or ""
    result = {
        "script": script,
        "method": improved.get("method"),
        "voice_id": None,
        "audio": None,
    }
    if not elevenlabs_available():
        # Defensive — the UI hides the card when unavailable, but if the endpoint
        # is hit directly, return the rewrite and say how to enable audio.
        result["note"] = "Set ELEVENLABS_API_KEY to also hear this script read aloud."
        return jsonify(result)
    try:
        import base64
        audio = synthesize_speech(script)
        result["audio"] = "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii")
        result["voice_id"] = _resolve_voice_id()  # cached — reports which voice was used
    except Exception as exc:
        # Keep the rewritten script even if synthesis fails (bad key, quota, etc.).
        result["audio_error"] = str(exc)
    return jsonify(result)


@app.route("/api/voices")
def api_voices():
    """List the ElevenLabs voices available to the configured account, so you can
    pick one for ELEVENLABS_VOICE_ID (free tier: use a voice you own)."""
    if not elevenlabs_available():
        return jsonify({
            "error": "ElevenLabs is not configured (set ELEVENLABS_API_KEY).",
            "voices": [],
        }), 200
    try:
        return jsonify({"voices": list_voices()})
    except Exception as exc:
        return jsonify({"error": f"Could not list voices: {exc}", "voices": []}), 200


# ---------------------------------------------------------------------------
# Background analysis jobs
# ---------------------------------------------------------------------------
#
# Analysis takes a long time (cold model load + transcription can run for
# minutes). Holding a single HTTP request open that whole time is unreliable:
# browsers, proxies, and OS network stacks drop stalled connections, which the
# frontend then reports as a "network error" even though the server finishes and
# logs a 200. So /analyze hands the work to a background thread and returns a job
# id immediately; the client polls /analyze/status/<id> with short requests that
# never stay open long enough to drop.

_analysis_jobs: dict = {}            # job_id -> {state, result?/error?, done_at}
_analysis_jobs_lock = threading.Lock()
_analysis_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("ANALYSIS_WORKERS", "2")),
    thread_name_prefix="analysis",
)
# Keep finished jobs around briefly so a dropped status poll can be retried.
JOB_RETENTION_SEC = int(os.environ.get("JOB_RETENTION_SEC", "900"))


def _prune_jobs():
    cutoff = time.time() - JOB_RETENTION_SEC
    with _analysis_jobs_lock:
        for jid in [j for j, v in _analysis_jobs.items()
                    if v.get("done_at") and v["done_at"] < cutoff]:
            _analysis_jobs.pop(jid, None)


def _run_analysis_job(job_id: str, audio_path: str, user_id):
    """Run the analysis off the request thread and stash the outcome by job id."""
    try:
        result = run_analysis(audio_path)
        if "error" not in result:
            # Logged-in users automatically get their score on the leaderboard.
            saved = _record_result(result, user_id=user_id)
            result["saved_to_leaderboard"] = saved is not None
        outcome = {"state": "done", "result": result}
    except RuntimeError as exc:  # missing Whisper, etc.
        outcome = {"state": "error", "error": str(exc)}
    except FileNotFoundError:  # ffmpeg (or another required binary) not on PATH
        outcome = {"state": "error", "error": FFMPEG_MISSING_MSG}
    except Exception as exc:  # pragma: no cover
        outcome = {"state": "error", "error": f"Analysis failed: {exc}"}
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass
    outcome["done_at"] = time.time()
    with _analysis_jobs_lock:
        _analysis_jobs[job_id] = outcome


@app.route("/analyze", methods=["POST"])
def analyze():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided (field name 'audio')."}), 400
    f = request.files["audio"]
    if not f.filename:
        return jsonify({"error": "Empty filename."}), 400
    if not _allowed(f.filename):
        return jsonify({
            "error": f"Unsupported file type. Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        }), 400

    suffix = "." + f.filename.rsplit(".", 1)[1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        f.save(tmp.name)
        tmp.close()
    except Exception as exc:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return jsonify({"error": f"Could not read upload: {exc}"}), 400

    # Capture the user now — the background thread has no request/session context.
    user_id = session.get("user_id")
    _prune_jobs()
    job_id = uuid.uuid4().hex
    with _analysis_jobs_lock:
        _analysis_jobs[job_id] = {"state": "processing", "done_at": None}
    # The background job owns the temp file from here and deletes it when done.
    _analysis_pool.submit(_run_analysis_job, job_id, tmp.name, user_id)
    return jsonify({"job_id": job_id}), 202


@app.route("/analyze/status/<job_id>")
def analyze_status(job_id):
    """Report on a background analysis job. The client polls this until it's
    'done' (full result attached) or 'error'."""
    with _analysis_jobs_lock:
        job = _analysis_jobs.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        return jsonify({
            "state": "unknown",
            "error": "Analysis job not found — it may have expired. Please try again.",
        }), 404
    state = snapshot.get("state")
    if state == "done":
        return jsonify({"state": "done", "result": snapshot.get("result")}), 200
    if state == "error":
        return jsonify({"state": "error", "error": snapshot.get("error")}), 200
    return jsonify({"state": "processing"}), 200


# Always return JSON (not Flask's HTML error pages) so the frontend's
# `await resp.json()` never chokes on an oversized upload or server error.
@app.errorhandler(413)
def _too_large(_e):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"File too large. Maximum upload size is {mb} MB."}), 413


@app.errorhandler(500)
def _server_error(_e):
    return jsonify({"error": "Internal server error."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
