#
# teaport — translations for what the brain says and shows on its own, without the LLM
# (today: Wi-Fi setup, wifi_voice.py and wifi_setup.py). The LLM already answers in the
# session's language; text the brain composes itself has to be translated here.
#
# Python's standard gettext — the machinery Django's _() wraps — over one catalog per
# language: teaport_brain/locale/<lang>/LC_MESSAGES/teaport.po is the editable source
# (any PO editor, e.g. Poedit), and the .mo beside it is what gettext loads, compiled
# from it by this module:
#
#     python -m teaport_brain.i18n          # recompile every .mo from its .po
#     python -m teaport_brain.i18n --check  # exit 1 if a committed .mo is stale
#     python3 -m teaport_brain.i18n --glyphs  # every shown character has a font (dev:
#                                             # fontTools + the fonts a phone has)
#
# (tests/test_i18n.py runs the check, the same way docs/CONFIG.md is held to its schema.)
#
# The language of a session is its voice's: the engine's TTS language (engine_tts.py,
# TTS_VOICE / TTS_LANGUAGE / OpenClaw's per-session choice / switch_voice), mapped to a
# catalog by ESPEAK_TO_LANG. A page in a phone's hands follows the phone's
# Accept-Language instead. English is the source language: its strings are the msgids,
# and a missing translation falls back to them.
#
import functools
import gettext
import os
import re
import struct
import sys

DOMAIN = "teaport"
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
# The engine's TTS languages (engine_tts._PREFIX_ESPEAK) -> catalog languages.
ESPEAK_TO_LANG = {
    "en-us": "en", "en-gb": "en", "es": "es", "fr-fr": "fr", "it": "it",
    "pt-br": "pt_BR", "hi": "hi", "ja": "ja", "cmn": "zh",
}
SOURCE_LANG = "en"
# Catalogs written right to left (the page sets dir="rtl" for them).
RTL = frozenset({"ar"})
# Each language by its own name, for a language picker: never translated, so a reader
# finds theirs whatever language the page is in (as every major vendor's picker does).
ENDONYMS = {
    "en": "English", "es": "Español", "fr": "Français", "it": "Italiano",
    "pt_BR": "Português (Brasil)", "de": "Deutsch", "nl": "Nederlands", "ru": "Русский",
    "ar": "العربية", "hi": "हिन्दी", "zh": "中文（简体）", "ja": "日本語", "ko": "한국어",
}



def N_(msgid: str) -> str:
    """Mark a string for translation without translating it here (gettext's N_ idiom):
    it travels as English — a failure reason in status.json, a form error — and the
    reader puts it in their language with T._() at the point of display."""
    return msgid


class T:
    """One language's strings: T._("text"), T.p("context", "text")."""

    def __init__(self, lang: str):
        self.lang = lang
        self._t = gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang], fallback=True)

    def _(self, msgid: str) -> str:
        return self._t.gettext(msgid)

    def p(self, context: str, msgid: str) -> str:
        return self._t.pgettext(context, msgid)

    @property
    def dir(self) -> str:
        return "rtl" if self.lang in RTL else "ltr"


@functools.cache
def get(lang: str) -> T:
    return T(lang if lang in catalogs() or lang == SOURCE_LANG else SOURCE_LANG)


def _have() -> dict[str, str]:
    """Every catalog (and the source language) by its lowercased name: 'pt_br' -> 'pt_BR'."""
    return {lang.lower(): lang for lang in (*catalogs(), SOURCE_LANG)}


def chosen(lang: str | None) -> T | None:
    """An explicit choice (?lang=, a cookie): its catalog, or None if it names none.
    Exactly a catalog's name, in any case and with - or _ ('pt_BR', 'pt-br')."""
    found = _have().get((lang or "").strip().replace("-", "_").lower())
    return get(found) if found else None


# Language tags whose catalog is not named after them.
_ALIASES = {
    "pt": "pt_BR",  # the one Portuguese the voice speaks (and has a catalog)
    "cmn": "zh",    # the engine's Mandarin (espeak's code)
    # Chinese of every script and region gets the Simplified catalog, the only one
    # there is: zh-TW, zh-HK and zh-Hant readers too. Deliberate -- a Traditional reader
    # can read Simplified far more easily than English; a zh_Hant catalog would be
    # the real fix, and would win here by name ("zh_TW" / "zh_Hant") once it exists.
    "zh": "zh",
}


