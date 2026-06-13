"""Presentation Helper — single-file Flask backend.

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
import shutil
import datetime
import threading
import tempfile
import statistics
from functools import wraps
from collections import Counter

from flask import Flask, request, jsonify, render_template, session

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load environment variables from a local .env file (e.g. MONGO_URI, SECRET_KEY)
# if one exists next to this file. This makes config work the same whether the
# app is launched from a shell `export`, an IDE "Run" button, or a double-click —
# all of which otherwise see different environments. Optional: if python-dotenv
# isn't installed, real environment variables are still used.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

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
# Fixed seed + temperature 0 (see analyze_content) make the LLM's content
# scores reproducible for the same transcript. Override LLM_SEED if desired.
LLM_SEED = int(os.environ.get("LLM_SEED", "42"))

# --- User accounts, sessions & leaderboard (MongoDB) -----------------------
# Login sessions are signed with this key — set a real SECRET_KEY in production.
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
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

# --- Speaking-rate reference (words per minute) ----------------------------
WPM_IDEAL_LOW = 120
WPM_IDEAL_HIGH = 150
WPM_TOO_SLOW = 110
WPM_TOO_FAST = 165
WINDOW_SECONDS = 15  # bucket size for timeline metrics

# --- Filler words -----------------------------------------------------------
# Multi-word fillers are checked first so "you know" isn't double counted.
FILLER_PHRASES = ["you know", "i mean", "sort of", "kind of"]
FILLER_WORDS = {
    "um", "uh", "uhm", "er", "ah", "hmm", "like", "basically",
    "actually", "literally", "so", "right", "okay", "well", "yeah",
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
    """Load audio as mono waveform. Returns (y, sr, np) or (None, None, None)."""
    try:
        import numpy as np
        import librosa
    except ImportError:
        return None, None, None
    try:
        y, sr = librosa.load(audio_path, sr=None, mono=True)
        return y, sr, np
    except Exception:
        return None, None, None


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
        f0, voiced_flag, _ = librosa.pyin(
            y, fmin=70, fmax=400, sr=sr, frame_length=2048,
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
    times = librosa.times_like(f0, sr=sr)
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
        frame, hop = 2048, 512
        rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    except Exception as exc:
        return {"available": False, "reason": f"Volume analysis failed: {exc}"}

    db = librosa.amplitude_to_db(rms, ref=np.max)  # 0 dB = loudest frame
    # Consider only frames with actual speech energy.
    speech = db[db > -45]
    if speech.size < 5:
        return {"available": False, "reason": "Audio too quiet to analyze."}

    mean_db = float(np.mean(speech))
    std_db = float(np.std(speech))
    consistency = _clamp(100 - std_db * 4)  # lower spread => more consistent

    quiet_thresh = mean_db - 12
    loud_thresh = -1.5
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

def analyze_pauses(words: list, duration: float, y, sr, np) -> dict:
    if not words:
        return {"available": False, "reason": "No timed words available."}

    SENTENCE_END = (".", "!", "?")
    pauses = []
    for prev, nxt in zip(words, words[1:]):
        gap = nxt["start"] - prev["end"]
        if gap < 0.25:  # ignore micro-gaps
            continue
        after_sentence = prev["word"].rstrip().endswith(SENTENCE_END)
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

    # Single-word fillers from timestamped tokens.
    for w in words:
        token = re.sub(r"[^a-z']", "", w["word"].lower())
        if token in FILLER_WORDS:
            found.append({"word": token, "t": _round(w["start"], 2)})

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

    penalty = len(repeated_words) * 4 + len(repeated_starters) * 5
    score = _clamp(100 - penalty)

    return {
        "available": True,
        "repeated_words": repeated_words,
        "repeated_sentence_starters": repeated_starters,
        "repeated_phrases": repeated_phrases,
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
        "You are an expert presentation coach. Evaluate this presentation "
        "transcript on five dimensions: introduction (clear opening, context "
        "& purpose), thesis (central message/goal clearly stated), evidence "
        "(examples/data/explanations supporting claims), organization "
        "(logical structure, coherent connections), and conclusion (summarizes "
        "key points, reinforces message). Give each a 0-100 score and concise, "
        "actionable feedback."
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
        "score": 80 if intro_hit else 45,
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
        "score": 78 if thesis_hit else 48,
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
        "score": 80 if concl_hit else 42,
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

def run_analysis(audio_path: str) -> dict:
    warnings = []

    # Fail fast with a clear message if ffmpeg is missing, rather than letting
    # the subprocess raise an opaque FileNotFoundError mid-transcription.
    if shutil.which("ffmpeg") is None:
        return {"error": FFMPEG_MISSING_MSG}

    tx = transcribe(audio_path)
    text, words, duration = tx["text"], tx["words"], tx["duration"]
    if not text:
        return {"error": "Transcription produced no text. Is there speech in the audio?"}

    y, sr, np = load_audio(audio_path)
    if y is None:
        warnings.append("Librosa/numpy unavailable — pitch/volume analysis skipped.")

    delivery = {
        "rate": analyze_speaking_rate(words, duration),
        "pitch": analyze_pitch(y, sr, np),
        "volume": analyze_volume(y, sr, np),
        "pauses": analyze_pauses(words, duration, y, sr, np),
        "fillers": analyze_fillers(words, text, duration),
    }

    transitions = analyze_transitions(text)

    # Buzzwords are flagged deterministically, then handed to the content LLM
    # call, which vets them for context; its verdict feeds back to suppress
    # false positives. One round trip, no extra latency.
    buzzwords = analyze_buzzwords(text)
    content = analyze_content(text, transitions, list((buzzwords.get("by_word") or {}).keys()))
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


def db_available() -> bool:
    """True only if MongoDB actually responds to a ping.

    Never raises — any failure (no pymongo, bad URI, server unreachable, auth
    error) is reported as simply "not available" so callers like /health stay
    200 instead of 500.
    """
    try:
        db = get_db()
        if db is None:
            return False
        db.client.admin.command("ping")
        return True
    except Exception:
        return False


def current_user():
    """Return the logged-in user document, or None."""
    uid = session.get("user_id")
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


def _record_result(result: dict):
    """Persist a completed analysis to the leaderboard for the logged-in user.

    Returns the saved overall score, or None if not saved (anonymous user,
    DB unavailable, or no score).
    """
    user = current_user()
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
        result = run_analysis(tmp.name)
        if "error" not in result:
            # Logged-in users automatically get their score on the leaderboard.
            saved = _record_result(result)
            result["saved_to_leaderboard"] = saved is not None
        status = 200 if "error" not in result else 422
        return jsonify(result), status
    except RuntimeError as exc:  # missing Whisper, etc.
        return jsonify({"error": str(exc)}), 503
    except FileNotFoundError:  # ffmpeg (or another required binary) not on PATH
        return jsonify({"error": FFMPEG_MISSING_MSG}), 503
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Analysis failed: {exc}"}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


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
