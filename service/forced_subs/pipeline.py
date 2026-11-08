"""Forced-subtitle pipeline.

1. Pick the movie's main (English) audio track and decode it to 16 kHz mono.
2. Find every spoken line with Silero VAD (CPU, cheap).
3. Detect the language of the speech in ~12 s windows, batched on the GPU.
   Windows that are clearly English are skipped; the rest are checked line by line.
4. Translate only the foreign lines to English with Whisper (task="translate").
5. Write ``<movie>.en.forced.srt`` next to the movie so Jellyfin picks it up.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import cuda_setup  # noqa: F401  (must run before faster_whisper loads CUDA)
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.vad import VadOptions, get_speech_timestamps

log = logging.getLogger("forced_subs")

SAMPLE_RATE = 16000
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi"}
ENGLISH_TAGS = {"eng", "en", "english"}

# Phrases Whisper tends to invent from its training captions.
JUNK_PATTERNS = [
    r"thank(s| you) for watching",
    r"subtitles? (by|made by|created by)",
    r"subscribe",
    r"amara\.org",
    r"^\W*$",
]
JUNK_RE = re.compile("|".join(JUNK_PATTERNS), re.IGNORECASE)


@dataclass
class Settings:
    model: str = "large-v3"            # NOT large-v3-turbo: turbo can't translate well
    device: str = "cuda"
    compute_type: str = "int8_float16"  # ~3 GB VRAM for large-v3, fine on an 8 GB 4060
    target_language: str = "en"
    window_seconds: float = 12.0       # speech per language-detection window
    batch_size: int = 8                # windows encoded per GPU batch
    refine_below: float = 0.90         # window P(English) below this -> check each line
    foreign_min_prob: float = 0.60     # line is foreign if top language != en with p >= this
    short_line_seconds: float = 1.2    # lines shorter than this need more confidence
    short_line_min_prob: float = 0.85
    max_chunk_seconds: float = 28.0    # foreign lines are translated in chunks up to this long
    merge_gap_seconds: float = 1.5
    beam_size: int = 5


@dataclass
class Result:
    status: str                         # done | no_foreign_dialogue | skipped
    message: str
    output: Optional[str] = None
    foreign_lines: int = 0
    languages: dict = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


ProgressFn = Callable[[str, float], None]


def _noop(stage: str, fraction: float) -> None:
    pass


class SkipItem(Exception):
    """Raised when a file should not be processed (e.g. it already has forced subs)."""


# --------------------------------------------------------------------------- model


class ModelHolder:
    """Loads the Whisper model on demand and unloads it to free the GPU."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Optional[WhisperModel] = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self) -> WhisperModel:
        with self._lock:
            if self._model is None:
                s = self.settings
                log.info("Loading Whisper %s on %s (%s)", s.model, s.device, s.compute_type)
                self._model = WhisperModel(s.model, device=s.device, compute_type=s.compute_type)
            return self._model

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                log.info("Unloading Whisper model")
                self._model = None
                gc.collect()


# --------------------------------------------------------------------------- media


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(out.stdout)


def _lang(stream: dict) -> str:
    return (stream.get("tags", {}).get("language") or "").lower()


def pick_audio_stream(info: dict) -> dict:
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise SkipItem("File has no audio track")
    english = [s for s in audio if _lang(s) in ENGLISH_TAGS]
    default = [s for s in audio if s.get("disposition", {}).get("default")]
    for group in ([s for s in english if s in default], english, default, audio):
        if group:
            # Skip commentary tracks when there is a choice.
            normal = [s for s in group if "comment" not in (s.get("tags", {}).get("title") or "").lower()]
            return (normal or group)[0]
    return audio[0]


def forced_output_path(video: Path, target_language: str) -> Path:
    return video.with_name(f"{video.stem}.{target_language}.forced.srt")


def find_existing_forced(video: Path, info: dict) -> Optional[str]:
    for s in info.get("streams", []):
        if s.get("codec_type") == "subtitle" and s.get("disposition", {}).get("forced"):
            return f"embedded forced subtitle track #{s.get('index')}"
    stem = video.stem.lower()
    for f in video.parent.iterdir():
        name = f.name.lower()
        if f.suffix.lower() in SUBTITLE_EXTS and name.startswith(stem) and ".forced" in name:
            return f"subtitle file {f.name}"
    return None