def _best(tag: str) -> str | None:
    """The catalog for one language tag ('es-MX', 'fr_fr', 'PT', 'zh-Hant-TW', 'cmn'):
    the tag itself, else its language with the region dropped, else an alias."""
    have = _have()
    tag = tag.strip().replace("-", "_").lower()
    lang = tag.split("_", 1)[0]
    for candidate in (tag, lang, _ALIASES.get(lang, "").lower()):
        if candidate and candidate in have:
            return have[candidate]
    return None


def for_espeak(code: str | None) -> T:
    """The strings for a session whose TTS speaks `code` ('en-us', 'es', 'cmn', ...).
    TTS_LANGUAGE / ?language= also give plain or regional codes ('fr', 'es-MX', 'ja-JP',
    'pt'): those find their language's catalog the way a browser's tag does."""
    code = (code or "").strip().lower()
    if code in ESPEAK_TO_LANG:
        return get(ESPEAK_TO_LANG[code])
    return get(_best(code) or SOURCE_LANG)


def for_accept_language(header: str | None) -> T:
    """The best catalog for a browser's Accept-Language ('es-MX,es;q=0.9,en;q=0.8')."""
    choices = []
    for i, part in enumerate((header or "").split(",")):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        # The weight is the q parameter, case-insensitively (RFC 9110 12.4.2): "Q=0" refuses too.
        m = re.search(r"(?:^|;)\s*q\s*=\s*([0-9.]+)", params, re.I)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if tag.strip() and q > 0:
            choices.append((-q, i, tag.strip()))
    for _, _, tag in sorted(choices):
        found = _best(tag)
        if found:
            return get(found)
    return get(SOURCE_LANG)


@functools.cache
def catalogs() -> tuple[str, ...]:
    try:
        return tuple(sorted(d for d in os.listdir(LOCALE_DIR)
                            if os.path.exists(_po_path(d))))
    except OSError:
        return ()


def _po_path(lang: str) -> str:
    return os.path.join(LOCALE_DIR, lang, "LC_MESSAGES", f"{DOMAIN}.po")


# ------------------------------------------------------------------ .po -> .mo
# A msgfmt for the subset these catalogs use: msgctxt/msgid/msgstr, multi-line strings,
# comments, the usual escapes; no plurals. Deterministic, so a stale .mo is detectable.

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote(s: str) -> str:
    s = s.strip()
    if not (s.startswith('"') and s.endswith('"')):
        raise ValueError(f"not a quoted string: {s!r}")
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s[1:-1])


def parse_po(text: str) -> dict[str, str]:
    """{key: msgstr}; key is msgid, or msgctxt + '\\x04' + msgid (gettext's convention).
    Untranslated entries (empty msgstr) are left out, so they fall back to English, and
    so are fuzzy ones (a PO editor's "#, fuzzy": a guess nobody has checked), as msgfmt
    leaves them out -- except the header, which msgfmt keeps fuzzy or not."""
    entries: dict[str, str] = {}
    cur: dict[str, str] = {}
    fuzzy = False
    field = None

    def flush():
        nonlocal fuzzy
        if "msgid" in cur and cur.get("msgstr") and not (fuzzy and cur["msgid"]):
            key = cur["msgid"] if "msgctxt" not in cur else cur["msgctxt"] + "\x04" + cur["msgid"]
            entries[key] = cur["msgstr"]
        cur.clear()
        fuzzy = False

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            # A blank line ends an entry; so does a comment after its msgstr (the next
            # entry's comments, with no blank line between).
            if cur.get("msgstr") is not None:
                flush()
                field = None
            if line.startswith("#,") and "fuzzy" in re.split(r"[\s,]+", line[2:]):
                fuzzy = True
            continue
        m = re.match(r"(msgctxt|msgid|msgstr)\s+(\".*\")$", line)
        try:
            if m:
                if m.group(1) in ("msgctxt", "msgid") and "msgstr" in cur:
                    flush()
                elif m.group(1) in cur or (m.group(1) == "msgctxt" and "msgid" in cur):
                    # A second msgid (or a msgctxt after one) before any msgstr: the
                    # entry before has none, and its msgctxt must not become this one's.
                    raise ValueError(f"{m.group(1)} before the entry above has a msgstr")
                field = m.group(1)
                cur[field] = _unquote(m.group(2))
            elif line.startswith('"') and field:
                cur[field] += _unquote(line)
            else:
                raise ValueError(f"unexpected line: {raw!r}")
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
    flush()
    return entries


