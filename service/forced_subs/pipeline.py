"""Subtitle pipeline: forced and/or full subtitles, in any of Whisper's languages.

1. Pick the movie's main audio track and decode it to 16 kHz mono.
2. Find every spoken line with Silero VAD (CPU, cheap).
3. Detect the language of the speech in ~12 s windows, batched on the GPU.
   Windows that are clearly the main language are skipped; the rest are checked line by line.
4. Translate the foreign lines to English with Whisper (task="translate").
5. Full subtitles only: transcribe the main-language lines, batched.
6. For other subtitle languages, translate the text with NLLB-200 (see translate.py).
7. Write ``Movie.en.forced.srt``, ``Movie.es.srt`` ... next to the movie.

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
from . import languages
from faster_whisper import BatchedInferencePipeline, WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.vad import VadOptions, get_speech_timestamps

log = logging.getLogger("forced_subs")

SAMPLE_RATE = 16000
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi"}

# Phrases Whisper tends to invent from its training captions.
BUILTIN_JUNK = [
    r"thank(s| you) (so much )?for watching",
    r"subtitles? (by|made by|created by)",
    r"please subscribe",
    r"like and subscribe",
    r"amara\.org",
    r"^\W*$",
]


@dataclass
class Settings:
    # What to make
    make_forced: bool = True            # only the foreign lines (Movie.en.forced.srt)
    make_full: bool = False             # every line (Movie.en.srt)
    subtitle_languages: list = field(default_factory=lambda: ["en"])

    # Model
    model: str = "large-v3"             # NOT large-v3-turbo: turbo can't translate well
    device: str = "cuda"                # cuda | cpu
    compute_type: str = "int8_float16"  # ~3 GB VRAM for large-v3, fine on an 8 GB 4060
    beam_size: int = 5
    transcribe_batch_size: int = 8      # full subtitles: chunks decoded in parallel
    translation_model: str = "nllb-3.3B"  # nllb-3.3B (best) | nllb-1.3B (faster) | a HF repo id
    translation_beam_size: int = 4

    # Audio track
    audio_languages: list = field(default_factory=lambda: ["en"])  # preferred main-audio languages
    require_audio_language: bool = False  # skip files with no track in audio_languages
    skip_commentary: bool = True

    # Speech detection
    vad_threshold: float = 0.5
    min_line_seconds: float = 0.25

    # Language detection
    main_language: str = "en"           # speech in this language is never in forced subtitles
    window_seconds: float = 12.0
    batch_size: int = 8
    refine_below: float = 0.90
    foreign_min_prob: float = 0.60
    short_line_seconds: float = 1.2
    short_line_min_prob: float = 0.85
    skip_languages: list = field(default_factory=list)   # never put these in forced subtitles
    only_languages: list = field(default_factory=list)   # if set, forced subtitles only cover these

    # Translation
    max_chunk_seconds: float = 28.0
    merge_gap_seconds: float = 1.5
    drop_phrases: list = field(default_factory=list)      # extra phrases to drop (case-insensitive)

    # Existing subtitles
    skip_if_embedded_forced: bool = True
    skip_if_external_forced: bool = True
    skip_existing_full: bool = True     # don't make Movie.es.srt if a Spanish subtitle already exists

    # Output
    language_tag_style: str = "1"       # "1" -> Movie.en.srt, "2" -> Movie.eng.srt
    mark_default: bool = False          # forced files get .default so players select them
    language_prefix: bool = False       # "[Russian] Good evening." on foreign lines
    max_line_chars: int = 42
    max_lines: int = 2
    min_cue_seconds: float = 1.0
    max_cue_seconds: float = 7.0

    def with_options(self, options: Optional[dict]) -> "Settings":
        """Return a copy with known keys from ``options`` applied (unknown keys are ignored)."""
        options = dict(options or {})
        # Back-compat with 0.2.0's output_language_tag ("en" / "eng").
        if "output_language_tag" in options and "language_tag_style" not in options:
            options["language_tag_style"] = "2" if str(options["output_language_tag"]).lower() == "eng" else "1"
        known = {f.name for f in dataclasses.fields(self)}
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
                        value = [v.strip() for v in re.split(r"[,\n;]", value) if v.strip()]
                    value = list(value)
                else:
                    value = str(value).strip()
            except (TypeError, ValueError):
                log.warning("Ignoring bad value for %s: %r", key, value)
                continue
            changes[key] = value
        s = dataclasses.replace(self, **changes)
        norm = languages.normalize
        s.main_language = norm(s.main_language) or "en"
        s.audio_languages = [norm(x) for x in s.audio_languages]
        s.skip_languages = [norm(x) for x in s.skip_languages]
        s.only_languages = [norm(x) for x in s.only_languages]
        s.subtitle_languages = list(dict.fromkeys(norm(x) for x in s.subtitle_languages)) or ["en"]
        s.language_tag_style = "2" if s.language_tag_style in ("2", "eng", "iso639-2") else "1"
        s.max_lines = max(1, s.max_lines)
        s.max_line_chars = max(10, s.max_line_chars)
        s.batch_size = max(1, s.batch_size)
        s.transcribe_batch_size = max(1, s.transcribe_batch_size)
        s.beam_size = max(1, s.beam_size)
        s.translation_beam_size = max(1, s.translation_beam_size)
        return s

    @property
    def model_key(self) -> tuple:
        return (self.model, self.device, self.compute_type)


@dataclass
class Result:
    status: str                         # done | no_foreign_dialogue | skipped
    message: str
    output: Optional[str] = None        # first file written (kept for older plugin versions)
    outputs: list = field(default_factory=list)
    foreign_lines: int = 0
    languages: dict = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


ProgressFn = Callable[[str, float], None]


def _noop(stage: str, fraction: float) -> None:
    pass


class SkipItem(Exception):
    """Raised when a file should not be processed (e.g. it already has the subtitles)."""


class Cancelled(Exception):
    """Raised from a progress callback to stop a job."""


# --------------------------------------------------------------------------- models


class ModelHolder:
    """Loads Whisper / the translator on demand, reloads on settings changes, unloads to free the GPU."""

    def __init__(self) -> None:
        self._model: Optional[WhisperModel] = None
        self._key: Optional[tuple] = None
        self._translator = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None or self._translator is not None

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

    def translator(self, s: Settings):
        from .translate import Translator  # imported lazily: only needed for non-English subtitles

        with self._lock:
            key = (s.translation_model, s.device)
            if self._translator is not None and self._translator.key != key:
                self._translator = None
                gc.collect()
            if self._translator is None:
                self._translator = Translator(s.translation_model, s.device)
            return self._translator

    def unload_whisper(self) -> None:
        with self._lock:
            if self._model is not None:
                log.info("Unloading Whisper model")
                self._model = None
                self._key = None
                gc.collect()

    def unload(self) -> None:
        self.unload_whisper()
        with self._lock:
            if self._translator is not None:
                log.info("Unloading translation model")
                self._translator = None
                gc.collect()


# --------------------------------------------------------------------------- media


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(out.stdout)


def _stream_lang(stream: dict) -> str:
    return languages.normalize(stream.get("tags", {}).get("language") or "")


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


# --------------------------------------------------------------------------- outputs & existing subs


@dataclass
class Output:
    mode: str     # forced | full
    lang: str     # Whisper code
    path: Path


@dataclass
class ExistingSubs:
    embedded: list   # (lang, forced)
    external: list   # (langs set, forced)

    @classmethod
    def scan(cls, video: Path, info: dict) -> "ExistingSubs":
        embedded = [
            (_stream_lang(st), bool(st.get("disposition", {}).get("forced")))
            for st in info.get("streams", []) if st.get("codec_type") == "subtitle"
        ]
        external = []
        stem = video.stem.lower()
        for f in video.parent.iterdir():
            name = f.name.lower()
            if f.suffix.lower() not in SUBTITLE_EXTS or not name.startswith(stem + "."):
                continue
            tokens = name[len(stem) + 1:].split(".")[:-1]
            external.append(({languages.normalize(t) for t in tokens}, "forced" in tokens))
        return cls(embedded, external)

    def covers(self, out: Output, s: Settings) -> Optional[str]:
        forced = out.mode == "forced"
        if forced:
            # An untagged forced track is assumed to be in the movie's main language.
            if s.skip_if_embedded_forced and any(
                    f and (lang or s.main_language) == out.lang for lang, f in self.embedded):
                return "embedded forced track"
            if s.skip_if_external_forced and any(f and out.lang in langs for langs, f in self.external):
                return "forced subtitle file"
        elif s.skip_existing_full:
            if any(not f and lang == out.lang for lang, f in self.embedded):
                return "embedded subtitle track"
            if any(not f and out.lang in langs for langs, f in self.external):
                return "subtitle file"
        return None


def output_path(video: Path, mode: str, lang: str, s: Settings) -> Path:
    tag = languages.file_tag(lang, s.language_tag_style)
    if mode == "forced":
        flags = ".default.forced" if s.mark_default else ".forced"
    else:
        flags = ""
    return video.with_name(f"{video.stem}.{tag}{flags}.srt")


def plan_outputs(video: Path, info: dict, s: Settings, overwrite: bool) -> tuple[list[Output], list[str]]:
    """Work out which files to write and why others are skipped."""
    modes = [m for m, on in (("forced", s.make_forced), ("full", s.make_full)) if on]
    existing = None if overwrite else ExistingSubs.scan(video, info)
    todo, notes = [], []
    for lang in s.subtitle_languages:
        if lang not in languages.LANGUAGES:
            notes.append(f"unknown language '{lang}'")
            continue
        if lang != "en" and not languages.nllb(lang):
            notes.append(f"{languages.name(lang)} can't be a subtitle language")
            continue
        for mode in modes:
            out = Output(mode, lang, output_path(video, mode, lang, s))
            if existing is not None:
                reason = existing.covers(out, s)
                if reason:
                    notes.append(f"{_label(out)} skipped ({reason})")
                    continue
            todo.append(out)
    return todo, notes


def _label(out: Output) -> str:
    return f"{languages.name(out.lang)} {out.mode}"


# --------------------------------------------------------------------------- speech & language detection


@dataclass
class Line:
    start: int  # samples
    end: int
    lang: str = ""
    prob: float = 0.0
    foreign: bool = False   # spoken in a language other than the main one
    forced: bool = False    # foreign and allowed in forced subtitles (skip/only lists)

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
    merged: list[Line] = []
    for ln in lines:  # join lines split at block boundaries
        if merged and ln.start - merged[-1].end < SAMPLE_RATE * 0.05:
            merged[-1].end = ln.end
        else:
            merged.append(ln)
    return merged


def _detect_batch(model: WhisperModel, clips: list[np.ndarray]) -> list[dict[str, float]]:
    """Return a {language: probability} dict per clip, encoding clips in one GPU batch."""
    feats = np.stack([pad_or_trim(model.feature_extractor(to_float(c))) for c in clips])
    results = model.model.detect_language(model.encode(feats))
    return [{tok[2:-2]: p for tok, p in r} for r in results]


def detect_languages(model: WhisperModel, pcm: np.ndarray, items: list[list[Line]],
                     batch_size: int, progress: ProgressFn, stage: str) -> list[dict[str, float]]:
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


def classify_line(ln: Line, probs: dict[str, float], s: Settings) -> None:
    lang, p = max(probs.items(), key=lambda kv: kv[1])
    ln.lang, ln.prob = lang, p
    needed = s.short_line_min_prob if ln.seconds < s.short_line_seconds else s.foreign_min_prob
    ln.foreign = lang != s.main_language and p >= needed
    ln.forced = ln.foreign and lang not in s.skip_languages and (not s.only_languages or lang in s.only_languages)


def classify_all(model: WhisperModel, pcm: np.ndarray, lines: list[Line], s: Settings,
                 progress: ProgressFn) -> None:
    """Mark each line foreign or not. Pass 1 rules out clear main-language windows cheaply."""
    windows = group_windows(lines, s.window_seconds)
    window_probs = detect_languages(model, pcm, windows, s.batch_size, progress, "Detecting languages")
    suspect: list[Line] = []
    for w, p in zip(windows, window_probs):
        if p.get(s.main_language, 0.0) < s.refine_below:
            suspect.extend(w)
        else:
            for ln in w:
                ln.lang, ln.prob = s.main_language, p.get(s.main_language, 0.0)
    log.info("%d/%d lines need a closer look", len(suspect), len(lines))
    if suspect:
        line_probs = detect_languages(model, pcm, [[ln] for ln in suspect], s.batch_size, progress, "Checking lines")
        for ln, probs in zip(suspect, line_probs):
            classify_line(ln, probs, s)


# --------------------------------------------------------------------------- transcription & translation


@dataclass
class Cue:
    start: float
    end: float
    text: str
    text_lang: str          # language `text` is written in
    src_lang: str           # language that was spoken
    foreign: bool = False
    forced: bool = False


def build_junk_re(s: Settings) -> re.Pattern:
    extra = [re.escape(p) for p in s.drop_phrases if p]
    return re.compile("|".join(BUILTIN_JUNK + extra), re.IGNORECASE)


def _keep(seg, junk: re.Pattern) -> Optional[str]:
    text = " ".join(seg.text.split())
    if not text or junk.search(text):
        return None
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
        return None  # probably not speech
    if seg.compression_ratio > 2.4:
        return None  # repetition loop
    return text


def chunk_lines(lines: list[Line], s: Settings, same_lang: bool) -> list[list[Line]]:
    chunks: list[list[Line]] = []
    for ln in lines:
        if chunks:
            last = chunks[-1]
            gap = (ln.start - last[-1].end) / SAMPLE_RATE
            span = (ln.end - last[0].start) / SAMPLE_RATE
            if (not same_lang or ln.lang == last[0].lang) and gap <= s.merge_gap_seconds and span <= s.max_chunk_seconds:
                last.append(ln)
                continue
        chunks.append([ln])
    return chunks


def translate_foreign(model: WhisperModel, pcm: np.ndarray, lines: list[Line], s: Settings,
                      junk: re.Pattern, progress: ProgressFn) -> list[Cue]:
    """Whisper speech translation of foreign lines into English."""
    chunks = chunk_lines(lines, s, same_lang=True)
    cues: list[Cue] = []
    for i, chunk in enumerate(chunks):
        start, end = chunk[0].start, chunk[-1].end
        segments, _ = model.transcribe(
            to_float(pcm[start:end]), language=chunk[0].lang, task="translate", beam_size=s.beam_size,
            condition_on_previous_text=False, vad_filter=False, temperature=[0.0, 0.2, 0.4],
        )
        offset, length = start / SAMPLE_RATE, (end - start) / SAMPLE_RATE
        forced = any(ln.forced for ln in chunk)
        for seg in segments:
            text = _keep(seg, junk)
            if text:
                cues.append(Cue(offset + max(0.0, seg.start), offset + min(length, seg.end), text,
                                "en", chunk[0].lang, foreign=True, forced=forced))
        progress("Translating foreign lines", (i + 1) / len(chunks))
    return cues


def transcribe_main(model: WhisperModel, pcm: np.ndarray, lines: list[Line], s: Settings,
                    junk: re.Pattern, progress: ProgressFn) -> list[Cue]:
    """Batched transcription of the main-language lines (full subtitles only)."""
    if not lines:
        return []
    chunks = chunk_lines(lines, s, same_lang=False)
    clips = [{"start": c[0].start / SAMPLE_RATE, "end": c[-1].end / SAMPLE_RATE} for c in chunks]
    pipe = BatchedInferencePipeline(model)
    segments, _ = pipe.transcribe(
        to_float(pcm), language=s.main_language, task="transcribe", beam_size=s.beam_size,
        batch_size=s.transcribe_batch_size, clip_timestamps=clips, condition_on_previous_text=False,
        without_timestamps=False,
    )
    total_end = clips[-1]["end"] or 1.0
    cues: list[Cue] = []
    for seg in segments:
        text = _keep(seg, junk)
        if text:
            cues.append(Cue(seg.start, seg.end, text, s.main_language, s.main_language))
        progress("Transcribing dialogue", min(1.0, seg.end / total_end))
    return cues


def localize(cues: list[Cue], lang: str, models: ModelHolder, s: Settings,
             cache: dict, progress: ProgressFn) -> list[Cue]:
    """Return copies of ``cues`` with text in ``lang``, translating with NLLB where needed."""
    need: dict[str, list[str]] = {}
    for c in cues:
        if c.text_lang != lang and (c.text, c.text_lang, lang) not in cache:
            if not languages.nllb(c.text_lang):
                continue  # e.g. a Latin-language movie: its lines can't be translated, so they're left out
            need.setdefault(c.text_lang, []).append(c.text)
    if need:
        if s.device == "cuda" and "3.3B" in s.translation_model:
            models.unload_whisper()  # make room for the big translator on 8 GB cards
        tr = models.translator(s)
        for src, texts in need.items():
            texts = list(dict.fromkeys(texts))
            progress(f"Translating to {languages.name(lang)}", 0.0)
            for i in range(0, len(texts), 256):
                part = texts[i:i + 256]
                for t, out in zip(part, tr.translate(part, src, lang, s.translation_beam_size)):
                    cache[(t, src, lang)] = out
                progress(f"Translating to {languages.name(lang)}", min(1.0, (i + 256) / len(texts)))
    result = []
    for c in cues:
        text = c.text if c.text_lang == lang else cache.get((c.text, c.text_lang, lang), "")
        text = text.strip()
        if not text:
            continue
        if s.language_prefix and c.foreign:
            text = f"[{languages.name(c.src_lang)}] {text}"
        result.append(dataclasses.replace(c, text=text, text_lang=lang))
    return result


# --------------------------------------------------------------------------- srt


def _ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


# Scripts written without spaces between words; their characters are double width on screen.
CJK_LANGS = {"ja", "zh", "yue"}


def _units(text: str, lang: str) -> tuple[list[str], str]:
    """Split text into wrap units: words, or single characters for CJK scripts.

    For CJK, spaces NLLB puts between phrases are dropped, but a space between two
    Latin-script words ("iPhone 15") is kept as its own unit.
    """
    if lang in CJK_LANGS:
        text = re.sub(r"\s+", " ", text.strip())
        units = []
        for i, ch in enumerate(text):
            if ch == " " and not (text[i - 1].isascii() and text[i + 1].isascii()):
                continue
            units.append(ch)
        return units or [text], ""
    return text.split(), " "


def _layout(words: list[str], width: int, max_lines: int, sep: str = " ") -> Optional[list[str]]:
    """Balance words into <= max_lines lines of <= width chars, or None if they don't fit."""
    line = sep.join(words)
    if len(line) <= width:
        return [line]
    if max_lines == 1:
        return None
    target = len(line) / max_lines
    lines, cur = [], ""
    for w in words:
        cand = f"{cur}{sep}{w}" if cur else w
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
    words, sep = _units(c.text, c.text_lang)
    if not words or not c.text.strip():
        return []
    width = max(5, s.max_line_chars // 2) if c.text_lang in CJK_LANGS else s.max_line_chars

    def layout(ws: list[str]) -> Optional[list[str]]:
        return _layout(ws, width, s.max_lines, sep)

    fit = layout(words)
    if fit is not None:
        return [dataclasses.replace(c, text="\n".join(fit))]
    greedy: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        if layout(cur + [w]) is None and cur:
            greedy.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        greedy.append(cur)
    n = len(greedy)
    target = len(sep.join(words)) / n
    pieces: list[list[str]] = [[]]
    for w in words:  # spread words evenly so the last cue isn't a stub
        if pieces[-1] and len(pieces) < n and len(sep.join(pieces[-1] + [w])) > target:
            pieces.append([])
        pieces[-1].append(w)
    if any(layout(p) is None for p in pieces):
        pieces = greedy
    total = sum(len(sep.join(p)) for p in pieces) or 1
    out, t, span = [], c.start, c.end - c.start
    for p in pieces:
        text = sep.join(p)
        dur = span * len(text) / total
        lines = layout(p) or [text]
        out.append(dataclasses.replace(c, start=t, end=t + dur, text="\n".join(lines)))
        t += dur
    return out


def finalize_cues(cues: list[Cue], s: Settings) -> list[Cue]:
    split: list[Cue] = []
    for c in sorted(cues, key=lambda c: c.start):
        split.extend(split_cue(c, s))
    split.sort(key=lambda c: c.start)
    for i, c in enumerate(split):
        c.end = min(max(c.end, c.start + s.min_cue_seconds), c.start + s.max_cue_seconds)
        if i + 1 < len(split):
            c.end = min(c.end, split[i + 1].start - 0.05)
        c.end = max(c.end, c.start + 0.3)  # never zero/negative, even when cues overlap
    return split


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = [f"{i}\n{_ts(c.start)} --> {_ts(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- main


def generate_subtitles(video: Path, models: ModelHolder, s: Settings,
                       overwrite: bool = False, progress: ProgressFn = _noop) -> Result:
    t0 = time.monotonic()
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"Not found: {video}")
    if not (s.make_forced or s.make_full):
        return Result("skipped", "Neither forced nor full subtitles are turned on")

    progress("Checking file", 0.0)
    info = probe(video)
    todo, notes = plan_outputs(video, info, s, overwrite)
    if not todo:
        return Result("skipped", "Nothing to do: " + ("; ".join(notes) or "no subtitle languages"))
    need_full = any(o.mode == "full" for o in todo)

    stream = pick_audio_stream(info, s)
    log.info("%s: audio stream #%s (%s); making %s", video.name, stream["index"],
             _stream_lang(stream) or "untagged", ", ".join(_label(o) for o in todo))

    progress("Decoding audio", 0.0)
    pcm = load_audio(video, stream["index"])
    lines = find_speech(pcm, s, progress)
    if not lines:
        return Result("no_foreign_dialogue", "No speech found", seconds=time.monotonic() - t0)

    model = models.get(s)
    classify_all(model, pcm, lines, s, progress)
    foreign = [ln for ln in lines if ln.foreign and (need_full or ln.forced)]
    junk = build_junk_re(s)

    cues = translate_foreign(model, pcm, foreign, s, junk, progress) if foreign else []
    if need_full:
        cues += transcribe_main(model, pcm, [ln for ln in lines if not ln.foreign], s, junk, progress)
    cues.sort(key=lambda c: c.start)

    del pcm  # free ~230 MB before translation
    written, cache = [], {}
    for out in todo:
        if out.mode == "forced":
            selected = [c for c in cues if c.forced and c.src_lang != out.lang]
        else:
            selected = cues
        if not selected:
            notes.append(f"{_label(out)}: nothing to write")
            continue
        final = finalize_cues(localize(selected, out.lang, models, s, cache, progress), s)
        if overwrite:
            out.path.unlink(missing_ok=True)
        write_srt(final, out.path)
        written.append({"mode": out.mode, "lang": out.lang, "path": str(out.path), "cues": len(final)})

    langs: dict[str, int] = {}
    for ln in lines:
        if ln.foreign:
            langs[ln.lang] = langs.get(ln.lang, 0) + 1
    seconds = time.monotonic() - t0
    if not written:
        status = "no_foreign_dialogue" if not langs else "done"
        return Result(status, "; ".join(notes) or "Nothing written", languages=langs, seconds=seconds)

    parts = [f"{languages.file_tag(w['lang'], s.language_tag_style)}{'.forced' if w['mode'] == 'forced' else ''} "
             f"({w['cues']})" for w in written]
    spoken = ", ".join(f"{languages.name(k)} ({v})" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))
    msg = "Wrote " + ", ".join(parts)
    if spoken:
        msg += f". Foreign lines: {spoken}"
    if notes:
        msg += ". " + "; ".join(notes)
    return Result("done", msg, output=written[0]["path"], outputs=written,
                  foreign_lines=sum(langs.values()), languages=langs, seconds=seconds)


# Name used by 0.1/0.2 callers.
generate_forced_subtitles = generate_subtitles
