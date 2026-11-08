"""Forced-subtitle pipeline.

1. Pick the movie's main audio track and decode it to 16 kHz mono.
2. Find every spoken line with Silero VAD (CPU, cheap).
3. Detect the language of the speech in ~12 s windows, batched on the GPU.
   Windows that are clearly the main language are skipped; the rest are checked line by line.
4. Translate only the foreign lines to English with Whisper (task="translate").
5. Write ``<movie>.en.forced.srt`` next to the movie so Jellyfin picks it up.

Every tunable lives in :class:`Settings`; the service builds one per job from the
plugin's options (see :meth:`Settings.with_options`).
"""

from __future__ import annotations

import dataclasses
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

# ISO 639-2 (what media files use) -> Whisper's ISO 639-1 codes, for the common cases.
ISO2_TO_1 = {
    "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "ita": "it",
    "por": "pt", "rus": "ru", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "ara": "ar",
    "hin": "hi", "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi",
    "pol": "pl", "tur": "tr", "gre": "el", "ell": "el", "heb": "he", "cze": "cs", "ces": "cs",
    "hun": "hu", "rum": "ro", "ron": "ro", "ukr": "uk", "tha": "th", "vie": "vi", "ind": "id",
    "per": "fa", "fas": "fa", "srp": "sr", "hrv": "hr", "bul": "bg", "slo": "sk", "slk": "sk",
}

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ar": "Arabic", "hi": "Hindi", "nl": "Dutch", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "tr": "Turkish", "el": "Greek",
    "he": "Hebrew", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "uk": "Ukrainian",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "fa": "Persian", "sr": "Serbian",
    "hr": "Croatian", "bg": "Bulgarian", "sk": "Slovak", "la": "Latin", "yi": "Yiddish",
    "ga": "Irish", "cy": "Welsh", "ur": "Urdu", "bn": "Bengali", "ta": "Tamil", "tl": "Tagalog",
    "ms": "Malay", "sw": "Swahili", "af": "Afrikaans", "is": "Icelandic", "lt": "Lithuanian",
    "lv": "Latvian", "et": "Estonian", "sl": "Slovenian", "mk": "Macedonian", "ka": "Georgian",
    "hy": "Armenian", "az": "Azerbaijani", "kk": "Kazakh", "mn": "Mongolian", "ne": "Nepali",
    "pa": "Punjabi", "km": "Khmer", "lo": "Lao", "my": "Burmese", "am": "Amharic", "so": "Somali",
}

# Phrases Whisper tends to invent from its training captions.
BUILTIN_JUNK = [
    r"thank(s| you) (so much )?for watching",
    r"subtitles? (by|made by|created by)",
    r"please subscribe",
    r"like and subscribe",
    r"amara\.org",
    r"^\W*$",
]


def lang1(code: str) -> str:
    code = (code or "").strip().lower()
    return ISO2_TO_1.get(code, code)


