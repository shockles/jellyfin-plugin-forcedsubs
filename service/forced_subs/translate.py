"""Text translation with NLLB-200 (Meta) running on CTranslate2.

Whisper can only translate speech into English, so subtitles in any other
language are made by translating the English (or main-language) subtitle text.

The NLLB-200 weights are licensed CC-BY-NC-4.0: fine for personal use, not for
commercial use.
"""

from __future__ import annotations

import logging
import os
import re

import ctranslate2
from huggingface_hub import snapshot_download
from tokenizers import Tokenizer

from . import languages

log = logging.getLogger("forced_subs")

# Ready-made CTranslate2 conversions published by the OpenNMT project.
MODELS = {
    "nllb-1.3B": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",   # ~1.4 GB, good balance
    "nllb-3.3B": "OpenNMT/nllb-200-3.3B-ct2-int8",             # ~3.3 GB, best quality
}


class Translator:
    def __init__(self, model: str, device: str):
        repo = MODELS.get(model, model)  # also accepts any Hugging Face repo id of a CT2 NLLB model
        log.info("Loading translation model %s on %s", repo, device)
        path = snapshot_download(repo)
        compute = "int8_float16" if device == "cuda" else "int8"
        self.translator = ctranslate2.Translator(path, device=device, compute_type=compute)
        self.tokenizer = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
        self.key = (model, device)

    def translate(self, texts: list[str], src: str, tgt: str, beam_size: int = 4) -> list[str]:
        """Translate a batch of lines from Whisper language code ``src`` to ``tgt``.

        NLLB is a sentence-level model: given several sentences it tends to drop
        some, and given a very short one it sometimes adds a second. So each line
        is split into sentences, every sentence is translated on its own (all in
        one GPU batch), extra invented sentences are trimmed, and the pieces are
        joined back into lines.
        """
        if not texts:
            return []
        src_code, tgt_code = languages.nllb(src), languages.nllb(tgt)
        if not src_code or not tgt_code:
            raise ValueError(f"Translation between {src} and {tgt} is not supported")

        pieces: list[str] = []
        owners: list[int] = []
        for i, t in enumerate(texts):
            for s in split_sentences(t):
                pieces.append(s)
                owners.append(i)

        batch = [[src_code] + self.tokenizer.encode(p, add_special_tokens=False).tokens + ["</s>"] for p in pieces]
        results = self.translator.translate_batch(
            batch,
            target_prefix=[[tgt_code]] * len(batch),
            beam_size=beam_size,
            max_batch_size=64,
            batch_type="examples",
            max_decoding_length=256,
        )
        translated = [[] for _ in texts]
        for owner, src_piece, r in zip(owners, pieces, results):
            tokens = r.hypotheses[0][1:]  # drop the target-language token
            ids = [i for i in (self.tokenizer.token_to_id(t) for t in tokens) if i is not None]
            text = self.tokenizer.decode(ids, skip_special_tokens=True).strip()
            translated[owner].append(_trim_extra(text, src_piece))
        return [" ".join(p for p in parts if p) for parts in translated]


# Sentence ends: . ! ? … and their full-width forms, followed by space or end.
_SENT_END = re.compile(r"(?<=[.!?…。！？])\s+")
_DIALOG_DASH = re.compile(r"^\s*[-–—]\s*")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_END.split(text.strip()) if p.strip()]
    return parts or [text.strip()]


def _trim_extra(translated: str, source: str) -> str:
    """Drop sentences NLLB invented: keep at most as many sentences as the source had."""
    text = _DIALOG_DASH.sub("", translated)
    if not _DIALOG_DASH.match(source):
        text = re.sub(r"\s+[-–—]\s+", " ", text)  # "- Guten Abend. - Danke." -> "Guten Abend. Danke."
    want = len(split_sentences(source))
    have = re.split(r"(?<=[.!?…。！？])\s*", text)
    have = [h for h in have if h.strip()]
    if len(have) > want:
        text = " ".join(h.strip() for h in have[:want])
    return text.strip()
