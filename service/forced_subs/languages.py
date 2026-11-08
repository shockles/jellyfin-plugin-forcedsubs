"""Whisper's languages with their names, ISO 639-2 codes and NLLB-200 codes.

Whisper code -> (English name, ISO 639-2/B code used in file names/media tags, NLLB code or None).
A language with no NLLB code can be detected and transcribed, but not used as a
subtitle target for translation.
"""

from __future__ import annotations

LANGUAGES: dict[str, tuple[str, str, str | None]] = {
    "en": ("English", "eng", "eng_Latn"),
    "zh": ("Chinese", "chi", "zho_Hans"),
    "de": ("German", "ger", "deu_Latn"),
    "es": ("Spanish", "spa", "spa_Latn"),
    "ru": ("Russian", "rus", "rus_Cyrl"),
    "ko": ("Korean", "kor", "kor_Hang"),
    "fr": ("French", "fre", "fra_Latn"),
    "ja": ("Japanese", "jpn", "jpn_Jpan"),
    "pt": ("Portuguese", "por", "por_Latn"),
    "tr": ("Turkish", "tur", "tur_Latn"),
    "pl": ("Polish", "pol", "pol_Latn"),
    "ca": ("Catalan", "cat", "cat_Latn"),
    "nl": ("Dutch", "dut", "nld_Latn"),
    "ar": ("Arabic", "ara", "arb_Arab"),
    "sv": ("Swedish", "swe", "swe_Latn"),
    "it": ("Italian", "ita", "ita_Latn"),
    "id": ("Indonesian", "ind", "ind_Latn"),
    "hi": ("Hindi", "hin", "hin_Deva"),
    "fi": ("Finnish", "fin", "fin_Latn"),
    "vi": ("Vietnamese", "vie", "vie_Latn"),
    "he": ("Hebrew", "heb", "heb_Hebr"),
    "uk": ("Ukrainian", "ukr", "ukr_Cyrl"),
    "el": ("Greek", "gre", "ell_Grek"),
    "ms": ("Malay", "may", "zsm_Latn"),
    "cs": ("Czech", "cze", "ces_Latn"),
    "ro": ("Romanian", "rum", "ron_Latn"),
    "da": ("Danish", "dan", "dan_Latn"),
    "hu": ("Hungarian", "hun", "hun_Latn"),
    "ta": ("Tamil", "tam", "tam_Taml"),
    "no": ("Norwegian", "nor", "nob_Latn"),
    "th": ("Thai", "tha", "tha_Thai"),
    "ur": ("Urdu", "urd", "urd_Arab"),
    "hr": ("Croatian", "hrv", "hrv_Latn"),
    "bg": ("Bulgarian", "bul", "bul_Cyrl"),
    "lt": ("Lithuanian", "lit", "lit_Latn"),
    "la": ("Latin", "lat", None),
    "mi": ("Maori", "mao", "mri_Latn"),
    "ml": ("Malayalam", "mal", "mal_Mlym"),
    "cy": ("Welsh", "wel", "cym_Latn"),
    "sk": ("Slovak", "slo", "slk_Latn"),
    "te": ("Telugu", "tel", "tel_Telu"),
    "fa": ("Persian", "per", "pes_Arab"),
    "lv": ("Latvian", "lav", "lvs_Latn"),
    "bn": ("Bengali", "ben", "ben_Beng"),
    "sr": ("Serbian", "srp", "srp_Cyrl"),
    "az": ("Azerbaijani", "aze", "azj_Latn"),
    "sl": ("Slovenian", "slv", "slv_Latn"),
    "kn": ("Kannada", "kan", "kan_Knda"),
    "et": ("Estonian", "est", "est_Latn"),
    "mk": ("Macedonian", "mac", "mkd_Cyrl"),
    "br": ("Breton", "bre", None),
    "eu": ("Basque", "baq", "eus_Latn"),
    "is": ("Icelandic", "ice", "isl_Latn"),
    "hy": ("Armenian", "arm", "hye_Armn"),
    "ne": ("Nepali", "nep", "npi_Deva"),
    "mn": ("Mongolian", "mon", "khk_Cyrl"),
    "bs": ("Bosnian", "bos", "bos_Latn"),
    "kk": ("Kazakh", "kaz", "kaz_Cyrl"),
    "sq": ("Albanian", "alb", "als_Latn"),
    "sw": ("Swahili", "swa", "swh_Latn"),
    "gl": ("Galician", "glg", "glg_Latn"),
    "mr": ("Marathi", "mar", "mar_Deva"),
    "pa": ("Punjabi", "pan", "pan_Guru"),
    "si": ("Sinhala", "sin", "sin_Sinh"),
    "km": ("Khmer", "khm", "khm_Khmr"),
    "sn": ("Shona", "sna", "sna_Latn"),
    "yo": ("Yoruba", "yor", "yor_Latn"),
    "so": ("Somali", "som", "som_Latn"),
    "af": ("Afrikaans", "afr", "afr_Latn"),
    "oc": ("Occitan", "oci", "oci_Latn"),
    "ka": ("Georgian", "geo", "kat_Geor"),
    "be": ("Belarusian", "bel", "bel_Cyrl"),
    "tg": ("Tajik", "tgk", "tgk_Cyrl"),
    "sd": ("Sindhi", "snd", "snd_Arab"),
    "gu": ("Gujarati", "guj", "guj_Gujr"),
    "am": ("Amharic", "amh", "amh_Ethi"),
    "yi": ("Yiddish", "yid", "ydd_Hebr"),
    "lo": ("Lao", "lao", "lao_Laoo"),
    "uz": ("Uzbek", "uzb", "uzn_Latn"),
    "fo": ("Faroese", "fao", "fao_Latn"),
    "ht": ("Haitian Creole", "hat", "hat_Latn"),
    "ps": ("Pashto", "pus", "pbt_Arab"),
    "tk": ("Turkmen", "tuk", "tuk_Latn"),
    "nn": ("Norwegian Nynorsk", "nno", "nno_Latn"),
    "mt": ("Maltese", "mlt", "mlt_Latn"),
    "sa": ("Sanskrit", "san", "san_Deva"),
    "lb": ("Luxembourgish", "ltz", "ltz_Latn"),
    "my": ("Burmese", "bur", "mya_Mymr"),
    "bo": ("Tibetan", "tib", "bod_Tibt"),
    "tl": ("Tagalog", "tgl", "tgl_Latn"),
    "mg": ("Malagasy", "mlg", "plt_Latn"),
    "as": ("Assamese", "asm", "asm_Beng"),
    "tt": ("Tatar", "tat", "tat_Cyrl"),
    "haw": ("Hawaiian", "haw", None),
    "ln": ("Lingala", "lin", "lin_Latn"),
    "ha": ("Hausa", "hau", "hau_Latn"),
    "ba": ("Bashkir", "bak", "bak_Cyrl"),
    "jw": ("Javanese", "jav", "jav_Latn"),
    "su": ("Sundanese", "sun", "sun_Latn"),
    "yue": ("Cantonese", "yue", "yue_Hant"),
}