@dataclass
class Settings:
    # Model
    model: str = "large-v3"             # NOT large-v3-turbo: turbo can't translate well
    device: str = "cuda"                # cuda | cpu
    compute_type: str = "int8_float16"  # ~3 GB VRAM for large-v3, fine on an 8 GB 4060
    beam_size: int = 5

    # Audio track
    audio_languages: list = field(default_factory=lambda: ["en"])  # preferred main-audio languages
    require_audio_language: bool = False  # skip files with no track in audio_languages
    skip_commentary: bool = True

    # Speech detection
    vad_threshold: float = 0.5
    min_line_seconds: float = 0.25

    # Language detection
    main_language: str = "en"           # speech in this language is never subtitled
    window_seconds: float = 12.0
    batch_size: int = 8
    refine_below: float = 0.90
    foreign_min_prob: float = 0.60
    short_line_seconds: float = 1.2
    short_line_min_prob: float = 0.85
    skip_languages: list = field(default_factory=list)   # never subtitle these
    only_languages: list = field(default_factory=list)   # if set, only subtitle these

    # Translation
    max_chunk_seconds: float = 28.0
    merge_gap_seconds: float = 1.5
    drop_phrases: list = field(default_factory=list)      # extra phrases to drop (case-insensitive)

    # Existing subtitles
    skip_if_embedded_forced: bool = True
    skip_if_external_forced: bool = True

    # Output
    output_language_tag: str = "en"     # en | eng  -> Movie.en.forced.srt
    mark_default: bool = False          # adds .default so players select it automatically
    language_prefix: bool = False       # "[Russian] Good evening."
    max_line_chars: int = 42
    max_lines: int = 2
    min_cue_seconds: float = 1.0
    max_cue_seconds: float = 7.0

    def with_options(self, options: Optional[dict]) -> "Settings":
        """Return a copy with known keys from ``options`` applied (unknown keys are ignored)."""
        if not options:
            return dataclasses.replace(self)
        known = {f.name: f for f in dataclasses.fields(self)}
        changes = {}
        for key, value in options.items():
            if key not in known or value is None:
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, list):
                    if isinstance(value, str):
                        value = [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]
                    value = list(value)
                else:
                    value = str(value).strip()
            except (TypeError, ValueError):
                log.warning("Ignoring bad value for %s: %r", key, value)
                continue
            changes[key] = value
        s = dataclasses.replace(self, **changes)
        s.main_language = lang1(s.main_language) or "en"
        s.audio_languages = [lang1(x) for x in s.audio_languages]
        s.skip_languages = [lang1(x) for x in s.skip_languages]
        s.only_languages = [lang1(x) for x in s.only_languages]
        s.max_lines = max(1, s.max_lines)
        s.max_line_chars = max(10, s.max_line_chars)
        s.batch_size = max(1, s.batch_size)
        s.beam_size = max(1, s.beam_size)
        return s

    @property
    def model_key(self) -> tuple:
        return (self.model, self.device, self.compute_type)


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


class Cancelled(Exception):
    """Raised from a progress callback to stop a job."""


# --------------------------------------------------------------------------- model


class ModelHolder:
    """Loads the Whisper model on demand, reloads it if settings change, unloads to free the GPU."""

    def __init__(self) -> None:
        self._model: Optional[WhisperModel] = None
        self._key: Optional[tuple] = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def loaded_key(self) -> Optional[tuple]:
        return self._key

    def get(self, s: Settings) -> WhisperModel:
        with self._lock:
            if self._model is not None and self._key != s.model_key:
                log.info("Model settings changed; reloading")
                self._model = None
                gc.collect()
            if self._model is None:
                log.info("Loading Whisper %s on %s (%s)", s.model, s.device, s.compute_type)
                self._model = WhisperModel(s.model, device=s.device, compute_type=s.compute_type)
                self._key = s.model_key
            return self._model

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                log.info("Unloading Whisper model")
                self._model = None
                self._key = None
                gc.collect()


# --------------------------------------------------------------------------- media


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(out.stdout)


def _stream_lang(stream: dict) -> str:
    return lang1(stream.get("tags", {}).get("language") or "")


def _is_commentary(stream: dict) -> bool:
    if stream.get("disposition", {}).get("comment"):
        return True
    return "comment" in (stream.get("tags", {}).get("title") or "").lower()


def pick_audio_stream(info: dict, s: Settings) -> dict:
    audio = [st for st in info.get("streams", []) if st.get("codec_type") == "audio"]
    if s.skip_commentary:
        audio = [st for st in audio if not _is_commentary(st)] or audio
    if not audio:
        raise SkipItem("File has no audio track")
    preferred = [st for st in audio if _stream_lang(st) in s.audio_languages]
    if s.require_audio_language and not preferred:
        tags = ", ".join(sorted({_stream_lang(st) or "untagged" for st in audio}))
        raise SkipItem(f"No audio track in {', '.join(s.audio_languages)} (found: {tags})")
    default = [st for st in audio if st.get("disposition", {}).get("default")]
    for group in ([st for st in preferred if st in default], preferred, default, audio):
        if group:
            return group[0]
    return audio[0]


def forced_output_path(video: Path, s: Settings) -> Path:
    flags = ".default.forced" if s.mark_default else ".forced"
    return video.with_name(f"{video.stem}.{s.output_language_tag}{flags}.srt")