def load_audio(video: Path, stream_index: int) -> np.ndarray:
    """Decode one audio stream to 16 kHz mono int16 (about 230 MB for a 2 h movie)."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
        "-map", f"0:{stream_index}", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(proc.stdout, dtype=np.int16)


def to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


# --------------------------------------------------------------------------- speech


@dataclass
class Line:
    start: int  # samples
    end: int
    lang: str = ""
    prob: float = 0.0

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / SAMPLE_RATE


def find_speech(pcm: np.ndarray, progress: ProgressFn) -> list[Line]:
    opts = VadOptions(
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=300,
        speech_pad_ms=150,
        max_speech_duration_s=15,
    )
    block = SAMPLE_RATE * 600  # 10-minute blocks keep memory low
    lines: list[Line] = []
    for offset in range(0, len(pcm), block):
        chunk = to_float(pcm[offset: offset + block])
        for ts in get_speech_timestamps(chunk, opts):
            lines.append(Line(offset + ts["start"], offset + ts["end"]))
        progress("Finding speech", min(1.0, (offset + block) / max(1, len(pcm))))
    # Join lines split at block boundaries.
    merged: list[Line] = []
    for ln in lines:
        if merged and ln.start - merged[-1].end < SAMPLE_RATE * 0.05:
            merged[-1].end = ln.end
        else:
            merged.append(ln)
    return merged


def _detect_batch(model: WhisperModel, clips: list[np.ndarray]) -> list[dict[str, float]]:
    """Return a {language: probability} dict per clip, encoding clips in one GPU batch."""
    feats = np.stack([pad_or_trim(model.feature_extractor(to_float(c))) for c in clips])
    enc = model.encode(feats)
    results = model.model.detect_language(enc)
    return [{tok[2:-2]: p for tok, p in r} for r in results]


def detect_languages(model: WhisperModel, pcm: np.ndarray, items: list[list[Line]],
                     batch_size: int, progress: ProgressFn, stage: str) -> list[dict[str, float]]:
    """Detect the language of each item; an item is a group of lines whose speech is joined."""
    out: list[dict[str, float]] = []
    for i in range(0, len(items), batch_size):
        batch = items[i: i + batch_size]
        clips = [np.concatenate([pcm[ln.start: ln.end] for ln in group]) for group in batch]
        out.extend(_detect_batch(model, clips))
        progress(stage, min(1.0, (i + batch_size) / max(1, len(items))))
    return out


def group_windows(lines: list[Line], window_seconds: float) -> list[list[Line]]:
    windows: list[list[Line]] = []
    current: list[Line] = []
    speech = 0.0
    for ln in lines:
        if current and speech + ln.seconds > window_seconds:
            windows.append(current)
            current, speech = [], 0.0
        current.append(ln)
        speech += ln.seconds
    if current:
        windows.append(current)
    return windows


def is_foreign(probs: dict[str, float], seconds: float, s: Settings) -> tuple[bool, str, float]:
    lang, p = max(probs.items(), key=lambda kv: kv[1])
    if lang == s.target_language:
        return False, lang, p
    needed = s.short_line_min_prob if seconds < s.short_line_seconds else s.foreign_min_prob
    return p >= needed, lang, p


# --------------------------------------------------------------------------- translate


@dataclass
class Cue:
    start: float
    end: float
    text: str


def chunk_foreign(lines: list[Line], s: Settings) -> list[list[Line]]:
    chunks: list[list[Line]] = []
    for ln in lines:
        if chunks:
            last = chunks[-1]
            gap = (ln.start - last[-1].end) / SAMPLE_RATE
            span = (ln.end - last[0].start) / SAMPLE_RATE
            if ln.lang == last[0].lang and gap <= s.merge_gap_seconds and span <= s.max_chunk_seconds:
                last.append(ln)
                continue
        chunks.append([ln])
    return chunks


def translate_chunk(model: WhisperModel, pcm: np.ndarray, chunk: list[Line], s: Settings) -> list[Cue]:
    start, end = chunk[0].start, chunk[-1].end
    audio = to_float(pcm[start:end])
    segments, _ = model.transcribe(
        audio,
        language=chunk[0].lang,
        task="translate",
        beam_size=s.beam_size,
        condition_on_previous_text=False,
        vad_filter=False,
        temperature=[0.0, 0.2, 0.4],
    )
    offset = start / SAMPLE_RATE
    chunk_len = (end - start) / SAMPLE_RATE
    cues: list[Cue] = []
    for seg in segments:
        text = seg.text.strip()
        if not text or JUNK_RE.search(text):
            continue
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
            continue
        cues.append(Cue(offset + max(0.0, seg.start), offset + min(chunk_len, seg.end), text))
    return cues


# --------------------------------------------------------------------------- srt


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def _wrap(text: str, width: int = 42) -> str:
    if len(text) <= width:
        return text
    words = text.split()
    best = text
    best_diff = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(a) - len(b))
        if best_diff is None or diff < best_diff:
            best, best_diff = f"{a}\n{b}", diff
    return best


def write_srt(cues: list[Cue], path: Path) -> None:
    cues = sorted(cues, key=lambda c: c.start)
    for i, c in enumerate(cues):
        c.end = max(c.end, c.start + 1.0)  # readable minimum
        if i + 1 < len(cues):
            c.end = min(c.end, cues[i + 1].start - 0.05)
    blocks = [f"{i}\n{_ts(c.start)} --> {_ts(c.end)}\n{_wrap(c.text)}\n" for i, c in enumerate(cues, 1)]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- main


def generate_forced_subtitles(video: Path, models: ModelHolder, settings: Settings,
                              overwrite: bool = False, progress: ProgressFn = _noop) -> Result:
    t0 = time.monotonic()
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"Not found: {video}")
    out_path = forced_output_path(video, settings.target_language)

    progress("Checking file", 0.0)
    info = probe(video)
    if not overwrite:
        existing = find_existing_forced(video, info)
        if existing:
            return Result("skipped", f"Already has forced subtitles ({existing})")
    elif out_path.exists():
        out_path.unlink()

    stream = pick_audio_stream(info)
    log.info("%s: using audio stream #%s (%s)", video.name, stream["index"], _lang(stream) or "untagged")

    progress("Decoding audio", 0.0)
    pcm = load_audio(video, stream["index"])

    lines = find_speech(pcm, progress)
    log.info("%s: %d speech segments", video.name, len(lines))
    if not lines:
        return Result("no_foreign_dialogue", "No speech found", seconds=time.monotonic() - t0)

    model = models.get()

    # Pass 1: coarse windows. Most of an English movie is ruled out here.
    windows = group_windows(lines, settings.window_seconds)
    window_probs = detect_languages(model, pcm, windows, settings.batch_size, progress, "Detecting languages")
    suspect = [ln for w, p in zip(windows, window_probs)
               if p.get(settings.target_language, 0.0) < settings.refine_below for ln in w]
    log.info("%s: %d/%d windows need a closer look (%d lines)", video.name,
             sum(p.get(settings.target_language, 0.0) < settings.refine_below for p in window_probs),
             len(windows), len(suspect))

    # Pass 2: line by line inside suspect windows.
    foreign: list[Line] = []
    if suspect:
        line_probs = detect_languages(model, pcm, [[ln] for ln in suspect], settings.batch_size,
                                      progress, "Checking lines")
        for ln, probs in zip(suspect, line_probs):
            yes, lang, p = is_foreign(probs, ln.seconds, settings)
            if yes:
                ln.lang, ln.prob = lang, p
                foreign.append(ln)

    if not foreign:
        return Result("no_foreign_dialogue", "No foreign-language dialogue found",
                      seconds=time.monotonic() - t0)

    # Pass 3: translate.
    chunks = chunk_foreign(foreign, settings)
    cues: list[Cue] = []
    for i, chunk in enumerate(chunks):
        cues.extend(translate_chunk(model, pcm, chunk, settings))
        progress("Translating", (i + 1) / len(chunks))

    if not cues:
        return Result("no_foreign_dialogue", "Foreign speech found but nothing translatable",
                      seconds=time.monotonic() - t0)

    write_srt(cues, out_path)
    langs: dict[str, int] = {}
    for ln in foreign:
        langs[ln.lang] = langs.get(ln.lang, 0) + 1
    summary = ", ".join(f"{k} ({v})" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))
    return Result(
        "done",
        f"Wrote {len(cues)} subtitles from {len(foreign)} foreign lines: {summary}",
        output=str(out_path),
        foreign_lines=len(foreign),
        languages=langs,
        seconds=time.monotonic() - t0,
    )