def compile_mo(entries: dict[str, str]) -> bytes:
    """GNU .mo bytes for `entries` (keys sorted, no hash table — gettext does not need one)."""
    keys = sorted(entries, key=lambda k: k.encode())
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        kb, vb = k.encode(), entries[k].encode()
        offsets.append((len(ids), len(kb), len(strs), len(vb)))
        ids += kb + b"\0"
        strs += vb + b"\0"
    n = len(keys)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    header = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    return header + struct.pack(f"{len(koffsets)}i", *koffsets) + \
        struct.pack(f"{len(voffsets)}i", *voffsets) + ids + strs


def compiled(lang: str) -> bytes:
    with open(_po_path(lang), encoding="utf-8") as f:
        return compile_mo(parse_po(f.read()))


def shown_characters() -> dict[str, set[str]]:
    """Every character a catalog can put on a screen, by language (patterns excluded:
    they are matched, never shown), plus the language picker's names."""
    out: dict[str, set[str]] = {}
    for lang in catalogs():
        with open(_po_path(lang), encoding="utf-8") as f:
            entries = parse_po(f.read())
        out[lang] = {c for k, v in entries.items() if k and not k.startswith("pattern\x04")
                     for c in v if not c.isspace()}
    out["picker"] = {c for name in ENDONYMS.values() for c in name if not c.isspace()}
    return out


def glyph_report() -> int:
    """Which shown characters no installed font can draw (they would be tofu: a box).
    Dev check, run on a machine with the fonts a phone has (Android's are Noto): needs
    fontTools and fontconfig's fc-list. Exit 1 if anything is missing."""
    import subprocess
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        print("needs fontTools (pip install fonttools) — try the system python3", file=sys.stderr)
        return 2
    files = subprocess.run(["fc-list", ":", "file"], capture_output=True, text=True).stdout
    covered: dict[int, set[str]] = {}
    for path in sorted({line.split(":")[0] for line in files.splitlines() if line.strip()}):
        try:
            fonts = TTCollection(path).fonts if path.lower().endswith(".ttc") else [TTFont(path, lazy=True)]
        except Exception:  # noqa: BLE001 — a font fontTools cannot read covers nothing
            continue
        for font in fonts:
            family = font["name"].getDebugName(1) or os.path.basename(path)
            cmap = font.getBestCmap() or {}
            for cp in cmap:
                covered.setdefault(cp, set()).add(family)
    missing_any = 0
    for lang, chars in shown_characters().items():
        gone = sorted(c for c in chars if ord(c) not in covered)
        noto = sorted(c for c in chars if ord(c) in covered
                      and not any(f.startswith("Noto") for f in covered[ord(c)]))
        missing_any += len(gone)
        line = f"{lang:7} {len(chars):4} characters: " + (
            "all drawable" if not gone else f"NO FONT for {''.join(gone)!r}")
        if noto:
            line += f"; not in Noto here: {''.join(noto)!r}"
        print(line)
    return 1 if missing_any else 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--glyphs" in argv:
        return glyph_report()
    check = "--check" in argv
    stale = []
    for lang in catalogs():
        mo = _po_path(lang)[:-3] + ".mo"
        data = compiled(lang)
        try:
            with open(mo, "rb") as f:
                current = f.read()
        except OSError:
            current = None
        if current == data:
            continue
        if check:
            stale.append(lang)
        else:
            with open(mo, "wb") as f:
                f.write(data)
            print(f"{mo}: compiled")
    if stale:
        print(f"stale .mo for {', '.join(stale)} — run: python -m teaport_brain.i18n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