def find_existing_forced(video: Path, info: dict, s: Settings) -> Optional[str]:
    if s.skip_if_embedded_forced:
        for st in info.get("streams", []):
            if st.get("codec_type") == "subtitle" and st.get("disposition", {}).get("forced"):
                return f"embedded forced subtitle track #{st.get('index')}"
    if s.skip_if_external_forced:
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
        "-map", f"0:{stream_index}", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(proc.stdout, dtype=np.int16)


def to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) * (1.0 / 32768.0)


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


def find_speech(pcm: np.ndarray, s: Settings, progress: ProgressFn) -> list[Line]:
    opts = VadOptions(
        threshold=s.vad_threshold,
        min_speech_duration_ms=int(s.min_line_seconds * 1000),
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


def classify(probs: dict[str, float], seconds: float, s: Settings) -> tuple[bool, str, float]:
    lang, p = max(probs.items(), key=lambda kv: kv[1])
    if lang == s.main_language or lang in s.skip_languages:
        return False, lang, p
    if s.only_languages and lang not in s.only_languages:
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


def build_junk_re(s: Settings) -> re.Pattern:
    extra = [re.escape(p) for p in s.drop_phrases if p]
    return re.compile("|".join(BUILTIN_JUNK + extra), re.IGNORECASE)


def translate_chunk(model: WhisperModel, pcm: np.ndarray, chunk: list[Line], s: Settings,
                    junk: re.Pattern) -> list[Cue]:
    start, end = chunk[0].start, chunk[-1].end
    segments, _ = model.transcribe(
        to_float(pcm[start:end]),
        language=chunk[0].lang,
        task="translate",
        beam_size=s.beam_size,
        condition_on_previous_text=False,
        vad_filter=False,
        temperature=[0.0, 0.2, 0.4],
    )
    offset = start / SAMPLE_RATE
    chunk_len = (end - start) / SAMPLE_RATE
    prefix = ""
    if s.language_prefix:
        prefix = f"[{LANGUAGE_NAMES.get(chunk[0].lang, chunk[0].lang.upper())}] "
    cues: list[Cue] = []
    for seg in segments:
        text = " ".join(seg.text.split())
        if not text or junk.search(text):
            continue
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
            continue  # probably not speech
        if seg.compression_ratio > 2.4:
            continue  # repetition loop
        cues.append(Cue(offset + max(0.0, seg.start), offset + min(chunk_len, seg.end), prefix + text))
    return cues


# --------------------------------------------------------------------------- srt


def _ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def _layout(words: list[str], width: int, max_lines: int) -> Optional[list[str]]:
    """Greedy-balance words into <= max_lines lines of <= width chars, or None if they don't fit."""
    line = " ".join(words)
    if len(line) <= width:
        return [line]
    if max_lines == 1:
        return None
    total = len(" ".join(words))
    target = total / max_lines
    lines, cur = [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if cur and (len(cand) > width or (len(cur) >= target and len(lines) < max_lines - 1)):
            lines.append(cur)
            cur = w
        else:
            cur = cand
    lines.append(cur)
    if len(lines) > max_lines or any(len(x) > width for x in lines):
        return None
    return lines


def split_cue(c: Cue, s: Settings) -> list[Cue]:
    """Split text that doesn't fit on screen into consecutive cues timed by character count."""
    words = c.text.split()
    if _layout(words, s.max_line_chars, s.max_lines) is not None:
        return [Cue(c.start, c.end, "\n".join(_layout(words, s.max_line_chars, s.max_lines)))]
    # Greedy pass tells us how many cues are needed...
    greedy: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        if _layout(cur + [w], s.max_line_chars, s.max_lines) is None and cur:
            greedy.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        greedy.append(cur)
    # ...then spread the words evenly over that many cues so the last one isn't a stub.
    n = len(greedy)
    target = len(c.text) / n
    pieces: list[list[str]] = [[]]
    for w in words:
        if pieces[-1] and len(pieces) < n and len(" ".join(pieces[-1] + [w])) > target:
            pieces.append([])
        pieces[-1].append(w)
    if any(_layout(p, s.max_line_chars, s.max_lines) is None for p in pieces):
        pieces = greedy
    total = sum(len(" ".join(p)) for p in pieces) or 1
    out, t = [], c.start
    span = c.end - c.start
    for p in pieces:
        text = " ".join(p)
        dur = span * len(text) / total
        lines = _layout(p, s.max_line_chars, s.max_lines) or [text]
        out.append(Cue(t, t + dur, "\n".join(lines)))
        t += dur
    return out


def finalize_cues(cues: list[Cue], s: Settings) -> list[Cue]:
    cues = sorted(cues, key=lambda c: c.start)
    split: list[Cue] = []
    for c in cues:
        split.extend(split_cue(c, s))
    split.sort(key=lambda c: c.start)
    for i, c in enumerate(split):
        c.end = min(max(c.end, c.start + s.min_cue_seconds), c.start + s.max_cue_seconds)
        if i + 1 < len(split):
            nxt = split[i + 1].start
            c.end = min(c.end, nxt - 0.05)
        c.end = max(c.end, c.start + 0.3)  # never zero/negative, even when cues overlap
    return split


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = [f"{i}\n{_ts(c.start)} --> {_ts(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- main


def generate_forced_subtitles(video: Path, models: ModelHolder, s: Settings,
                              overwrite: bool = False, progress: ProgressFn = _noop) -> Result:
    t0 = time.monotonic()
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"Not found: {video}")
    out_path = forced_output_path(video, s)

    progress("Checking file", 0.0)
    info = probe(video)
    if overwrite:
        out_path.unlink(missing_ok=True)
    else:
        existing = find_existing_forced(video, info, s)
        if existing:
            return Result("skipped", f"Already has forced subtitles ({existing})")

    stream = pick_audio_stream(info, s)
    log.info("%s: using audio stream #%s (%s)", video.name, stream["index"], _stream_lang(stream) or "untagged")

    progress("Decoding audio", 0.0)
    pcm = load_audio(video, stream["index"])

    lines = find_speech(pcm, s, progress)
    log.info("%s: %d speech segments", video.name, len(lines))
    if not lines:
        return Result("no_foreign_dialogue", "No speech found", seconds=time.monotonic() - t0)

    model = models.get(s)

    # Pass 1: coarse windows. Most of a main-language movie is ruled out here.
    windows = group_windows(lines, s.window_seconds)
    window_probs = detect_languages(model, pcm, windows, s.batch_size, progress, "Detecting languages")
    suspect_windows = [w for w, p in zip(windows, window_probs) if p.get(s.main_language, 0.0) < s.refine_below]
    suspect = [ln for w in suspect_windows for ln in w]
    log.info("%s: %d/%d windows need a closer look (%d lines)",
             video.name, len(suspect_windows), len(windows), len(suspect))

    # Pass 2: line by line inside suspect windows.
    foreign: list[Line] = []
    if suspect:
        line_probs = detect_languages(model, pcm, [[ln] for ln in suspect], s.batch_size,
                                      progress, "Checking lines")
        for ln, probs in zip(suspect, line_probs):
            yes, lang, p = classify(probs, ln.seconds, s)
            if yes:
                ln.lang, ln.prob = lang, p
                foreign.append(ln)

    if not foreign:
        return Result("no_foreign_dialogue", "No foreign-language dialogue found",
                      seconds=time.monotonic() - t0)

    # Pass 3: translate.
    junk = build_junk_re(s)
    chunks = chunk_foreign(foreign, s)
    cues: list[Cue] = []
    for i, chunk in enumerate(chunks):
        cues.extend(translate_chunk(model, pcm, chunk, s, junk))
        progress("Translating", (i + 1) / len(chunks))

    if not cues:
        return Result("no_foreign_dialogue", "Foreign speech found but nothing translatable",
                      seconds=time.monotonic() - t0)

    cues = finalize_cues(cues, s)
    write_srt(cues, out_path)
    langs: dict[str, int] = {}
    for ln in foreign:
        langs[ln.lang] = langs.get(ln.lang, 0) + 1
    summary = ", ".join(f"{LANGUAGE_NAMES.get(k, k)} ({v})" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))
    return Result(
        "done",
        f"Wrote {len(cues)} subtitles from {len(foreign)} foreign lines: {summary}",
        output=str(out_path),
        foreign_lines=len(foreign),
        languages=langs,
        seconds=time.monotonic() - t0,
    )