# ISO 639-2 terminology codes (and a few others) that media files also use.
_EXTRA_ALIASES = {
    "fra": "fr", "deu": "de", "zho": "zh", "nld": "nl", "ces": "cs", "ron": "ro", "slk": "sk",
    "ell": "el", "fas": "fa", "isl": "is", "hye": "hy", "kat": "ka", "msa": "ms", "mya": "my",
    "sqi": "sq", "eus": "eu", "cym": "cy", "mri": "mi", "bod": "bo", "mkd": "mk", "nob": "no",
    "jv": "jw", "iw": "he", "in": "id", "fil": "tl", "cmn": "zh",
}

_ALIASES: dict[str, str] = {}
for _code, (_name, _iso2, _nllb) in LANGUAGES.items():
    _ALIASES[_code] = _code
    _ALIASES[_iso2] = _code
    _ALIASES[_name.lower()] = _code
_ALIASES.update(_EXTRA_ALIASES)


def normalize(code: str) -> str:
    """Map 'eng', 'en', 'English', 'fra', ... to Whisper's code. Unknown codes pass through lowercased."""
    c = (code or "").strip().lower()
    return _ALIASES.get(c, c)


def name(code: str) -> str:
    entry = LANGUAGES.get(code)
    return entry[0] if entry else code.upper()


def iso2(code: str) -> str:
    entry = LANGUAGES.get(code)
    return entry[1] if entry else code


def nllb(code: str) -> str | None:
    entry = LANGUAGES.get(code)
    return entry[2] if entry else None


def file_tag(code: str, style: str) -> str:
    """Language tag for subtitle file names: 'en' (style '1') or 'eng' (style '2')."""
    return iso2(code) if style == "2" else code
